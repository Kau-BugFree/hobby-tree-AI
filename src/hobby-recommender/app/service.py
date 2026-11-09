"""답변 검증 → 활동 조건 필터 → 점수 계산을 수행하는 상태 없는 추천 엔진."""
from statistics import mean

from app.config import Activity, DataBundle
from app.models import (
    ActivityResult, Coverage, ExcludedHobby, Preferences, Recommendation,
    RecommendationRequest, RecommendationResponse, ScorePart,
)


class AnswerValidationError(ValueError):
    """필드별 설문 오류를 모아 HTTP 계층의 422 응답으로 전달한다."""

    def __init__(self, errors: list[dict]):
        """FastAPI의 detail 배열과 호환되는 오류 목록을 보관한다."""
        self.errors = errors
        super().__init__("Invalid onboarding answers")


def selections(request: RecommendationRequest, key: str) -> list[str]:
    """단일·복수·미응답을 모두 선택 문자열 목록으로 정규화한다."""
    value = request.answers.get(key)
    return [value] if isinstance(value, str) else (value or [])


class RecommendationService:
    """시작 시 검증한 데이터 묶음으로 요청마다 독립적인 추천을 계산한다."""

    def __init__(self, data: DataBundle):
        """설문·규칙·카탈로그를 주입하며 사용자 데이터는 저장하지 않는다."""
        self.data = data

    def validate(self, request: RecommendationRequest):
        """선택지·배타 조건·직접 입력 쌍·자원 ID를 검사하고 오류를 한 번에 알린다."""
        errors = []

        def fail(field, message):
            """점으로 구분한 필드 경로를 클라이언트가 표시할 loc 배열로 바꾼다."""
            errors.append({"loc": ["body", *field.split(".")], "msg": message, "type": "value_error"})

        # 요청 모델의 자료형 검증 뒤, 실제 설문 정의에 맞는 선택 조합인지 확인한다.
        for key, value in request.answers.items():
            group = self.data.groups.get(key)
            if group is None:
                fail(f"answers.{key}", "정의되지 않은 질문 키입니다.")
                continue
            if value is None:
                continue
            if (group.kind == "single" and not isinstance(value, str)) or (group.kind == "multi" and not isinstance(value, list)):
                fail(f"answers.{key}", "단일 선택은 문자열, 복수 선택은 문자열 배열이어야 합니다.")
                continue
            selected = selections(request, key)
            if any(option not in group.options for option in selected):
                fail(f"answers.{key}", "정의된 선택지 문구를 그대로 보내 주세요.")
            if len(selected) != len(set(selected)):
                fail(f"answers.{key}", "중복 선택은 허용하지 않습니다.")
            if group.max is not None and len(selected) > group.max:
                fail(f"answers.{key}", f"최대 {group.max}개까지 선택할 수 있습니다.")
            if len(selected) > 1 and set(selected) & set(group.exclusive):
                fail(f"answers.{key}", "단독 선택 항목을 다른 항목과 함께 보낼 수 없습니다.")

        # 직접 입력 선택과 실제 입력값은 반드시 함께 존재해야 한다.
        for key, option, field in [
            ("q04", "직접 금액 입력", "q04_amount_krw"),
            ("q05", "직접 금액 입력", "q05_amount_krw"),
            ("q06", "직접 시간 입력", "q06_minutes"),
            ("q02", "다른 취미 직접 입력", "q02_other"),
            ("q09Exclude", "직접 입력", "q09_exclude_text"),
        ]:
            selected = option in selections(request, key)
            value = getattr(request.custom_values, field)
            present = value is not None and (not isinstance(value, str) or bool(value.strip()))
            if selected and not present:
                fail(f"custom_values.{field}", "직접 입력 선택에 해당하는 값을 함께 보내 주세요.")
            elif value is not None and not selected:
                fail(f"custom_values.{field}", "직접 입력 선택 없이 값을 보낼 수 없습니다.")

        for resource in request.resources:
            if resource not in self.data.catalog.resources:
                fail(f"resources.{resource}", "등록되지 않은 자원 ID입니다.")
        if errors:
            raise AnswerValidationError(errors)

    def _score(self, request: RecommendationRequest, hobby_id: str) -> list[ScorePart]:
        """질문별 최댓값/평균 적합도에 가중치를 곱해 점수와 설명 근거를 만든다."""
        parts = []
        for criterion in self.data.rules.criteria:
            selected = selections(request, criterion.key)
            # 점수표에 없는 관심사·모름 응답은 근거에서 제외하고, 근거가 없으면 0.5다.
            fits = [criterion.scores[o][hobby_id] for o in selected if o in criterion.scores]
            fit = (max(fits) if criterion.aggregation == "max" else mean(fits)) if fits else 0.5
            parts.append(ScorePart(
                question_key=criterion.key, weight=criterion.weight, fit=round(fit, 6),
                points=round(criterion.weight * fit, 4), answered=bool(fits), selected=selected,
            ))
        return parts

    @staticmethod
    def _limits(request: RecommendationRequest):
        """원화·분 단위 한도를 계산하고 월 기지출을 차감한다. 미정은 None이다."""
        money = {"0원": 0, "1만 원까지": 10_000, "3만 원까지": 30_000, "5만 원까지": 50_000, "10만 원까지": 100_000}
        times = {"10분": 10, "15분": 15, "30분": 30, "1시간": 60, "2시간": 120}
        first = request.custom_values.q04_amount_krw if request.answers.get("q04") == "직접 금액 입력" else money.get(request.answers.get("q04"))
        monthly = request.custom_values.q05_amount_krw if request.answers.get("q05") == "직접 금액 입력" else money.get(request.answers.get("q05"))
        minutes = request.custom_values.q06_minutes if request.answers.get("q06") == "직접 시간 입력" else times.get(request.answers.get("q06"))
        if monthly is not None and request.monthly_committed_krw is not None:
            monthly = max(0, monthly - request.monthly_committed_krw)
        return first, monthly, minutes

    def _activity(self, request: RecommendationRequest, activity: Activity):
        """필수 조건 위반 활동을 제외하고, 나머지 활동의 미확인 조건을 모은다."""
        first, monthly, minutes = self._limits(request)
        exclusions = set(selections(request, "q09Exclude")) - {"없음", "직접 입력"}
        # 명시적인 제외 조건·한도 초과·사용 불가 자원은 점수보다 먼저 적용한다.
        rejected = []
        matches = sorted(exclusions & set(activity.exclusion_tags))
        if matches:
            rejected.append("반드시 제외 조건: " + ", ".join(matches))
        if first is not None and activity.first_cost_krw > first:
            rejected.append(f"첫 활동 예상비용 {activity.first_cost_krw:,}원이 한도 {first:,}원을 초과해요.")
        if monthly is not None and activity.first_cost_krw > monthly:
            rejected.append(f"첫 활동 예상비용이 이번 달 가용 한도 {monthly:,}원을 초과해요.")
        if minutes is not None and activity.total_minutes > minutes:
            rejected.append(f"전체 예상시간 {activity.total_minutes}분이 한도 {minutes}분을 초과해요.")
        for resource in activity.required_resources:
            if request.resources.get(resource) is False:
                rejected.append(f"사용 불가로 확인한 자원: {self.data.catalog.resources[resource]}")
        if rejected:
            return None, [f"{activity.title}: {reason}" for reason in rejected]

        # 미확인 자원과 조건은 가능하다고 단정하지 않고 확인 목록에 남긴다.
        pending = list(activity.checks)
        for resource in activity.required_resources:
            if request.resources.get(resource) is not True:
                qualifier = "구매 없이 " if "새 장비 구매" in exclusions else ""
                pending.append(f"{qualifier}{self.data.catalog.resources[resource]} 사용 가능 여부를 확인해 주세요.")
        if not selections(request, "q09Exclude"):
            pending.append("반드시 피해야 할 조건이 있는지 확인해 주세요.")
        if minutes is None:
            pending.append("준비·정리·이동을 포함한 전체 시간을 확인해 주세요.")
        if activity.first_cost_krw > 0:
            if first is None:
                pending.append("첫 활동에 쓸 수 있는 금액을 확인해 주세요.")
            if monthly is None or request.monthly_committed_krw is None:
                pending.append("다른 취미의 지출·예약액을 포함해 이번 달 남은 예산을 확인해 주세요.")
        pending = list(dict.fromkeys(pending))
        return ActivityResult(
            activity_id=activity.id, title=activity.title,
            total_minutes=activity.total_minutes, active_minutes=activity.active_minutes,
            estimated_first_cost_krw=activity.first_cost_krw,
            estimated_repeat_cost_krw=activity.repeat_cost_krw,
            status="needs_confirmation" if pending else "eligible",
            confirmation_required=pending,
        ), []

    def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        """검증된 답변으로 상태·취미 순위·첫 활동·추가 확인 질문을 구성한다."""
        self.validate(request)
        # 선택/기록 방식은 화면 선호이며 취미 점수를 임의로 바꾸지 않는다.
        preferences = Preferences(
            selection_mode=request.answers.get("q11") or "2~3개 후보 중 직접 고르기",
            recording_mode=request.answers.get("q12"),
            burdens=selections(request, "q13"), weekly_frequency=request.answers.get("q07"),
        )
        coverage_parts = self._score(request, self.data.catalog.hobbies[0].id)
        coverage = Coverage(
            weighted_answer_coverage=sum(p.weight for p in coverage_parts if p.answered),
            answered_groups=sum(bool(selections(request, key)) for key in self.data.groups),
            total_groups=len(self.data.groups),
        )
        supported = {h.interest_option for h in self.data.catalog.hobbies}
        unsupported = [o for o in selections(request, "q02") if o not in supported and o not in {"아직 모르겠어요", "다른 취미 직접 입력"}]
        if request.custom_values.q02_other:
            unsupported.append(request.custom_values.q02_other.strip())
        notices = [
            "점수는 규칙상 선호 적합도이며 성공 확률이나 검증된 추천 정확도가 아닙니다.",
            "활동의 비용·시간은 예시 추정치입니다. 후보를 동시에 실행하는 월간 예산 계획은 계산하지 않습니다.",
        ]
        if coverage.weighted_answer_coverage == 0:
            notices.append("점수에 쓸 선호 정보가 없어 중립 점수 50점으로 후보를 보여 드립니다.")
        if unsupported:
            names = "·".join(h.name for h in self.data.catalog.hobbies)
            notices.append(f"아직 지원하지 않는 관심 취미가 있습니다. 현재 후보는 {names}입니다. 입력은 이 응답에 반환하며 서버에는 저장하지 않습니다.")
        if request.answers.get("q04") == "나와 맞는지 확인한 뒤 결정":
            notices.append("첫 활동 예산은 미정으로 처리했습니다. 0원으로 해석하지 않습니다.")
        if request.answers.get("q11") == "전체 단계를 보며 고르기":
            notices.append("로드맵 표시 선호를 전달합니다. 이 API는 첫 활동 후보를 반환하며 전체 로드맵은 앱의 별도 기능입니다.")

        # 어떤 분기에서도 버전·안내·충실도 필드를 가진 동일한 응답 계약을 유지한다.
        result = RecommendationResponse(
            status="recommended", schema_version=self.data.questionnaire.version,
            rules_version=self.data.rules.version, catalog_version=self.data.catalog.version,
            recommendations=[], excluded=[], unsupported_interests=unsupported,
            notices=notices, follow_up_questions=[], preferences=preferences, coverage=coverage,
        )
        if request.answers.get("q01") == "지금은 취미를 시작할 생각이 없어요" or preferences.selection_mode == "안내 없이 직접 찾기":
            result.status = "browse"
            result.notices.append("둘러보기와 나중에 시작하기를 표시해 주세요. 순위 추천을 강요하지 않습니다.")
            return result
        # Q09와 Q14 자유 입력은 자동 해석하지 않는다. 둘 다 있으면 모두 확인한다.
        if "직접 입력" in selections(request, "q09Exclude"):
            result.follow_up_questions.append("직접 입력한 제외 조건을 구조화된 조건으로 확인해 주세요: " + request.custom_values.q09_exclude_text.strip())
            result.notices.append("자유 입력 제외 조건은 자동 추론하지 않습니다. 조건 검토 전에는 활동을 추천하지 않습니다.")
        if request.context_text and request.context_text.strip():
            result.follow_up_questions.append("추가 고려사항을 구조화된 조건으로 확인해 주세요: " + request.context_text.strip())
            result.notices.append("Q14 자유 입력은 자동 해석하지 않습니다. 고려사항 검토 전에는 활동을 추천하지 않습니다.")
        if result.follow_up_questions:
            result.status = "needs_review"
            return result

        # 활동이 하나라도 조건을 통과한 취미만 점수를 계산하고 후보에 포함한다.
        for hobby in self.data.catalog.hobbies:
            activities, rejection_reasons = [], []
            for activity in hobby.activities:
                accepted, rejected = self._activity(request, activity)
                if accepted:
                    activities.append(accepted)
                rejection_reasons.extend(rejected)
            if not activities:
                result.excluded.append(ExcludedHobby(hobby_id=hobby.id, name=hobby.name, reasons=rejection_reasons))
                continue
            parts = self._score(request, hobby.id)
            reasons = [f"{self.data.groups[p.question_key].title} → {', '.join(p.selected)}: {p.points:g}/{p.weight:g}점" for p in parts if p.answered and p.fit > 0.5]
            if not reasons:
                reasons = ["현재 답변에서 뚜렷한 선호 근거가 적어 중립 기준을 포함해 계산했어요."]
            activities.sort(key=lambda a: (a.status != "eligible", a.estimated_first_cost_krw, a.total_minutes, a.activity_id))
            result.recommendations.append(Recommendation(
                hobby_id=hobby.id, name=hobby.name, ui_key=hobby.ui_key, rank=0,
                score=round(sum(p.points for p in parts), 4), breakdown=parts,
                reasons=reasons, activities=activities,
            ))

        # 점수 동점은 ID로 정렬해 재현성을 보장하고, Q11/요청 top_k를 적용한다.
        result.recommendations.sort(key=lambda rec: (-rec.score, rec.hobby_id))
        limit = request.top_k if request.top_k is not None else (1 if preferences.selection_mode == "한 개를 제안받기" else 3)
        result.recommendations = result.recommendations[:limit]
        for rank, rec in enumerate(result.recommendations, 1):
            rec.rank = rank
            result.follow_up_questions.extend(rec.activities[0].confirmation_required)
        result.follow_up_questions = list(dict.fromkeys(result.follow_up_questions))
        if not result.recommendations:
            result.status = "no_match"
            result.notices.append("현재 조건을 만족하는 등록 활동이 없습니다. 제외 조건을 자동으로 완화하지 않았습니다.")
        return result
