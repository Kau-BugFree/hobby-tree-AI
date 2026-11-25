"""요청 분류 규칙 표와 판정 함수: 한 문장을 큰 분류와 세부 종류로 정합니다. 세션·저장소·검색기·모델은 건드리지 않습니다."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from .dialogue import INTERRUPT, is_trouble
from .dialogue_state import issue_resolved
from .retrieval import COOKING_WORDS
from .slots import (STEP_NUMBER, candidate_number, other_dishes_pattern, requested_step, selects_candidate,
                    spoken_constraints, step_move)
from .vocabulary import Vocabulary, vocabulary_for
from .turn_router import COACHING_REQUEST, OBSERVATION_SUMMARY, SS_FINAL, coaching_requirements, observation_quotes


# 시작·전환 같은 행동을 하지 말라는 부정 표현.
NEGATED_ACTION = re.compile(r"(하지\s*(?:마|말)|않|안\s*(?:시작|할)|못|취소|생각\s*없)")

COMPLETION = re.compile(r"(완료했|다\s*했|끝냈|끝났|마쳤|완성했)\S*")
NOT_COMPLETION = re.compile(r"(아직|못|안\s*했|않|[?？]|는지|하지\s*(?:마|말)|말아|취소|가정|예시|했다고)")
# 완료 표현 밖에 이런 말만 남으면 활동 전체 완료다.
WHOLE_ACTIVITY_WORDS = {"활동", "요리", "이거", "이것", "그거", "전부", "모두", "전체", "오늘", "방금",
                        "드디어", "지금", "이제", "벌써", "제가", "내가", "저", "나", "다",
                        "감사합니다", "감사해요", "고마워요", "고마워"}

@dataclass(frozen=True)
class ScopePatterns:
    out_of_scope: re.Pattern          # 취미 밖 주제 낱말
    off_topic_complaint: re.Pattern   # 주제 밖 낱말이 주어인 짧은 문제 보고
    stated_limit: re.Pattern          # 말로 밝힌 시간·도구 조건


@lru_cache
def scope_patterns(vocab: Vocabulary) -> ScopePatterns:
    """취미 낱말로 범위·조건 패턴을 한 번 조립합니다. 낱말 묶음(frozen) 값이 캐시 열쇠입니다."""
    out_of_scope = re.compile(rf"({'|'.join(re.escape(word) for word in vocab.out_of_scope)})")
    # '주식이 망했어요', '러닝은 너무 어려워'처럼 주제 밖 낱말이 주어이고 다른 내용이 없는 문제 보고.
    # 선택 활동의 문제가 아니다. '날씨가 습해서 반죽이 안 돼'처럼 다른 내용이 이어지는 말은 여기에 걸리지 않는다.
    complaint = re.compile(
        rf"\s*(?:요즘|오늘|아|나|저|내|제)?\s*{out_of_scope.pattern}(?:이|가|은|는|도)?\s*"
        r"(?:너무|정말|진짜|아주|좀|많이|계속|자꾸|다)?\s*\S+[\s.!~…ㅠㅜ]*"
    )
    # 쓸 수 있는 시간이나 도구를 직접 밝힌 조건. 선택 활동에 대한 물음 속 '10분 끓여도'와 구분한다.
    tools = "".join(f"|{re.escape(tool)}" for tool in vocab.key_tools)
    stated_limit = re.compile(
        rf"\d\s*(?:분|시간)\s*(?:만(?!\s*더)|안에|안으로|내에|내로|이내|이하)|시간\s*없{tools}"
        r"|시간\s*(?:은|는|이)?\s*(?:제한|상관|관계)"
    )
    return ScopePatterns(out_of_scope, complaint, stated_limit)


def _patterns(hobby: str) -> ScopePatterns:
    return scope_patterns(vocabulary_for(hobby))


# 요리의 패턴. 예전 가져오기 경로(from app.intent_rules import OUT_OF_SCOPE)를 유지한다.
OUT_OF_SCOPE = _patterns("cooking").out_of_scope
OFF_TOPIC_COMPLAINT = _patterns("cooking").off_topic_complaint
STATED_LIMIT = _patterns("cooking").stated_limit


def off_topic(text: str, activity: dict | None, *, hobby: str = "cooking") -> bool:
    """취미와 무관한 주제를 말했으면 True를 반환합니다.

    취미 맥락어가 함께 있거나, 그 낱말이 선택 활동 본문에 나오면('주식과 곁들임을 하나씩 골라요') 주제 이탈이 아닙니다.
    맥락어는 주제 밖 낱말을 지운 나머지에서 찾습니다. 요리는 두 목록이 겹치지 않아 지우지 않을 때와 같습니다.
    """
    # ponytail: 맥락어는 검색과 함께 쓰는 COOKING_WORDS 하나다. 다른 취미도 이 목록으로 본다. 취미가 늘면 낱말 묶음으로 옮긴다.
    out_of_scope = _patterns(hobby).out_of_scope
    words = out_of_scope.findall(text)
    if not words or COOKING_WORDS.search(out_of_scope.sub(" ", text)):
        return False
    own = json.dumps(activity, ensure_ascii=False) if activity else ""
    return not any(word in own for word in words)

# '감자로 만들 수 있는 요리'처럼 재료·도구로 요리를 찾는 말. 선택 활동 조회가 아니라 추천이다.
FIND_DISH = re.compile(r"((?:만들|할|해\s*먹을)\s*수\s*있는|뭐\s*(?:만들|해\s*먹))")
# 다른 요리를 분명히 찾는 말. 문제 보고 안에 있어도 추천으로 본다.
DISH_REQUEST = (
    rf"추천|메뉴|뭐\s*있|{FIND_DISH.pattern}|다른\s*(?:거|것|요리|활동|메뉴)|딴\s*거"
    r"|(?:요리|메뉴|활동)\s*(?:있|없)|(?:되는|하는|쉬운|간단한|빠른)\s*(?:거|것|요리)\s*(?:있|없)"
    r"|(?:할|해\s*볼|만들)\s*만한|레시피\s*(?:좀\s*)?(?:알려|보여|찾아|줘|있|없)"
)
CLEARLY_ASKS_FOR_DISHES = re.compile(DISH_REQUEST)
# 다른 요리를 찾는 말. 선택 활동이 있어도 이런 말이 있으면 그 활동에 대한 물음이 아니라 새 추천이다.
# '레시피'·'만한'은 '레시피대로 했는데 망했어', '먹을 만한지 모르겠어'처럼 지금 활동을 말할 때도 쓰이므로
# 문제 보고에서는 CLEARLY_ASKS_FOR_DISHES만 본다.
ASKS_FOR_DISHES = re.compile(rf"(레시피|만한|{DISH_REQUEST})")
# '설탕 대신 다른 거 넣어도 돼?'의 '다른 거'는 다른 요리가 아니라 대체 재료다.
SUBSTITUTE = re.compile(r"대신\s*(?:다른|딴)\s*(?:거|것|걸)|(?:다른|딴)\s*(?:거|것|걸)\S*\s*(?:넣|써|쓰|사용)")


def asks_for_dishes(text: str) -> bool:
    """다른 요리를 찾는 말이면 True를 반환합니다. 대체 재료를 묻는 '다른 거'는 뺍니다."""
    return bool(ASKS_FOR_DISHES.search(SUBSTITUTE.sub(" ", text)))


# 물음표를 생략한 물음도 많다.
# '넣어도 돼', '하면 돼'는 물음이지만 '분리돼'는 상태를 말하는 서술문이다. '다 했나', '익었나'는 스스로 묻는 말이다.
QUESTION_END = re.compile(
    rf"([?？]|(?:도|면|로)\s*돼|되나요?|될까요?|가능해|괜찮아|괜찮을까요?|나요|까요?|어때|[{SS_FINAL}]나)\s*[.!~]*$")
# 활동을 시작하거나 이어가겠다는 말. '녹기 시작해요'는 상태가 변하기 시작했다는 보고이고
# '진행할지 모르겠어요', '진행할 수 있어'는 시작 선언이 아니다. '만들기 시작할게'처럼 하겠다는 어미는 '~기' 뒤에서도 시작이다.
# ponytail: '요리하기 시작해요'처럼 '~기 시작해'로 말한 시작 선언은 상태 보고와 모양이 같아 시작으로 보지 않는다.
START = re.compile(r"다시\s*시작|활동\s*시작|시작(?:할(?:게|래|께)|하자)|(?<!기)(?<!기\s)시작(?:할|해)|진행할(?!지|\s*수)")
# 선택 활동의 단계·준비물·시간을 그대로 읽는 조회 표현.
ACTIVITY_LOOKUP = re.compile(
    rf"(다음\s*단계|이전\s*단계|현재\s*단계|{STEP_NUMBER.pattern}|순서|준비물|재료|도구|몇\s*분|얼마나\s*걸)"
)
# 확인 질문의 답('2단계요', '도구가 없어')과 달리 정보를 직접 달라고 하는 말.
# '눈금이 안 보여'의 '보여'는 요청이 아니다. '보여줘'처럼 달라는 말만 요청으로 본다.
INFO_REQUEST = re.compile(r"(알려|보여\s*(?:줘|주|줄|달)|설명|비교|차이|뭐(?:야|예요|지|였)|[?？]\s*$)")
# 코치 자신의 능력 범위를 묻는 말. '너무'의 '너'는 코치를 부르는 말이 아니다.
CAPABILITY = re.compile(r"(?:^|\s)(?:너|넌|니가|네가|당신)(?:는|은|가)?\s.*수\s*있|(?:어떤|무슨)\s*질문|도와줄\s*수\s*있")
# 자기 수행 평가나 다음 난이도 진입을 판단해 달라는 말. '다음 단계로 넘어가'는 단계 조회다.
SELF_ASSESSMENT = re.compile(
    r"평가해|피드백|잘한\s*점|부족한\s*점|못한\s*점|내\s*실력|(?:난이도|레벨|수준)[^.?!]*넘어가|다음\s*활동\S*\s*넘어가"
)
# 직전 추천의 이유를 묻는 말. '이유식', '20분 기준으로 추천'은 추천 요청이다.
RECOMMEND_REASON = re.compile(r"(?:왜|어째서)[^.?!]*(?:추천|골랐|고른)|추천(?:한|해\s*준)?\s*(?:이유|기준|근거)")
# '매운 게 먹고 싶어'처럼 요리를 찾는 뜻이 드러나는 말. '포기하고 싶어', '설탕을 줄이고 싶어'는 요리를 찾는 말이 아니다.
WANTS_DISH = re.compile(
    r"(?:먹|마시|만들|굽|구워|끓이|끓여|볶아?|튀기|튀겨|요리(?:를)?\s*(?:하|해)|해\s*먹)(?:어|아)?\s*(?:보)?고\s*싶"
    r"|해\s*보고\s*싶|땡겨|당겨|먹을래|먹을까|찾아\s*줘"
)
# 가리키는 대상이 있어야 뜻이 정해지는 말. 선택 활동이 없으면 무엇을 가리키는지 알 수 없다.
DEICTIC = re.compile(r"이거|이것|이건|그거|그것|그건|저거|여기")
# 조사나 정도 부사가 있어 무언가의 상태를 말하는 문장.
STATE_SUBJECT = re.compile(r"[가-힣](?:이|가|을|를|은|는|도)\s+\S|너무|뭔가|자꾸|계속|아직")


def states_condition(text: str) -> bool:
    """모델 없이 쓰는 근사 판정: 조사·부사가 있고 반말 서술어로 끝나면 선택 활동의 상태 보고로 봅니다."""
    # ponytail: 음절 모양만 본다. '오이 파스타'처럼 서술어 모양으로 끝나는 요리 이름은 틀린다.
    # 정확한 분류는 모델 해석(interpret_turn)이 맡고, 이 규칙은 모델이 없을 때만 쓴다.
    last = re.sub(r"[\s.!~…ㅠㅜ]+$", "", text)[-1:]
    # '재료를 알려줘', '정리해 줘'는 서술문이 아니라 요청이다.
    if (not STATE_SUBJECT.search(text) or re.search(r"뭐|뭘|무슨", text)
            or re.search(r"(?:줘|주세요|줄래|주라)[\s.!~]*$", text) or not "가" <= last <= "힣"):
        return False
    code = ord(last) - 0xAC00
    # 요·다·네, 또는 받침 없이 ㅏ·ㅐ·ㅓ·ㅕ·ㅘ·ㅙ·ㅝ로 끝나는 음절(없어·짜·넘쳐·이상해).
    return last in "요다네" or code % 28 == 0 and code // 28 % 21 in {0, 1, 4, 6, 9, 10, 14}


def partial_completion(text: str, activity: dict) -> bool:
    """완료 문장이 활동 전체가 아니라 단계나 일부 작업을 가리키면 True를 반환합니다.

    활동 이름은 제목 전체나 이어진 제목 단어 두 개 이상만 인정한다. '간장', '밥'처럼 한 단어는 재료나 작업일 수 있다.
    """
    rest = "".join(
        stem
        for word in re.findall(r"[가-힣A-Za-z0-9]+", COMPLETION.sub(" ", text))
        if word not in WHOLE_ACTIVITY_WORDS
        and (stem := re.sub(r"(을|를|은|는|이|가|도)$", "", word)) not in WHOLE_ACTIVITY_WORDS
    )
    words = re.findall(r"[가-힣A-Za-z0-9]+", activity["title"])
    names = {"".join(words[i:j]) for i in range(len(words)) for j in range(i + 2, len(words) + 1)}
    return bool(rest) and rest not in names | {"".join(words)}


# 아래는 coach.py의 분기 안에 그대로 적혀 있던 판정 패턴이다.
ALLERGY = re.compile(r"(알레르기|알러지)")
CERTAINTY = re.compile(r"확실해|검토.*됐|무슨\s*근거")
COMPARE = re.compile(r"비교|차이")
# 시작에 관한 물음('시작해도 돼?')이나 다른 요청이 섞인 말은 시작 선언이 아니다.
NOT_START = re.compile(r"(추천|만한|알려|[?？]|해도|해야)")
NOT_LOOKUP = re.compile(r"(추천|비교|차이|설명|쉽게|왜|어떻게|원문)")
LOOKUP_MATERIALS = re.compile(r"(준비물|재료|도구)")
LOOKUP_DURATION = re.compile(r"(몇\s*분|얼마나\s*걸)")
RECORDS = re.compile(r"(기록|얼마나\s*했|완료\s*목록)")
# DialogueCoach.accepts의 문제 관리 낱말과 열린 문제의 후속 표현.
ISSUE_WORDS = re.compile(r"해결됐|해결되었|(?:새|새로운)\s*문제|다른\s*문제|문제.*그만")
ISSUE_CORRECTION = re.compile(r"^(아니|정정|사실)")
ISSUE_FOLLOWUP = re.compile(r"^(그럼|그러면|그래도|이건|그건)|왜|어떻게")
# 열린 문제 중에도 추천·설명 요청이나 인사·맞장구는 문제의 후속이 아니다.
NOT_FOLLOWUP = re.compile(r"추천|레시피|설명|쉽게|비교|차이|뜻|고마|감사|안녕|알겠|오케이|^(응|네|좋아요?)[.! ]*$")
# 인사·감사·맞장구. 낱말마다 원자 묶음으로 둔다. 묶음이 없으면 '안녕안녕…'처럼 긴 말에서 되돌아보기가 폭증한다.
GREETING = re.compile(
    r"(?:(?>안녕\S*|고(?:마|맙)\S*|감사\S*|땡큐|알겠\S*|오케이|좋아요?|응|네)\s*)+[ㅎㅋ.!?~ ]*")
EXPLAIN_WORDS = re.compile(r"(설명|비교|차이|원문)")
# '따뜻한'의 '뜻', '쉽게 만들 수 있는 요리 추천해줘'의 '쉽게'는 설명 요청이 아니다.
EXPLAIN_SOFT_WORDS = re.compile(r"(쉽게|왜|어떻게|(?<!따)뜻)")

PROCESSING = frozenset({"state_change", "lookup", "recommend"})
# 처리 분류여도 맞으면 바로 멈추는 규칙. 단계 범위 오류는 사슬에서 상태 변경보다 앞서 되묻던 안내라
# '99단계 완료했어'를 겹침(None)으로 보내지 않는다. 완료 보고는 사슬에서 조회·추천보다 앞서 받던 말이라
# '재료 손질 다 했어'를 겹침으로 보내지 않는다.
STOPS = frozenset({"step_out_of_range", "complete"})


@dataclass(frozen=True)
class TurnContext:
    selected: dict | None            # 선택 활동(없으면 None)
    candidate_count: int             # 직전 추천 후보 수
    candidate_selection_pending: bool
    pending_question: dict | None    # dialogue.pending_question
    issue_open: bool                 # dialogue.issue_id가 있음
    issue_phase: str | None
    constraints_changed: bool        # 화면에서 조건을 바꿔 보냄(request.constraints)
    hobby_key: str = "cooking"
    interprets_turns: bool = True    # 규칙이 정하지 못한 말을 모델이 해석할 수 있음


@dataclass(frozen=True)
class Intent:
    category: str   # state_change | lookup | recommend | question | reply | notice | overlap(처리 분류 둘 이상)
    kind: str       # 설계 문서 4.1절의 세부 종류 이름
    rule: str       # 맞은 규칙 이름
    slots: dict     # 추출한 값


@dataclass(frozen=True)
class Rule:
    name: str
    category: str | None   # None이면 판정하지 않는다(처리 쪽에서 되묻기).
    kind: str | None
    test: Callable[[str, TurnContext], bool]


# 규칙들이 함께 쓰는 판정 값. coach.py의 _respond_locked가 분기 앞에서 미리 계산하던 값과 같다.

def _follows_up(text: str, context: TurnContext) -> bool:
    """확인 질문 대기 중이거나 열린 문제에 관찰을 덧붙인 말이면 True. 요청·해결·완료 선언은 후속이 아니다."""
    return bool(
        (context.pending_question or context.issue_phase in {"open", "answer_provided"} and observation_quotes(text))
        and not (INFO_REQUEST.search(text) or "추천" in text or issue_resolved(text) or COMPLETION.search(text)))


def _out_of_scope(text: str, context: TurnContext) -> bool:
    """요리와 무관한 주제의 말이면 True. 선택 활동의 문제 보고나 확인 질문의 답에 섞인 말은 주제 이탈이 아니다."""
    if not off_topic(text, context.selected, hobby=context.hobby_key):
        return False
    complaint = _patterns(context.hobby_key).off_topic_complaint
    reports_progress = context.selected and (
        is_trouble(text) and not complaint.fullmatch(text) or _follows_up(text, context))
    return not reports_progress


def _coaching_request(text: str, context: TurnContext) -> bool:
    return bool(COACHING_REQUEST.search(text) and (
        len(coaching_requirements(text)) > 1
        or context.issue_open and not ACTIVITY_LOOKUP.search(text) and not COMPARE.search(text)))


def _lookup_request(text: str, context: TurnContext) -> bool:
    return bool(INFO_REQUEST.search(text) and (ACTIVITY_LOOKUP.search(text) or COMPARE.search(text))
                and not is_trouble(text) and not _coaching_request(text, context))


def wanted_step(text: str) -> int | None:
    """말한 단계 번호. '5단계 넘는 건 빼고 추천'의 단계는 선택 활동의 단계 번호가 아니다."""
    return None if "추천" in text else requested_step(text)


def _spoken(text: str, context: TurnContext) -> dict:
    """말로 한 시간·도구 조건. 세션과 합치는 일은 처리 함수가 한다."""
    return spoken_constraints(text, {}, context.selected is None or asks_for_dishes(text), hobby=context.hobby_key)


def _states_conditions(text: str, context: TurnContext) -> bool:
    """시간·도구 조건만 밝혔거나 화면에서 조건을 바꿨으면 True. 요리를 찾는다는 말은 없어도 된다."""
    return bool(_spoken(text, context) or context.constraints_changed
                or _patterns(context.hobby_key).stated_limit.search(text) or WANTS_DISH.search(text))


def _seeks_dishes(text: str, context: TurnContext) -> bool:
    """요리를 찾거나 조건을 밝힌 말이면 True. 지금 추천 분기로 가는 조건과 같다."""
    return asks_for_dishes(text) or _states_conditions(text, context)


def _names_candidate(text: str, context: TurnContext) -> bool:
    """후보가 있을 때 후보 번호를 말했으면 True. '2번으로 하고 바로 시작할게'는 어느 활동인지 정할 수 없다."""
    return context.candidate_count > 0 and candidate_number(text) is not None


# 판정 함수. 각 함수는 coach.py의 같은 자리 분기 조건이다.

def _step_out_of_range(text: str, context: TurnContext) -> bool:
    wanted = wanted_step(text)
    return wanted is not None and (not context.selected or not 1 <= wanted <= len(context.selected["steps"]))


def _pending_reply(text: str, context: TurnContext) -> bool:
    # 확인 질문 대기 중에도 '준비물 알려줘', '된장찌개 레시피 알려줘'처럼 활동 정보나 다른 요리를 직접 요청하거나
    # 시작·전환을 말하면 답이 아니다.
    return bool(context.selected and context.pending_question and not _out_of_scope(text, context)
                and not INTERRUPT.search(text) and not _lookup_request(text, context)
                and not (selects_candidate(text) and context.candidate_selection_pending)
                and not (INFO_REQUEST.search(text) and (ACTIVITY_LOOKUP.search(text) or asks_for_dishes(text))))


def _select(text: str, context: TurnContext) -> bool:
    return selects_candidate(text)


def _complete(text: str, context: TurnContext) -> bool:
    # 완료 기록은 되돌릴 수 없다. '다 끝났나요' 같은 물음이나 '다 했는데 안 돼요' 같은 문제 보고로는 만들지 않는다.
    # 문제 표현이 완료 표현 뒤에 올 때만 문제 보고다. '어려웠는데 완성했어요'는 완료를 말한 것이다.
    # '러닝 끝냈어'는 선택해 둔 요리 활동의 완료가 아니므로 범위 밖 안내로 보낸다.
    done = COMPLETION.search(text)
    return bool(done and not NOT_COMPLETION.search(text) and not QUESTION_END.search(text)
                and not is_trouble(text[done.end():]) and not _out_of_scope(text, context))


def _start(text: str, context: TurnContext) -> bool:
    # '지금 시작해도 돼?'처럼 시작에 관한 물음이나 '시작할 때부터 막혔어요' 같은 문제 보고는 실행을 만들지 않는다.
    # 문제 표현이 시작 표현 뒤에 올 때만 문제 보고다. '어렵지만 시작할게요'는 시작하겠다는 말이다.
    begin = START.search(text)
    return bool(begin and not NOT_START.search(text) and not QUESTION_END.search(text)
                and not is_trouble(text[begin.end():]) and not _names_candidate(text, context))


def _decline(text: str, context: TurnContext) -> bool:
    # '활동 시작하지 마', '활동 시작 취소'처럼 전환을 부정하는 말.
    return _start(text, context) and bool(NEGATED_ACTION.search(text))


def _trouble(text: str, context: TurnContext) -> bool:
    # 문제 보고 속의 '레시피대로', '먹을 만한지'는 다른 요리를 찾는 말이 아니다. 추천은 분명히 요청했을 때만 한다.
    return bool(context.selected and not _out_of_scope(text, context)
                and not CLEARLY_ASKS_FOR_DISHES.search(SUBSTITUTE.sub(" ", text)))


def _trouble_report(text: str, context: TurnContext) -> bool:
    return _trouble(text, context) and is_trouble(text)


def _trouble_question(text: str, context: TurnContext) -> bool:
    return _trouble(text, context) and _coaching_request(text, context)


def _lookup(text: str, context: TurnContext) -> bool:
    return _lookup_text(text, bool(context.selected))


@lru_cache(maxsize=64)
def _lookup_text(text: str, selected: bool) -> bool:
    """조회 규칙 셋이 함께 쓰는 판정. 문장과 선택 여부로만 정해지므로 한 번 계산해 둔다."""
    # '3단계에서 설탕이 안 녹아', '재료를 다 넣었어'처럼 상태를 말하는 문장은 단계·준비물 조회가 아니다.
    return bool(ACTIVITY_LOOKUP.search(text) and not NOT_LOOKUP.search(text) and not FIND_DISH.search(text)
                and not (selected and (observation_quotes(text) or states_condition(text))))


def _lookup_materials(text: str, context: TurnContext) -> bool:
    return _lookup(text, context) and bool(LOOKUP_MATERIALS.search(text))


def _lookup_duration(text: str, context: TurnContext) -> bool:
    return _lookup(text, context) and bool(LOOKUP_DURATION.search(text))


def _records(text: str, context: TurnContext) -> bool:
    return bool(RECORDS.search(text))


def _accepted(text: str, context: TurnContext) -> bool:
    """DialogueCoach.accepts 분기의 공통 조건: 다른 요리를 찾거나 활동 정보를 묻는 말은 넘기지 않는다."""
    return not asks_for_dishes(text) and not _lookup_request(text, context)


def _accepted_issue_control(text: str, context: TurnContext) -> bool:
    return _accepted(text, context) and bool(
        ISSUE_WORDS.search(text) or context.issue_open and ISSUE_CORRECTION.search(text))


def _accepted_report(text: str, context: TurnContext) -> bool:
    return _accepted(text, context) and bool(
        is_trouble(text) or context.selected and (observation_quotes(text) or OBSERVATION_SUMMARY.search(text)))


def _accepted_question(text: str, context: TurnContext) -> bool:
    return _accepted(text, context) and bool(
        context.pending_question and context.selected or context.issue_open and ISSUE_FOLLOWUP.search(text))


def _issue_followup(text: str, context: TurnContext) -> bool:
    # accepts의 마지막 절: 열린 문제의 그 밖의 후속 말. 모델 없이는 요리 이름과 후속을 가릴 수 없어 맞추지 않고
    # 뒤의 규칙에 맡긴다. 모델이 없을 때의 근사(states_condition)는 쓰지 않는다(설계 6절 5번).
    return context.interprets_turns and _accepted(text, context) and bool(
        context.issue_open and context.issue_phase in {"open", "answer_provided"}
        and not NOT_FOLLOWUP.search(text))


def deictic_without_activity(text: str, context: TurnContext) -> bool:
    # 선택 활동 없이 '이거 어떻게 해?'라고 하면 가리키는 활동을 알 수 없다. 검색 1위 활동으로 추측하지 않는다.
    # '나 이거 먹고 싶어', '그거 10분 안에 돼?'처럼 요리를 찾거나 조건을 밝힌 말은 추천으로 둔다.
    return bool(not context.selected and DEICTIC.search(text) and candidate_number(text) is None
                and not _seeks_dishes(text, context))


def _explain(text: str, context: TurnContext) -> bool:
    # 선택 활동이 있으면 다른 요리를 찾거나 조건을 밝히는 말이 없는 물음은 그 활동에 대한 질문이다.
    # 추천을 말했으면 추천이 먼저다.
    return bool(EXPLAIN_WORDS.search(text) or EXPLAIN_SOFT_WORDS.search(text) and "추천" not in text or (
        context.selected and (QUESTION_END.search(text) or INFO_REQUEST.search(text))
        and not asks_for_dishes(text) and not _patterns(context.hobby_key).stated_limit.search(text)))


def _search(pattern: re.Pattern) -> Callable[[str, TurnContext], bool]:
    return lambda text, context: bool(pattern.search(text))


# 규칙 표. 순서는 coach.py 분기 순서다. 처리 분류(상태 변경·조회·추천)의 규칙은 앞선 다른 분류 규칙이
# 맞으면 탈락한다(그 분기가 먼저 받던 말이다). 처리 분류끼리는 앞뒤 없이 함께 평가한다(STOPS의 규칙은 맞으면 바로 멈춘다).
RULES = (
    Rule("allergy", "notice", "out_of_scope", _search(ALLERGY)),
    Rule("capability", "notice", "capabilities", _search(CAPABILITY)),
    Rule("issue_resolved", "question", "issue_control",
         lambda text, context: bool(context.selected and not _out_of_scope(text, context) and issue_resolved(text))),
    Rule("self_assessment", "question", "issue_control", _search(SELF_ASSESSMENT)),
    Rule("recommend_reason", "notice", "recommend_reason", _search(RECOMMEND_REASON)),
    Rule("certainty", "notice", "certainty", _search(CERTAINTY)),
    Rule("step_out_of_range", "lookup", "steps", _step_out_of_range),
    Rule("pending_reply", "reply", "reply", _pending_reply),
    Rule("select", "state_change", "select", _select),
    Rule("complete", "state_change", "complete", _complete),
    Rule("decline", "state_change", "decline", _decline),
    Rule("start", "state_change", "start", _start),
    Rule("trouble_report", "question", "problem", _trouble_report),
    Rule("trouble_question", "question", "ask", _trouble_question),
    Rule("lookup_materials", "lookup", "materials", _lookup_materials),
    Rule("lookup_duration", "lookup", "duration", _lookup_duration),
    Rule("lookup_steps", "lookup", "steps", _lookup),
    Rule("records", "lookup", "records", _records),
    Rule("out_of_scope", "notice", "out_of_scope", _out_of_scope),
    Rule("issue_control", "question", "issue_control", _accepted_issue_control),
    Rule("observation", "question", "problem", _accepted_report),
    Rule("issue_question", "question", "ask", _accepted_question),
    Rule("issue_followup", "question", "ask", _issue_followup),
    Rule("greeting", "notice", "greeting", lambda text, context: bool(GREETING.fullmatch(text))),
    Rule("deictic_without_activity", None, None, deictic_without_activity),
    Rule("explain", "question", "explain", _explain),
    # 겹침을 따지는 추천은 다른 요리를 분명히 찾는 말만이다. '레시피 재료 알려줘'의 '레시피'는 조회와 겨루지 않는다.
    Rule("recommend_request", "recommend", "recommend",
         lambda text, context: bool(CLEARLY_ASKS_FOR_DISHES.search(SUBSTITUTE.sub(" ", text)))),
)
# 조건만 밝힌 말('오븐 없어')이나 '레시피'·'만한'만 있는 말은 아무 규칙도 안 맞았을 때만 추천이다. 다른 분류와 겨루지 않는다.
CONDITION_ONLY = Rule("recommend_condition", "recommend", "recommend", _seeks_dishes)
# 처리 분류 둘 이상에 걸린 말. 한 번에 하나씩 말해 달라고 되묻는다(설계 6-1의 Claude 해석 대신).
OVERLAP = Intent(category="overlap", kind="overlap", rule="overlap", slots={})


def _slots(kind: str, text: str, context: TurnContext) -> dict:
    """판정 뒤에 세부 종류에 필요한 값을 뽑습니다."""
    if kind == "select":
        return {"number": candidate_number(text)}
    if kind == "steps":
        return {"step": wanted_step(text), "move": step_move(text)}
    if kind == "recommend":
        other = other_dishes_pattern(vocabulary_for(context.hobby_key)).search(text)
        return {"constraints": _spoken(text, context), "other": bool(other)}
    if kind == "complete":
        return {"partial": bool(context.selected) and partial_completion(text, context.selected)}
    return {}


def classify(text: str, context: TurnContext) -> Intent | None:
    """한 문장의 요청 종류를 정합니다. 어느 규칙에도 맞지 않으면 None을 반환합니다.

    처리 분류(상태 변경·조회·분명한 추천 요청) 둘 이상에 걸리면 겹침(OVERLAP, 서비스 단계와 관계없이 되묻는다)을 반환합니다.
    STOPS의 규칙(단계 범위 오류, 완료 보고)은 처리 분류여도 맞으면 겹침을 따지지 않고 그 규칙을 반환합니다.
    """
    vocabulary_for(context.hobby_key)   # 없는 취미는 어느 규칙이 낱말을 쓰든 말든 KeyError로 멈춘다.
    handled: dict[str, Rule] = {}
    other = None
    for rule in RULES:
        if not rule.test(text, context):
            continue
        if rule.category in PROCESSING and rule.name not in STOPS:
            handled.setdefault(rule.category, rule)
            continue
        # 처리 분류가 아닌 규칙이 맞으면 그 뒤의 규칙은 지금 분기 순서에서 닿지 않는다.
        # 처리 규칙 둘 사이에서 맞아도 여기서 멈추므로 겹침 검사는 이 규칙 앞의 처리 규칙만 본다. 지금 체인과 같은 결과다.
        other = rule
        break
    if len(handled) > 1:
        return OVERLAP
    chosen = next(iter(handled.values()), other)
    if chosen is None and CONDITION_ONLY.test(text, context):
        chosen = CONDITION_ONLY
    if chosen is None or chosen.category is None:
        return None
    return Intent(chosen.category, chosen.kind, chosen.name, _slots(chosen.kind, text, context))
