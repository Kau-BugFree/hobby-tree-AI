"""설문 입력과 추천 결과의 JSON 계약을 엄격하게 검증하는 Pydantic 모델."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """알 수 없는 필드와 문자열→숫자 등의 묵시적 형 변환을 거부한다."""
    model_config = ConfigDict(extra="forbid", strict=True)


class ErrorResponse(StrictModel):
    """500·503에서 내부 예외 대신 클라이언트에 전달하는 안내 메시지."""
    detail: str


# 원화 금액과 직접 입력 문구의 공통 범위를 요청 필드마다 동일하게 적용한다.
Money = Annotated[int, Field(ge=0, le=1_000_000_000)]
Text200 = Annotated[str, Field(min_length=1, max_length=200)]


class CustomValues(StrictModel):
    """Q02·Q04·Q05·Q06·Q09의 직접 입력 선택과 함께 보내는 실제 값."""
    q04_amount_krw: Money | None = None
    q05_amount_krw: Money | None = None
    q06_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None
    q02_other: Text200 | None = None
    q09_exclude_text: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class RecommendationRequest(StrictModel):
    """추천 엔진 입력. 화면 전용 상태와 Spring의 추천 생성 요청은 포함하지 않는다."""
    schema_version: Literal["onboarding-2026-10-13"] = "onboarding-2026-10-13"
    answers: dict[str, str | list[str] | None] = Field(default_factory=dict, max_length=14)
    custom_values: CustomValues = Field(default_factory=CustomValues)
    # Q14는 구조화된 제약으로 추측하지 않고 서비스에서 검토 필요 상태로 돌려준다.
    context_text: Text200 | None = None
    # Q08의 넓은 물품 분류로 추측하지 않고, 개별 자원 사용 가능 여부만 받는다.
    resources: dict[str, bool] = Field(default_factory=dict, max_length=100)
    monthly_committed_krw: Money | None = None
    top_k: Annotated[int, Field(ge=1, le=10)] | None = None


class ScorePart(StrictModel):
    """한 질문이 특정 취미의 점수에 기여한 가중치·적합도·선택 근거."""
    question_key: str
    weight: float
    fit: float
    points: float
    answered: bool
    selected: list[str]


class ActivityResult(StrictModel):
    """조건 필터를 통과한 활동의 비용·시간과 아직 확인해야 할 사항."""
    activity_id: str
    title: str
    total_minutes: int
    active_minutes: int
    estimated_first_cost_krw: int
    estimated_repeat_cost_krw: int
    status: Literal["eligible", "needs_confirmation"]
    confirmation_required: list[str]


class Recommendation(StrictModel):
    """취미별 순위·설명·활동 후보. hobby_id는 엔진 코드이며 DB 숫자 ID가 아니다."""
    hobby_id: str
    name: str
    ui_key: str
    rank: int
    score: float
    breakdown: list[ScorePart]
    reasons: list[str]
    activities: list[ActivityResult]


class ExcludedHobby(StrictModel):
    """모든 활동이 필수 조건에서 탈락한 취미와 구체적인 제외 이유."""
    hobby_id: str
    name: str
    reasons: list[str]


class Preferences(StrictModel):
    """점수 계산과 별개로 앱이 표시·기록·계획에 사용하는 사용자 선호."""
    selection_mode: str
    recording_mode: str | None
    burdens: list[str]
    weekly_frequency: str | None


class Coverage(StrictModel):
    """점수에 사용된 답변의 가중치 합과 응답한 설문 그룹 수."""
    weighted_answer_coverage: float
    answered_groups: int
    total_groups: int


class RecommendationResponse(StrictModel):
    """추천·탐색·검토·후보 없음 상태를 구분하는 엔진 응답 전체."""
    status: Literal["recommended", "browse", "needs_review", "no_match"]
    schema_version: str
    rules_version: str
    catalog_version: str
    recommendations: list[Recommendation]
    excluded: list[ExcludedHobby]
    unsupported_interests: list[str]
    notices: list[str]
    follow_up_questions: list[str]
    preferences: Preferences
    coverage: Coverage
