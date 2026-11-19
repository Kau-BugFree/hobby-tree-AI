"""에이전트가 한 턴에 고르는 행동 하나의 정의. 실행은 서버가 하고, 사용자에게 보일 글은 쓰지 않는다.

각 모델의 docstring이 언제 그 행동을 고르는지 설명하며 ACTION_GUIDE로 모여 지시문에 붙는다.
"""
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError

from .dialogue_models import Contract, ObservationUpdate, RequirementProposal

Move = Literal["next", "prev"] | None


class SelectCandidate(Contract):
    """추천 후보 중 하나를 번호·순서·이름으로 가리키며 그것으로 하겠다는 말. 번호로 가리킨 것을 묻는 물음과 'N단계'는 해당하지 않는다.
    후보 목록이 없거나 범위 밖이면 서버가 '해당 순서의 추천이 없습니다'로 안내한다. number는 1부터 센 후보 순서다."""
    action: Literal["select_candidate"]
    number: int


class StartActivity(Contract):
    """선택한 활동을 시작하거나 이어서 하겠다는 말. 확인 없이 실행되며 진행 중인 수행은 이어 간다.
    처음부터 다시 하겠다고 하면 restart를 참으로 한다(새 수행을 1단계부터 시작).
    후보 번호를 댄 말은 select_candidate, 'N단계부터 하겠다'는 show_steps다."""
    action: Literal["start_activity"]
    restart: bool = False


class DeclineStart(Contract):
    """활동을 시작하지 않겠다거나 시작·다시 시작하지 말라는 말(시작 취소 포함). 어렵다는 하소연은 아니다. 현재 진행 상태는 그대로 둔다."""
    action: Literal["decline_start"]


class CompleteActivity(Contract):
    """활동(요리) 전체를 마쳤다는 말에만. 해결됐다는 말은 완료가 아니고(열린 문제가 있으면 coach의 resolve),
    단계나 일부 작업만 마쳤다는 말은 notice의 partial_completion이다."""
    action: Literal["complete_activity"]


class AnswerConfirmation(Contract):
    """대기 중인 확인 질문(완료 확인, 문제 전환 확인)에 대한 답만. 수락이면 accept를 참으로 한다."""
    action: Literal["answer_confirmation"]
    accept: bool


class ShowMaterials(Contract):
    """선택한 활동의 준비물을 묻는 말. 다음·이전 단계의 준비물이면 move를 next·prev로 한다."""
    action: Literal["show_materials"]
    move: Move = None


class ShowDuration(Contract):
    """선택한 활동의 소요 시간을 묻는 말. 다음·이전 단계의 시간이면 move를 next·prev로 한다."""
    action: Literal["show_duration"]
    move: Move = None


class ShowSteps(Contract):
    """선택한 활동의 단계를 묻는 말. 특정 단계는 step, 다음·이전 단계는 move, 전체 순서는 whole을 참으로 한다.
    'N단계부터 하겠다'는 말도 여기다(step에 그 번호)."""
    action: Literal["show_steps"]
    step: int | None = None
    move: Move = None
    whole: bool = False


class ShowRecords(Contract):
    """지금까지의 완료 기록을 묻는 말."""
    action: Literal["show_records"]


class Recommend(Contract):
    """요리나 활동을 찾거나 추천받으려는 말. 요리 이름만 말해도 해당한다.
    topic은 주제, max_minutes는 시간 상한, clear_time_limit는 시간 조건 해제, excluded_tools는 쓰지 않겠다는 도구,
    scope는 활동·레시피 범위, other는 이미 본 것 말고 다른 것을 원할 때 참이다."""
    action: Literal["recommend"]
    topic: str = ""
    max_minutes: float | None = None
    clear_time_limit: bool = False
    excluded_tools: list[str] = Field(default_factory=list)
    scope: Literal["activities", "recipes"] | None = None
    other: bool = False


class Explain(Contract):
    """활동의 사실이나 방법을 묻는 조회·비교. target은 선택한 활동(selected), 후보 비교나 번호로 가리킨 후보
    (candidates, 번호는 candidate_numbers), 활동 이름이나 주제로 찾기(search, 주제는 topic) 중 하나다.
    체크 기준·점검 항목 같은 활동 자료를 묻는 말도 여기다.
    특정 단계를 물으면 step에 그 번호를 넣는다.
    자료의 원문을 그대로 보여 달라고 하면 original을 참으로 한다(원문을 거절하는 말은 아님)."""
    action: Literal["explain"]
    target: Literal["selected", "candidates", "search"]
    candidate_numbers: list[int] = Field(default_factory=list)
    topic: str = ""
    step: int | None = None
    original: bool = False


class CoachTurn(Contract):
    """선택한 활동을 하다가 겪은 어려움이나 이상(막힘, 아직 못 함 포함)을 말하거나(dialogue) 자기 수행을 평가해 달라는 말(reflection).
    transition은 문제 흐름(계속·정정·새 문제·해결·중단), observation_updates는 사용자 보고 인용,
    requirement_proposals는 요구 항목, step은 문제가 생긴 단계다. 어려움 없는 진행 보고에 new_issue를 열지 않는다.
    같은 문제에 대한 말(관찰, 질문, 확인 질문의 답)은 transition을 continue로 한다. none은 문제 흐름과 무관한
    자기 평가(reflection)에만 쓴다."""
    action: Literal["coach"]
    route: Literal["dialogue", "reflection"] = "dialogue"
    transition: Literal["continue", "correct", "new_issue", "resolve", "abandon", "none"] = "none"
    transition_quote: str = ""
    # TurnInterpretation과 같은 한도. 넘으면 형식이 맞지 않는 판단이다(실행 전에 거른다).
    observation_updates: list[ObservationUpdate] = Field(default_factory=list, max_length=8)
    requirement_proposals: list[RequirementProposal] = Field(default_factory=list, max_length=8)
    step: int | None = None


class Notice(Contract):
    """고정 안내로 답할 말. kind는 인사·감사·맞장구와 어려움 없는 진행 보고(greeting), 코치가 할 수 있는 일(capabilities), 추천 이유(recommend_reason),
    확신 여부(certainty), 요리와 무관한 활동·주제에 대한 물음이나 보고, 알레르기가 있는데 먹어도 되는지 같은 판정 요청(out_of_scope),
    특정 단계나 일부 작업만 마쳤다고 구체적으로 말한 것(partial_completion)이다."""
    action: Literal["notice"]
    kind: Literal["greeting", "capabilities", "recommend_reason", "certainty", "out_of_scope", "partial_completion"]


class AskUser(Contract):
    """뜻을 정할 수 없어 되물을 말. 다른 행동으로 답할 수 없을 때만 고른다. kind는 말의 뜻 자체를 모름(unclear), 어느 활동인지 모름(which_activity),
    요청이 두 가지(two_requests)다."""
    action: Literal["ask_user"]
    kind: Literal["unclear", "which_activity", "two_requests"]


Action = (SelectCandidate | StartActivity | DeclineStart | CompleteActivity | AnswerConfirmation | ShowMaterials
          | ShowDuration | ShowSteps | ShowRecords | Recommend | Explain | CoachTurn | Notice | AskUser)
ACTIONS: tuple[type[Contract], ...] = (SelectCandidate, StartActivity, DeclineStart, CompleteActivity,
                                       AnswerConfirmation, ShowMaterials, ShowDuration, ShowSteps, ShowRecords,
                                       Recommend, Explain, CoachTurn, Notice, AskUser)
ACTION_NAMES: tuple[str, ...] = tuple(model.model_fields["action"].annotation.__args__[0] for model in ACTIONS)
ACTION_GUIDE: str = "\n".join(f"{name}: {' '.join(model.__doc__.split())}"
                              for name, model in zip(ACTION_NAMES, ACTIONS))


# Claude에게 보내고 받는 형식. 선택 필드나 합집합이 있으면 공급자 문법이 커져 호출이 400으로 거부된다.
# 그래서 모든 필드가 필수이고, 고른 행동에 해당하지 않는 인자는 Claude가 중립값으로 채운다. 서버는 from_wire로 줄인다.
class WireObservation(Contract):
    quote: str = Field(description="사용자 보고. 현재 발화의 글자 그대로")
    supersedes: list[str] = Field(description="정정하는 이전 관찰 ID. 없으면 빈 목록")
    status: Literal["reported", "unknown"] = Field(description="모르겠다는 보고는 unknown")


class WireRequirement(Contract):
    text: str = Field(description="사용자가 알고 싶은 것")
    kind: Literal["lookup", "procedure", "cause", "remedy", "assessment"] = Field(description="요구 항목의 종류")
    source_quote: str = Field(description="근거가 된 현재 발화의 글자 그대로")


class WireDecision(Contract):
    action: Literal[ACTION_NAMES] = Field(description="고른 행동 하나")
    number: int = Field(description="select_candidate의 후보 번호(1부터). 해당 없으면 0")
    restart: bool = Field(description="start_activity에서 처음부터 다시 할 때 true")
    accept: bool = Field(description="answer_confirmation에서 수락이면 true")
    move: Literal["none", "next", "prev"] = Field(description="다음·이전 단계로 옮길 때 next·prev. 아니면 none")
    step: int = Field(description="말한 단계 번호. 없으면 0")
    whole: bool = Field(description="show_steps에서 전체 순서를 원할 때 true")
    topic: str = Field(description="recommend·explain의 주제. 없으면 빈 글")
    max_minutes: float = Field(description="recommend의 새 시간 상한(분). 변경 없으면 0")
    clear_time_limit: bool = Field(description="recommend에서 시간 조건을 풀 때 true")
    excluded_tools: list[str] = Field(description="recommend에서 쓰지 않겠다는 도구. 없으면 빈 목록")
    scope: Literal["keep", "activities", "recipes"] = Field(description="recommend의 범위. 말하지 않았으면 keep")
    other: bool = Field(description="recommend에서 이미 본 것 말고 다른 것을 원할 때 true")
    target: Literal["none", "selected", "candidates", "search"] = Field(description="explain의 대상. 아니면 none")
    candidate_numbers: list[int] = Field(description="explain이 가리킨 후보 번호. 없으면 빈 목록")
    original: bool = Field(description="explain에서 자료 원문을 그대로 보여 달라고 할 때 true")
    route: Literal["dialogue", "reflection"] = Field(description="coach의 경로. 자기 평가 요청이면 reflection")
    transition: Literal["continue", "correct", "new_issue", "resolve", "abandon", "none"] = Field(
        description="coach의 문제 흐름. 해당 없으면 none")
    transition_quote: str = Field(description="전환 근거가 된 현재 발화의 글자 그대로. 없으면 빈 글")
    observation_updates: list[WireObservation] = Field(description="coach의 사용자 보고. 없으면 빈 목록")
    requirement_proposals: list[WireRequirement] = Field(description="coach의 요구 항목. 없으면 빈 목록")
    kind: Literal["none", "greeting", "capabilities", "recommend_reason", "certainty", "out_of_scope",
                  "partial_completion", "unclear", "which_activity", "two_requests"] = Field(
        description="notice·ask_user의 종류. 아니면 none")


class AgentDecision(Contract):
    decision: WireDecision


# 전송 형식의 중립값 중 행동 모델에서 None(없음·변경 없음)으로 읽는 것
_NEUTRAL_AS_NONE = {"move": "none", "step": 0, "max_minutes": 0, "scope": "keep"}
_MODELS = dict(zip(ACTION_NAMES, ACTIONS))


def from_wire(wire: dict) -> dict:
    """평평한 판단을 고른 행동의 필드만 남긴 행동 dict로 줄인다. 모르는 행동은 그대로 돌려 parse_decision이 거르게 한다."""
    model = _MODELS.get(wire.get("action")) if isinstance(wire, dict) else None
    if model is None:
        return wire
    return {name: None if name in _NEUTRAL_AS_NONE and wire[name] == _NEUTRAL_AS_NONE[name] else wire[name]
            for name in model.model_fields if name in wire}


_DECISION = TypeAdapter(Annotated[Action, Field(discriminator="action")])


def parse_decision(raw: object) -> Action | None:
    """형식에 맞는 행동 모델을 돌려준다. 어떤 행동에도 맞지 않으면 예외 없이 None이다."""
    try:
        return _DECISION.validate_python(raw)
    except ValidationError:
        return None
