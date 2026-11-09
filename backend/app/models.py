"""Swagger에 표시되는 요청 형식. 잘못된 조건과 단계는 입력 단계에서 거부합니다."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

# 빈 도구명은 '' in 도구 비교가 항상 참이 되어 모든 활동을 걸러내므로 받지 않는다.
ToolName = Annotated[str, Field(min_length=1, max_length=100)]


class Contract(BaseModel):
    """정의되지 않은 요청 필드는 거부하고 문자열 앞뒤 공백을 제거합니다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Constraints(Contract):
    """세션 내 조건입니다. 누락은 유지, 명시한 null은 해제입니다."""

    max_minutes: float | None = Field(
        None, gt=0, le=43200, description="오늘의 전체 가용 시간. null은 제한 해제"
    )
    available_tools: list[ToolName] | None = Field(
        None, max_length=30, description="쓸 수 있는 도구. null은 도구 제한 없음"
    )
    excluded_tools: list[ToolName] | None = Field(None, max_length=30)
    max_difficulty: int | None = Field(
        None,
        ge=0,
        le=5,
        description="카탈로그/나무 단계와 다른 별도 난이도. 미지정 자료는 조건 충족으로 보지 않음",
    )
    budget_max: int | None = Field(
        None,
        ge=0,
        description="현재 자료에는 검증된 비용이 없어 조건 지정 시 확인 안내",
    )


class SessionCreate(Contract):
    """새 대화의 취미 선택 요청입니다."""

    hobby_key: str = Field("cooking", min_length=1, max_length=40)


class CoachRequest(Contract):
    """질문과 화면 맥락입니다. 키나 임의의 검색 근거는 받지 않습니다."""

    session_id: str | None = Field(None, min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    activity_id: str | None = Field(None, min_length=1, max_length=120)
    current_step: int | None = Field(None, ge=1, le=500)
    constraints: Constraints | None = None
    request_id: str | None = Field(
        None,
        min_length=1,
        max_length=120,
        description="같은 메시지의 재전송에는 동일한 값 사용",
    )


class SearchRequest(Contract):
    """카탈로그 또는 참고 레시피 범위를 지정하는 검색 요청입니다."""

    query: str = Field("", max_length=1000)
    hobby_key: str = Field("cooking", min_length=1, max_length=40)
    scope: Literal["activities", "recipes", "knowledge"] = "activities"
    constraints: Constraints = Field(default_factory=Constraints)
    limit: int = Field(3, ge=1, le=20)


class SelectRequest(Contract):
    """추천 카드에서 선택한 활동 ID입니다."""

    activity_id: str = Field(min_length=1, max_length=120)


class ExecutionCreate(SelectRequest):
    """중복 방지 요청 ID를 포함한 새 활동 수행 요청입니다."""

    request_id: str = Field(min_length=1, max_length=120)


class ProgressUpdate(Contract):
    """전체 단계 수와 같은 길이의 완료 체크 배열입니다."""

    done: list[bool] = Field(min_length=1, max_length=500)


class AskRequest(Contract):
    """RAG 검증용 단일 질문입니다. 대화 세션·분기를 거치지 않습니다."""

    question: str = Field(min_length=1, max_length=2000)
    hobby_key: str = Field("cooking", min_length=1, max_length=40)
    constraints: Constraints = Field(default_factory=Constraints)
    top_k: int = Field(3, ge=1, le=5, description="Claude에 근거로 넘길 상위 활동 수")


class RecordCreate(Contract):
    """완료 기록 요청입니다. 중복 판단의 기준은 execution_id입니다."""

    execution_id: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=1, max_length=120)
    note: str = Field("", max_length=2000)
