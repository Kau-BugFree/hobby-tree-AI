"""요청을 받기 전에 설문·점수 규칙·활동 카탈로그와 상호 참조를 검증한다."""
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.models import StrictModel


class QuestionGroup(StrictModel):
    """질문 한 묶음의 선택 방식·허용 문구·배타 조건·직접 입력 옵션."""
    key: str
    code: str
    title: str
    kind: Literal["single", "multi"]
    options: list[str]
    exclusive: list[str] = Field(default_factory=list)
    max: int | None = None
    custom_option: str | None = None


class Questionnaire(StrictModel):
    """같은 버전으로 배포하는 설문 그룹 전체."""
    version: str
    groups: list[QuestionGroup]


class Criterion(StrictModel):
    """질문별 가중치와 선택지→취미 적합도 표."""
    key: str
    weight: float = Field(gt=0)
    aggregation: Literal["max", "mean"]
    scores: dict[str, dict[str, float]]


class Rules(StrictModel):
    """점수 정책 버전과 가중치 기준 목록."""
    version: str
    criteria: list[Criterion]


class Activity(StrictModel):
    """필터링에 필요한 활동 비용·전체 시간·필수 자원·제외 태그."""
    id: str
    title: str
    total_minutes: int = Field(ge=1)
    active_minutes: int = Field(ge=1)
    first_cost_krw: int = Field(ge=0)
    repeat_cost_krw: int = Field(ge=0)
    required_resources: list[str]
    exclusion_tags: list[str]
    checks: list[str]

    @model_validator(mode="after")
    def check_time(self):
        """직접 활동 시간이 준비·대기를 포함한 전체 시간보다 길면 거부한다."""
        if self.active_minutes > self.total_minutes:
            raise ValueError("active_minutes cannot exceed total_minutes")
        return self


class Hobby(StrictModel):
    """엔진 취미 코드와 화면 키를 그 취미의 활동 목록에 연결한다."""
    id: str
    name: str
    ui_key: str
    interest_option: str
    activities: list[Activity] = Field(min_length=1)


class Catalog(StrictModel):
    """카탈로그 버전, 확인 가능한 자원 이름, 지원 취미 목록."""
    version: str
    resources: dict[str, str]
    hobbies: list[Hobby] = Field(min_length=1)


class DataBundle:
    """세 JSON 파일을 함께 검증해 부분적으로 불일치한 데이터 배포를 막는다."""

    def __init__(self, directory: Path):
        """파일을 UTF-8로 읽고 구조 검증 후 파일 간 참조를 검사한다."""
        def read(filename):
            """지정된 데이터 디렉터리 안의 JSON 파일 한 개를 읽는다."""
            return json.loads((directory / filename).read_text(encoding="utf-8"))

        self.questionnaire = Questionnaire.model_validate(read("questionnaire.json"))
        self.rules = Rules.model_validate(read("rules.json"))
        self.catalog = Catalog.model_validate(read("catalog.json"))
        self.groups = {q.key: q for q in self.questionnaire.groups}
        self._validate_references()

    def _validate_references(self):
        """ID 중복·버전·가중치 합계·선택지·취미·자원 참조의 일관성을 검사한다."""
        # 서로 다른 항목이 같은 ID를 공유하면 조회·정렬·설명이 모호해지므로 거부한다.
        hobby_ids = [h.id for h in self.catalog.hobbies]
        criteria_keys = [c.key for c in self.rules.criteria]
        activity_ids = [a.id for h in self.catalog.hobbies for a in h.activities]
        for label, values in [("hobby", hobby_ids), ("criterion", criteria_keys), ("activity", activity_ids)]:
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label} IDs")
        if len(self.groups) != len(self.questionnaire.groups):
            raise ValueError("Duplicate question keys")
        # Q09는 장소와 필수 제외라는 별개 그룹이며 Q14는 요청의 context_text이다.
        required_keys = {f"q{i:02d}" for i in range(1, 14)} - {"q09"} | {"q09Place", "q09Exclude"}
        if set(self.groups) != required_keys:
            raise ValueError("Questionnaire keys do not match the v1 request contract")
        if self.questionnaire.version != "onboarding-2026-10-13":
            raise ValueError("Questionnaire version requires a new request contract")
        if not math.isclose(sum(c.weight for c in self.rules.criteria), 100):
            raise ValueError("Criterion weights must sum to 100")
        for group in self.groups.values():
            if len(group.options) != len(set(group.options)) or not group.options:
                raise ValueError(f"Invalid options: {group.key}")
            if not set(group.exclusive) <= set(group.options):
                raise ValueError(f"Invalid exclusive options: {group.key}")
            if group.custom_option and group.custom_option not in group.options:
                raise ValueError(f"Invalid custom option: {group.key}")
            if group.max is not None and group.max < 1:
                raise ValueError("Selection max must be positive")
        # 각 점수 선택지는 모든 취미에 대해 유한한 0~1 적합도를 가져야 한다.
        for criterion in self.rules.criteria:
            if criterion.key not in self.groups:
                raise ValueError(f"Unknown scoring question: {criterion.key}")
            for option, scores in criterion.scores.items():
                if option not in self.groups[criterion.key].options:
                    raise ValueError(f"Unknown scoring option: {option}")
                if set(scores) != set(hobby_ids):
                    raise ValueError("Every scored option must define every hobby")
                if any(not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()):
                    raise ValueError("Fit must be finite and between 0 and 1")
        # 활동의 자원·제외 태그가 설문과 자원 사전에 실제로 정의되어 있는지 검사한다.
        for hobby in self.catalog.hobbies:
            if hobby.interest_option not in self.groups["q02"].options:
                raise ValueError(f"Unknown interest option: {hobby.id}")
            for activity in hobby.activities:
                if not set(activity.required_resources) <= set(self.catalog.resources):
                    raise ValueError(f"Unknown resources: {activity.id}")
                allowed_tags = set(self.groups["q09Exclude"].options) - {"없음", "직접 입력"}
                if not set(activity.exclusion_tags) <= allowed_tags:
                    raise ValueError(f"Unknown exclusion tags: {activity.id}")
