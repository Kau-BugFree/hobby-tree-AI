"""질문 유형별 처리기: 세션/조회 → 필요한 검색 → 조건부 설명 생성 순서로 처리합니다."""

import asyncio
import hashlib
import json
import re
import time
import uuid
import weakref
from .store import StoreError
from .dialogue import DialogueCoach, INTERRUPT, is_trouble
from .dialogue_models import new_dialogue, TurnInterpretation
from .dialogue_state import archive_issue, normalize_dialogue, build_issue_context, serialize_dialogue, issue_resolved, UNKNOWN
from .turn_router import COACHING_REQUEST, SS_FINAL, coaching_requirements, observation_quotes
from .turn_budget import TurnBudget, BudgetExceeded
from .metacognition import recommendation_reason, build_metacognition, certainty_text
from .answer_policy import AnswerPolicy, evidence_for
from .retrieval import COOKING_WORDS, outside_cooking, query_terms

# '세 번째 단계'처럼 단계를 한글 서수로 말한 경우의 번호다.
KOREAN_ORDINALS = {"첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
STEP_NUMBER = re.compile(r"(\d+)\s*(?:번째|번)?\s*단계|(첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*번째\s*단계")


def requested_step(text):
    """'3단계', '세 번째 단계'처럼 단계 번호를 직접 말했으면 그 번호를, 아니면 None을 반환합니다."""
    match = STEP_NUMBER.search(text)
    if not match:
        return None
    return int(match[1]) if match[1] else KOREAN_ORDINALS[match[2]]


# 후보 선택 서수. 뒤에 단계·재료·과정이 오거나 '1번 더', '1번만', '2번 정도'처럼 횟수를 말하면 후보 선택이 아니다.
ORDINAL = re.compile(
    r"(?:(첫|두|세|네)\s*번째|(\d+)\s*번(?:째)?(?!째))(?!\s*(?:단계|재료|준비물|도구|과정|순서|더|만|정도|씩))"
)
# 서수 바로 뒤에 붙은 선택 표현. '2번 뒤집어도 좋아?', '3번 섞으면 거품이 보여'처럼 사이에 다른 동작이 끼면
# 그 번호는 횟수다. 잘못 고르면 선택 활동이 바뀌고 열린 문제가 보관되므로 서수에 붙은 말만 선택으로 본다.
# '2번 걸로 …'처럼 후보를 가리키는 '거·것'이 붙으면 뒤의 말과 관계없이 선택이다.
# ponytail: '2번 간장 드레싱으로 할게'처럼 서수와 선택 표현 사이에 요리 이름을 넣은 말은 못 잡는다.
# 그런 말은 선택되지 않을 뿐 상태를 잘못 바꾸지는 않는다. 필요하면 후보 제목과 대조한다.
SELECTS_ORDINAL = re.compile(
    r"\s*(?:그|이)?(?:거|것|걸|꺼)\s*(?:으로|로)"
    r"|\s*(?:(?:그|이)?(?:거|것|걸|꺼)|활동|요리|메뉴|레시피)?\s*(?:이|가|을|를|은|는|도|으로|로)?\s*(?:더|제일|가장)?\s*"
    r"(?:선택|고르|골라|정했|할게|할래|하자|하겠|하고\s*싶|해\s*(?:줘|주세요|줄래|볼게|볼래|보자)"
    r"|갈게|갈래|가자|부탁|주세요|좋아|좋겠|보여\s*(?:줘|주|줄|달))"
)
# 서수로 끝나는 말('좋아 2번') 앞에 있으면 선택으로 보는 표현.
SELECT_WORDS = re.compile(r"(선택|할게|할래|하자|걸로|보여|좋아)")
# 서수가 직전 추천의 후보를 가리키는 말('2번 설명해줘', '두 번째 거 어때?', '2번은 어떤 요리야?').
# '2번 뒤집어도 좋아?'처럼 동작이 바로 이어지면 횟수이고, 그때는 선택한 활동에 대한 물음이다.
NAMES_CANDIDATE = re.compile(
    r"\s*(?:(?:그|이)?(?:거|것|걸|꺼|건|게)|활동|요리|메뉴|레시피|후보|(?:이|가|은|는|을|를|도)(?:\s|$)"
    r"|이랑|랑|하고|설명|어때|어떤|뭐|알려|자세히|$)"
)
# 서수 말고 '거요', '으로 해줘' 같은 말만 남으면 번호만 말한 선택이다.
ORDINAL_ONLY = re.compile(r"(?:거|것|걸|꺼|으로|로|이요|요|해줘|\s)*[.!~]*")

# 시작·전환 같은 행동을 하지 말라는 부정 표현.
NEGATED_ACTION = re.compile(r"(하지\s*(?:마|말)|않|안\s*(?:시작|할)|못|취소|생각\s*없)")

COMPLETION = re.compile(r"(완료했|다\s*했|끝냈|끝났|마쳤|완성했)\S*")
NOT_COMPLETION = re.compile(r"(아직|못|안\s*했|않|[?？]|는지|하지\s*(?:마|말)|말아|취소|가정|예시|했다고)")
# 완료 표현 밖에 이런 말만 남으면 활동 전체 완료다.
WHOLE_ACTIVITY_WORDS = {"활동", "요리", "이거", "이것", "그거", "전부", "모두", "전체", "오늘", "방금",
                        "드디어", "지금", "이제", "벌써", "제가", "내가", "저", "나", "다",
                        "감사합니다", "감사해요", "고마워요", "고마워"}

# 요리 맥락을 나타내는 말(COOKING_WORDS)이 함께 있으면 범위 밖으로 보지 않는다. 그 목록은 검색과 함께 쓴다.
OUT_OF_SCOPE = re.compile(r"(날씨|주식|정치|러닝|달리기|커피|독서)")
# '주식이 망했어요', '러닝은 너무 어려워'처럼 주제 밖 낱말이 주어이고 다른 내용이 없는 문제 보고.
# 선택 활동의 문제가 아니다. '날씨가 습해서 반죽이 안 돼'처럼 다른 내용이 이어지는 말은 여기에 걸리지 않는다.
OFF_TOPIC_COMPLAINT = re.compile(
    rf"\s*(?:요즘|오늘|아|나|저|내|제)?\s*{OUT_OF_SCOPE.pattern}(?:이|가|은|는|도)?\s*"
    r"(?:너무|정말|진짜|아주|좀|많이|계속|자꾸|다)?\s*\S+[\s.!~…ㅠㅜ]*"
)


def off_topic(text, activity):
    """요리와 무관한 주제를 말했으면 True를 반환합니다.

    요리 맥락어가 함께 있거나, 그 낱말이 선택 활동 본문에 나오면('주식과 곁들임을 하나씩 골라요') 주제 이탈이 아닙니다.
    """
    words = OUT_OF_SCOPE.findall(text)
    if not words or COOKING_WORDS.search(text):
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


def asks_for_dishes(text):
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
# 쓸 수 있는 시간이나 도구를 직접 밝힌 조건. 선택 활동에 대한 물음 속 '10분 끓여도'와 구분한다.
STATED_LIMIT = re.compile(
    r"\d\s*(?:분|시간)\s*(?:만(?!\s*더)|안에|안으로|내에|내로|이내|이하)|시간\s*없|오븐"
    r"|시간\s*(?:은|는|이)?\s*(?:제한|상관|관계)"
)
# 선택 활동을 가리키는 말. 없으면 질문이 다른 활동을 말하는지 검색으로 확인한다.
CURRENT_CONTEXT = re.compile(r"(이거|이것|그거|지금|현재|단계|여기|방금|이\s*(?:활동|요리|부분))")
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
CAPABILITIES_TEXT = (
    "저는 요리 활동코치예요. 시간·도구 조건에 맞는 요리 활동 추천, 활동 선택과 준비물·단계·소요 시간 안내, "
    "단계 설명, 막힌 부분에 대한 확인 질문과 활동 원문 근거 안내, 활동 시작·완료 기록을 도와요. "
    "알레르기 판정, 검토된 자료 없이 실패 원인을 확정하는 일, 근거에 없는 수량·시간을 만드는 일, 요리 외 주제는 다루지 않아요."
)
OUT_OF_SCOPE_TEXT = "현재는 요리 활동을 지원합니다. 오늘 가능한 시간이나 하고 싶은 요리를 알려주세요."
# '매운 게 먹고 싶어'처럼 요리를 찾는 뜻이 드러나는 말. '포기하고 싶어', '설탕을 줄이고 싶어'는 요리를 찾는 말이 아니다.
WANTS_DISH = re.compile(
    r"(?:먹|마시|만들|굽|구워|끓이|끓여|볶아?|튀기|튀겨|요리(?:를)?\s*(?:하|해)|해\s*먹)(?:어|아)?\s*(?:보)?고\s*싶"
    r"|해\s*보고\s*싶|땡겨|당겨|먹을래|먹을까|찾아\s*줘"
)
# 가리키는 대상이 있어야 뜻이 정해지는 말. 선택 활동이 없으면 무엇을 가리키는지 알 수 없다.
DEICTIC = re.compile(r"이거|이것|이건|그거|그것|그건|저거|여기")
# 조사나 정도 부사가 있어 무언가의 상태를 말하는 문장.
STATE_SUBJECT = re.compile(r"[가-힣](?:이|가|을|를|은|는|도)\s+\S|너무|뭔가|자꾸|계속|아직")
# 뜻을 정할 수 없을 때는 추천이나 문제 기록으로 추측하지 않고 되묻는다.
UNCLEAR_SELECTED = (
    "어떤 도움이 필요한지 조금 더 알려주세요. 지금 활동에서 막힌 부분이라면 어느 단계에서 어떤 상태인지, "
    "다른 요리를 찾는다면 원하는 요리나 조건을 적어 주세요."
)
UNCLEAR_UNSELECTED = (
    "어떤 도움이 필요한지 조금 더 알려주세요. 요리를 추천받으려면 원하는 요리나 가능한 시간을, "
    "진행 중인 요리의 문제라면 어떤 활동인지 먼저 알려주세요."
)


def states_condition(text):
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


# '냉장고 안에 30분'의 '안에'는 장소이므로 시간 숫자 바로 뒤의 '안에'만 시간 조건으로 본다.
TIME_LIMIT = re.compile(r"(오늘|가능|이내|이하|추천|만\s*할|\d\s*(?:분|시간)\s*(?:만|안에|안으로|내에|내로))")
TIME_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*시간(?:\s*(\d+)\s*분)?|(\d+(?:\.\d+)?)\s*분")
# '30분 이상', '3분만 더'는 쓸 수 있는 시간의 상한이 아니다.
NOT_A_LIMIT = re.compile(r"(이상|넘게|\d\s*분\s*(?:만\s*)?더)")
# '시간 없어서'는 시간이 부족하다는 뜻이다. 명시적 해제 표현만 해제로 본다.
NO_TIME_LIMIT = re.compile(r"시간\s*(?:은|는|이)?\s*(?:제한\s*(?:은|이)?\s*(?:없|해제|풀)|상관\s*없|관계\s*없)")
# 없다고 말한 도구. 뒤의 어미까지 잡아 검색 주제에서 통째로 뺀다. 한 글자 도구는 '전체가 없어'처럼
# 다른 낱말 끝에 걸리지 않게 앞 글자를 본다.
# ponytail: 자주 쓰는 도구만 적었다. '식칼'·'가스레인지' 같은 말은 못 잡는다. 필요하면 카탈로그 도구 사전으로 바꾼다.
NO_TOOL = re.compile(
    r"(오븐|전자레인지|프라이팬|후라이팬|냄비|도마|저울|계량스푼|계량컵|찜기|밀대|거품기|핸드믹서|온도계|뒤집개|집게"
    r"|(?<![가-힣])(?:칼|체|팬))\s*(?:이|가|은|는|도)?\s*(?:없|못\s*써|안\s*써)\S*"
)
# 이미 본 후보 말고 다른 것을 찾는 말. 뒤의 조사까지 잡아 검색 주제에서 뺀다.
OTHER_DISHES = re.compile(r"(?:다른|딴)\s*(?:거|것|걸|건|게|요리|활동|메뉴|레시피|음식|종류)(?:으로|로|을|를|은|는|도)?")
# '다른 거 추천해 주세요', '다른 거 보여줘'에서 주제가 아닌 채 남는 말. 한 글자 낱말(좀·또·해·요)은 길이로 거른다.
REQUEST_FILLER = {"주세요", "줄래", "줄래요", "주라", "보여", "보여줘", "보여줘요", "보여주세요", "하나", "부탁", "부탁해", "부탁해요",
                  "없나", "없나요", "없을까", "없을까요", "있나", "있나요", "있을까", "있을까요", "추천해"}
# 주제가 맞는 활동이 없어도 입문 목록을 대신 보여 줄 만큼 추천을 바라는 말.
WANTS_SUGGESTION = re.compile(r"추천|뭐부터|뭐\s*하지|뭘\s*할|할\s*만한|해볼\s*만한")


def names_topic(text):
    """요청 표현을 빼고도 검색할 주제어가 남으면 True를 반환합니다."""
    return any(len(word) > 1 and word not in REQUEST_FILLER for word in query_terms(text, "activities")[0].split())


def spoken_constraints(text, constraints, finding_dishes=True):
    """문장의 시간·도구 조건을 세션 조건에 덮어쓸 값으로 반환합니다. 세션은 바꾸지 않고, 값 검증은 반영할 때 합니다.

    오븐 말고 다른 도구는 요리를 찾는 말일 때만 조건으로 봅니다. 활동 중의 '계량스푼이 없어'는 그 활동의 문제 보고입니다.
    """
    updates = {}
    # '1시간 30분'은 시간과 분을 더한다. 첫 숫자만 읽으면 60분이 된다.
    match = TIME_AMOUNT.search(text)
    if match and TIME_LIMIT.search(text) and not NOT_A_LIMIT.search(text):
        updates["max_minutes"] = float(match[1]) * 60 + float(match[2] or 0) if match[1] else float(match[3])
    elif NO_TIME_LIMIT.search(text):
        updates["max_minutes"] = None
    missing = [tool for tool in NO_TOOL.findall(text) if tool == "오븐" or finding_dishes]
    if missing:
        excluded = constraints.get("excluded_tools") or []
        updates["excluded_tools"] = list(dict.fromkeys([*excluded, *missing]))
    return updates


def apply_constraints(constraints, updates):
    """말로 한 조건을 세션 조건에 반영합니다. 범위 밖 시간은 반영하지 않고 거부합니다."""
    minutes = updates.get("max_minutes")
    if minutes is not None and not 0 < minutes <= 43200:
        raise StoreError(422, "가용 시간은 0보다 크고 30일 이하여야 합니다.")
    constraints.update(updates)


def partial_completion(text, activity):
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


def globally_unique(request_id):
    """request_id가 무작위(v4) UUID의 표준 표기이면 True를 반환합니다.

    세션 없는 첫 요청의 세션을 다시 찾는 열쇠로 쓰는 값이라, 여러 클라이언트 사이에서 겹치지 않고 추측할 수도 없어야 합니다.
    nil UUID, 시각 기반(v1), 대문자나 하이픈 없는 표기는 받지 않습니다.
    """
    try:
        parsed = uuid.UUID(request_id or "")
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == request_id


def names_activity(text, activity):
    """질문에 활동 제목 전체가 들어 있으면 True를 반환합니다. 띄어쓰기는 무시합니다."""
    # ponytail: 제목 전체 일치만 본다. 긴 제목을 줄여 부르면 전환하지 않는다. 필요하면 리랭커 점수 비교로 바꾼다.
    return activity["title"].replace(" ", "") in text.replace(" ", "")




class Coach:
    def __init__(self, store, retrieval, llm):
        """저장소·검색기·생성기를 주입하여 외부 API 없이도 각 처리 경로를 검증합니다."""
        self.store = store
        self.retrieval = retrieval
        self.llm = llm
        self.dialogue = DialogueCoach(retrieval, llm)
        # 진행 중인 요청이 잡고 있는 동안만 남는 세션별 잠금. 끝난 세션의 잠금은 자동으로 사라진다.
        self.locks = weakref.WeakValueDictionary()

    def activity(self, aid):
        """내부 ID 또는 기존 앱의 별칭으로 활동을 찾고, 없는 ID는 명시적으로 거부합니다."""
        item = self.retrieval.get(aid)
        if not item:
            raise StoreError(404, "활동을 찾을 수 없습니다. 다른 활동을 선택해 주세요.")
        return item

    def select(self, session, aid):
        """이전 후보의 순서를 유지하면서 명시적으로 선택한 활동의 맥락을 변경합니다."""
        item = self.activity(aid)
        if item["hobby_key"] != session["hobby_key"]:
            raise StoreError(422, "현재 대화의 취미와 다른 활동입니다.")
        if session["selected_activity_id"] != item["activity_id"]:
            archive_issue(session)
            session['context_after_message_id'] = (session['messages'][-1].get('message_id')
                                                   if session.get('messages') else None)
            session.update(
                selected_activity_id=item["activity_id"],
                current_step=1,
                execution_id=None,
                dialogue=new_dialogue(),
            )
        session["candidate_selection_pending"] = False
        return item

    @staticmethod
    def attach_execution(session, execution):
        """시작한 새 수행을 현재 실행으로 정합니다. 1단계부터 시작하고 이전 수행의 확인 질문·관찰은 넘기지 않습니다."""
        archive_issue(session)
        session.update(execution_id=execution["execution_id"], current_step=1)

    def ensure_execution(self, session, activity, request_id, force_new=False):
        """진행하던 실행을 재사용합니다. 명시적인 다시 시작만 새 수행 건을 만듭니다."""
        if session["execution_id"] and not force_new:
            current = self.store.get_execution(session["execution_id"])
            if current["activity_id"] == activity["activity_id"]:
                return current
        current, _ = self.store.start_execution(session["session_id"], activity, request_id)
        session["execution_id"] = current["execution_id"]
        return current

    async def _candidates(self, session, query, scope, other, may_fall_back, budget):
        """추천 후보를 찾아 (후보, 검색 결과, 입문 목록으로 대신했는지)를 반환합니다.

        주제 없이 둘러볼 때는 이 대화에서 완료한 활동을, '다른 거'를 찾을 때는 이미 보여 준 활동까지 뺍니다.
        주제를 말한 검색은 완료한 활동도 그대로 보여 줍니다. 완료 기록이 세션 단위라 다른 대화의 기록은 모릅니다.
        """
        # 저장소·색인 조회는 스레드에서 읽는다. 한 사용자의 조회가 다른 사용자의 요청까지 멈추지 않게 한다.
        records = await asyncio.to_thread(self.store.records, session["session_id"])
        done = {record["activity_id"] for record in records}
        seen = set(session.get("shown_ids", []))
        # ponytail: 검색 상한이 100건이라 뺄 활동이 97개를 넘으면 남은 활동을 다 채우지 못한다. 카탈로그 162개에서는 드물다.
        limit = 3 + len(done | seen)

        def search(text):
            return asyncio.to_thread(
                self.retrieval.search, text, session["hobby_key"], session["constraints"], limit, scope)

        budget.consume('retrieval')
        found, starter = await search(query), False
        # 추천을 바랐는데 주제가 맞는 활동이 없으면 빈손으로 두지 않고, 조건에 맞는 활동을 짧은 순서로 보여 준다.
        # 이번 턴의 검색 횟수를 다 썼으면 대신 보여 주지 않는다. 한도 초과로 방금 찾은 결과까지 버리지 않기 위해서다.
        if (not found["items"] and may_fall_back and scope == "activities"
                and found["diagnostics"].get("eligible_documents")
                and budget.counts['retrieval'] < budget.limits['retrieval']):
            budget.consume('retrieval')
            found, starter = await search(""), True
        browsing = found["diagnostics"]["search_mode"] == "catalog_browse"
        skip = (seen if other else set()) | (done if other or browsing else set())
        return [item for item in found["items"] if item["activity_id"] not in skip][:3], found, starter

    async def _unruled(self, text, session, response, message_id, selected, budget, problem_step=None):
        """어느 규칙에도 걸리지 않은 말의 의도를 정합니다. False면 추천 검색으로 넘깁니다.

        모델이 있으면 대화 처리기의 라우터가 모델 해석을 한 번 호출하고, 추천·범위 밖·불명확은
        response['handoff']로 돌려줍니다. 모델이 없으면 선택 활동의 상태 보고로 보이는 문장만 문제로 받습니다.
        """
        if self.dialogue.interprets_turns():
            await self.dialogue.respond(text, session, response, message_id, problem_step=problem_step, budget=budget)
            return True
        if not selected or not states_condition(text):
            return False
        report = TurnInterpretation(
            route="dialogue", transition="continue", transition_quote=text,
            observation_updates=[{"quote": text, "status": "unknown" if UNKNOWN.search(text) else "reported"}],
        )
        await self.dialogue.respond(text, session, response, message_id, problem_step=problem_step,
                                    interpretation=report, budget=budget)
        return True

    async def respond(self, request):
        """같은 세션의 요청을 순서대로 실행하고 재전송은 캐시된 결과를 반환합니다.

        로컬 시연은 단일 서버 프로세스로 실행합니다. 기록 중복은 별도로 DB UNIQUE
        제약을 사용하므로 재전송과 프로세스 재시작 후에도 완료 수가 늘어나지 않습니다.
        """
        if request.session_id:
            # 저장소 호출은 스레드에서 실행한다. SQLite 잠금 대기가 다른 사용자의 요청까지 멈추지 않게 한다.
            session = await asyncio.to_thread(self.store.get_session, request.session_id)
        else:
            # 같은 request_id의 재전송이면 그 요청이 만든 세션을 다시 쓴다. 새 세션을 또 만들지 않는다.
            # 무작위 UUID일 때만 찾는다. 'demo-1'이나 '1' 같은 값은 다른 사용자의 새 대화와 겹치고,
            # 겹치면 남의 대화에 이어 붙거나 첫 메시지가 409로 거부된다.
            # 찾기와 만들기는 저장소의 한 트랜잭션이라 같은 요청이 동시에 와도 세션이 하나다.
            if globally_unique(request.request_id):
                session = await asyncio.to_thread(self.store.session_for_origin, request.request_id)
            else:
                session = await asyncio.to_thread(self.store.create_session)
        sid = session["session_id"]
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            session = await asyncio.to_thread(self.store.get_session, sid)
            # 보낸 필드만으로 지문을 만든다. 조건을 생략한 요청(유지)과 null로 보낸 요청(해제)은 뜻이 다르다.
            payload = request.model_dump(exclude={"request_id"}, exclude_unset=True)
            fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            cached = await asyncio.to_thread(self.store.cached_request, sid, request.request_id, fingerprint)
            if cached:
                cached["diagnostics"] = {
                    **cached["diagnostics"],
                    "replayed": True,
                    "llm_calls": 0,
                    'decision_calls':0, 'retrieval_calls':0, 'generation_calls':0, 'verification_calls':0,
                    'usage':{},
                }
                return cached
            return await self._respond_locked(request, session, fingerprint)

    async def _respond_locked(self, request, session, fingerprint):
        sid = session["session_id"]
        budget = TurnBudget()
        started = time.perf_counter()
        text = request.message
        user_message_id = str(uuid.uuid4())
        if request.activity_id:
            self.select(session, request.activity_id)
        selected = (
            self.activity(session["selected_activity_id"])
            if session["selected_activity_id"]
            else None
        )
        if request.current_step is not None:
            if not selected or request.current_step > len(selected["steps"]):
                raise StoreError(422, "선택 활동의 단계 범위를 확인해 주세요.")
            session["current_step"] = request.current_step
        changed = bool(request.constraints and request.constraints.model_fields_set)
        if request.constraints:
            session["constraints"].update(
                request.constraints.model_dump(exclude_unset=True)
            )
        # 말로 한 시간·오븐 조건은 추천·조건 변경과 검색 설명에서만 세션에 반영한다.
        # 선택 활동에 대한 질문 속 '10분 끓여도'를 시간 제한으로 저장하지 않기 위해서다.
        asks_dishes = asks_for_dishes(text)
        spoken = spoken_constraints(text, session["constraints"], not selected or asks_dishes)
        response = {
            "message_id": str(uuid.uuid4()),
            "session_id": sid,
            "mode": "direct",
            "text": "",
            "activities": [],
            "sources": [],
            "actions": [],
            "selected_activity_id": session["selected_activity_id"],
            "current_step": session["current_step"],
            "execution_id": session["execution_id"],
            "diagnostics": {
                "intent": "unknown",
                "search_performed": False,
                "search_mode": "none",
                "llm_calls": 0,
                "llm_status": "not_needed",
                "latency_ms": 0,
                "trace_id": str(uuid.uuid4()),
            },
        }
        diag = response["diagnostics"]
        try:
            async with asyncio.timeout(budget.remaining_seconds()):
                rid = request.request_id or str(uuid.uuid4())
                # 1. 순서 선택은 새 검색을 하지 않습니다.
                ordinal = ORDINAL.search(text)
                number = (KOREAN_ORDINALS[ordinal[1]] if ordinal[1] else int(ordinal[2])) if ordinal else None
                # '1번이랑 2번 비교해줘', '두 번째 거 설명해줘'는 선택이 아니라 설명이다.
                selection = (
                    ordinal
                    and len(ORDINAL.findall(text)) < 2
                    and not re.search(r"(비교|차이|설명)", text)
                    and (
                        SELECTS_ORDINAL.match(text, ordinal.end())
                        or ORDINAL_ONLY.fullmatch(text[: ordinal.start()] + text[ordinal.end():])
                        # '좋아 2번'처럼 선택 표현이 앞에 오고 서수로 끝나는 말
                        or ORDINAL_ONLY.fullmatch(text, ordinal.end()) and SELECT_WORDS.search(text, 0, ordinal.start())
                    )
                )
                # 선택 활동의 문제 보고('커피를 넣었더니 망했어')나 확인 질문의 답('네 날씨가 많이 습했어요')에
                # 섞인 말은 주제 이탈이 아니다. 요청·해결 선언·완료 선언처럼 답이 아닌 말은 그대로 범위 밖으로 본다.
                # 확인 질문에는 무엇이든 답일 수 있어 내용으로 가리지 않는다. 답이 아닌 잡담도 관찰로 남을 수 있다.
                # 답변을 받은 뒤 덧붙인 관찰('날씨가 습한 날이었어요')도 열려 있는 문제의 후속이다.
                dialogue = session["dialogue"]
                follows_up = (
                    dialogue["pending_question"]
                    or dialogue.get("phase") in {"open", "answer_provided"} and observation_quotes(text)
                ) and not (INFO_REQUEST.search(text) or "추천" in text or issue_resolved(text) or COMPLETION.search(text))
                reports_progress = selected and (
                    is_trouble(text) and not OFF_TOPIC_COMPLAINT.fullmatch(text) or follows_up)
                out_of_scope = off_topic(text, selected) and not reports_progress
                interrupt = INTERRUPT.search(text)
                coaching_request = bool(COACHING_REQUEST.search(text) and
                    (len(coaching_requirements(text))>1 or session['dialogue'].get('issue_id')
                     and not ACTIVITY_LOOKUP.search(text) and not re.search(r'비교|차이',text)))
                lookup_request = bool(INFO_REQUEST.search(text) and (ACTIVITY_LOOKUP.search(text) or re.search(r"비교|차이", text))
                                      and not is_trouble(text) and not coaching_request)
                # '활동 시작하지 마', '활동 시작 취소'처럼 전환을 부정하면 진행 중인 확인 질문·관찰을 지우지 않는다.
        
                # '5단계 넘는 건 빼고 추천'의 단계는 선택 활동의 단계 번호가 아니다.
                wanted = None if "추천" in text else requested_step(text)
                recommend = False
                if re.search(r"(알레르기|알러지)", text):
                    diag["intent"] = "out_of_scope"
                    response.update(
                        mode="clarify",
                        text="이 활동코치는 알레르기 조건 판정을 지원하지 않습니다. 활동 추천이나 진행 방법을 질문해 주세요.",
                    )
                # 코치의 능력·수행 평가·추천 이유 같은 메타인지 질문은 검색하지 않고 확인된 상태로만 답한다.
                elif CAPABILITY.search(text):
                    diag["intent"] = "capabilities"
                    response["text"] = CAPABILITIES_TEXT
                elif selected and not out_of_scope and issue_resolved(text):
                    await self.dialogue.respond(text, session, response, user_message_id, budget=budget)
                elif SELF_ASSESSMENT.search(text):
                    diag["intent"] = "self_assessment"
                    await self.dialogue.respond(text, session, response, user_message_id, budget=budget)
                elif RECOMMEND_REASON.search(text):
                    diag["intent"] = "recommend_reason"
                    response["text"] = recommendation_reason(
                        session.get("last_recommendation"), session["constraints"], [self.activity(x) for x in session["candidate_ids"]]
                    )
                elif re.search(r"확실해|검토.*됐|무슨\s*근거", text):
                    state = normalize_dialogue(session["dialogue"], session)
                    evidence = await asyncio.to_thread(
                        self.retrieval.knowledge_for_activity, selected["activity_id"]) if selected else []
                    state.coverage = AnswerPolicy.assess_coverage(state.requirements,state.coverage,evidence,state.observations)
                    session['dialogue'] = serialize_dialogue(state)
                    response["metacognition"] = build_metacognition(state, evidence, None)
                    response["text"] = certainty_text(response["metacognition"], evidence)
                    diag["intent"] = "metacognition"
                elif wanted is not None and (
                    not selected or not 1 <= wanted <= len(selected["steps"])
                ):
                    diag["intent"] = "activity_lookup"
                    response.update(
                        mode="clarify",
                        text=(
                            f"단계 범위를 확인해 주세요. 선택한 활동은 1~{len(selected['steps'])}단계입니다."
                            if selected else "단계를 확인할 활동을 먼저 선택해 주세요."
                        ),
                    )
                # 확인 질문 대기 중에도 '준비물 알려줘', '된장찌개 레시피 알려줘'처럼 활동 정보나 다른 요리를 직접 요청하거나
                # 시작·전환을 말하면 답이 아니다. 질문은 남겨 둔다.
                elif (selected and session["dialogue"]["pending_question"] and not out_of_scope and not interrupt
                      and not lookup_request
                      and not (selection and session.get("candidate_selection_pending"))
                      and not (INFO_REQUEST.search(text) and (ACTIVITY_LOOKUP.search(text) or asks_dishes))):
                    await self.dialogue.respond(text, session, response, user_message_id, problem_step=wanted, budget=budget)
                elif selection:
                    if number < 1 or number > len(session["candidate_ids"]):
                        response.update(
                            mode="clarify",
                            text="해당 순서의 추천이 없습니다. 먼저 활동을 추천받아 주세요.",
                        )
                    else:
                        selected = self.select(
                            session, session["candidate_ids"][number - 1]
                        )
                        response.update(
                            text=f"'{selected['title']}' 활동을 선택했어요. 준비물이나 현재 단계를 물어보세요.",
                            activities=[selected],
                        )
                    diag["intent"] = "select"
                # '러닝 끝냈어'는 선택해 둔 요리 활동의 완료가 아니므로 아래 범위 밖 안내로 보낸다.
                # 완료 기록은 되돌릴 수 없다. '다 끝났나요' 같은 물음이나 '다 했는데 안 돼요' 같은 문제 보고로는 만들지 않는다.
                # 문제 표현이 완료 표현 뒤에 올 때만 문제 보고다. '어려웠는데 완성했어요'는 완료를 말한 것이다.
                elif ((done := COMPLETION.search(text)) and not NOT_COMPLETION.search(text)
                      and not QUESTION_END.search(text) and not is_trouble(text[done.end():]) and not out_of_scope):
                    diag["intent"] = "complete"
                    if not selected:
                        response.update(
                            mode="clarify", text="완료한 활동을 먼저 선택해 주세요."
                        )
                    elif partial_completion(text, selected):
                        response.update(
                            mode="clarify",
                            text="단계나 일부 작업을 마친 것만으로 활동 전체를 완료 처리하지 않아요. 모든 작업을 마쳤다면 '활동 완료했어'라고 알려주세요.",
                        )
                    else:
                        execution = self.ensure_execution(
                            session, selected, "chat-start-" + rid
                        )
                        self.store.update_progress(
                            execution["execution_id"], [True] * len(selected["steps"])
                        )
                        record = self.store.complete(
                            selected["activity_id"], execution["execution_id"], rid
                        )
                        response.update(
                            mode="action",
                            text=(
                                "활동 완료를 기록했어요."
                                if record["created"]
                                else "이미 완료 기록이 저장되어 있어요."
                            ),
                            record=record,
                            actions=[{"type": "view_records", "label": "완료 기록 보기"}],
                        )
                # '지금 시작해도 돼?'처럼 시작에 관한 물음이나 '시작할 때부터 막혔어요' 같은 문제 보고는 실행을 만들지 않는다.
                # 문제 표현이 시작 표현 뒤에 올 때만 문제 보고다. '어렵지만 시작할게요'는 시작하겠다는 말이다.
                elif ((begin := START.search(text)) and not re.search(r"(추천|만한|알려|[?？]|해도|해야)", text)
                      and not QUESTION_END.search(text) and not is_trouble(text[begin.end():])):
                    diag["intent"] = "start"
                    if NEGATED_ACTION.search(text):
                        response.update(mode="clarify", text="활동을 시작하거나 다시 시작하지 않았어요. 현재 진행 상태를 유지할게요.")
                    elif not selected:
                        response.update(
                            mode="clarify", text="시작할 활동을 먼저 선택해 주세요."
                        )
                    else:
                        previous = session["execution_id"]
                        # 완료한 실행은 이어가지 않는다. 완료 선언의 중복 기록 방지는 ensure_execution 재사용에 맡긴다.
                        finished = bool(previous) and self.store.get_execution(previous)["status"] == "completed"
                        execution = self.ensure_execution(
                            session, selected, "chat-start-" + rid, force_new="다시" in text or finished
                        )
                        total = len(selected["steps"])
                        # 진행 중인 실행을 이어갈 때는 현재 단계를 유지한다. 새 실행만 1단계부터다.
                        if execution["execution_id"] != previous:
                            self.attach_execution(session, execution)
                            reply = f"'{selected['title']}' 활동을 시작했어요. 총 {total}단계입니다."
                        else:
                            reply = f"'{selected['title']}' 활동을 이어서 진행할게요. 현재 {session['current_step']}/{total}단계입니다."
                        response.update(mode="action", text=reply, activities=[selected])
                # 문제 보고 속의 '레시피대로', '먹을 만한지'는 다른 요리를 찾는 말이 아니다. 추천은 분명히 요청했을 때만 한다.
                elif (selected and (is_trouble(text) or coaching_request) and not out_of_scope
                      and not CLEARLY_ASKS_FOR_DISHES.search(SUBSTITUTE.sub(" ", text))):
                    await self.dialogue.respond(text, session, response, user_message_id, problem_step=wanted, budget=budget)
                # '3단계에서 설탕이 안 녹아', '재료를 다 넣었어'처럼 상태를 말하는 문장은 단계·준비물 조회가 아니다.
                elif (ACTIVITY_LOOKUP.search(text) and not re.search(r"(추천|비교|차이|설명|쉽게|왜|어떻게|원문)", text)
                      and not FIND_DISH.search(text)
                      and not (selected and (observation_quotes(text) or states_condition(text)))):
                    diag["intent"] = "activity_lookup"
                    if not selected:
                        response.update(
                            mode="clarify",
                            text="어느 활동인지 먼저 선택해 주세요. 예: 두 번째 걸로 할게",
                        )
                    else:
                        if "다음" in text:
                            session["current_step"] = min(
                                len(selected["steps"]), session["current_step"] + 1
                            )
                        if "이전" in text:
                            session["current_step"] = max(1, session["current_step"] - 1)
                        if re.search(r"(준비물|재료|도구)", text):
                            materials = selected.get("materials", [])
                            tools = selected.get("tools", [])
                            response["text"] = (
                                "준비물: "
                                + (
                                    " · ".join(materials)
                                    if materials
                                    else "별도 재료가 기재되어 있지 않아요."
                                )
                                + "\n도구: "
                                + (
                                    " · ".join(tools)
                                    if tools
                                    else "도구 정보가 기재되어 있지 않아요."
                                )
                            )
                            if "quantity_units_unverified" in selected.get(
                                "quality", {}
                            ).get("flags", []):
                                response[
                                    "text"
                                ] += "\n계량 단위가 검증되지 않은 참고 자료이므로 수량을 확정해 안내할 수 없어요."
                            response["sources"] = [evidence_for(selected)]
                        elif re.search(r"(몇\s*분|얼마나\s*걸)", text):
                            total = selected["duration"]["total_minutes"]
                            response["text"] = (
                                f"기재된 전체 예상 시간은 {total:g}분입니다."
                                if total is not None
                                else "전체 소요 시간이 확인되지 않습니다. 직접 작업 시간과 대기 시간을 합쳐 추측하지 않을게요."
                            )
                            response["sources"] = [
                                {
                                    "activity_id": selected["activity_id"],
                                    "title": selected["title"],
                                    "kind": selected["kind"],
                                    "excerpt": json.dumps(
                                        selected["duration"], ensure_ascii=False
                                    ),
                                }
                            ]
                        elif "순서" in text:
                            response["text"] = "\n".join(
                                f"{i+1}. {x}" for i, x in enumerate(selected["steps"])
                            )
                            response["sources"] = [evidence_for(selected)]
                        else:
                            if wanted is not None:
                                session["current_step"] = wanted
                            step = session["current_step"]
                            response["text"] = (
                                f'{step}/{len(selected["steps"])}단계\n{selected["steps"][step-1]}'
                            )
                            response["sources"] = [evidence_for(selected, step)]
                elif re.search(r"(기록|얼마나\s*했|완료\s*목록)", text):
                    diag["intent"] = "records"
                    records = await asyncio.to_thread(self.store.records, sid)
                    response.update(
                        text=f"이 대화에서 완료한 활동은 {len(records)}개입니다."
                        + (
                            "\n" + "\n".join(x["activity_title"] for x in records[:10])
                            if records
                            else ""
                        ),
                        records=records,
                        actions=[{"type": "view_records", "label": "완료 기록 보기"}],
                    )
                elif out_of_scope:
                    diag["intent"] = "out_of_scope"
                    response.update(mode="clarify", text=OUT_OF_SCOPE_TEXT)
                elif self.dialogue.accepts(text, session) and not asks_dishes and not lookup_request:
                    await self.dialogue.respond(text, session, response, user_message_id, budget=budget)
                # 인사·감사·맞장구는 검색하지 않는다. 검색하면 직전 추천 목록이 바뀐다.
                elif re.fullmatch(
                    r"(?:(?:안녕\S*|고(?:마|맙)\S*|감사\S*|땡큐|알겠\S*|오케이|좋아요?|응|네)\s*)+[ㅎㅋ.!?~ ]*", text
                ):
                    diag["intent"] = "greeting"
                    response["text"] = (
                        "요리 활동을 함께 진행할게요. 오늘 할 활동을 추천받거나 선택한 활동의 준비물·순서를 물어보세요."
                    )
                # 선택 활동 없이 '이거 어떻게 해?'라고 하면 가리키는 활동을 알 수 없다. 검색 1위 활동으로 추측하지 않는다.
                # '나 이거 먹고 싶어', '그거 10분 안에 돼?'처럼 요리를 찾거나 조건을 밝힌 말은 추천으로 둔다.
                elif (not selected and DEICTIC.search(text) and number is None
                      and not (spoken or changed or asks_dishes
                               or STATED_LIMIT.search(text) or WANTS_DISH.search(text))):
                    diag["intent"] = "ambiguous"
                    response.update(
                        mode="clarify",
                        text="어느 활동을 말하는지 알 수 없어요. 추천 목록의 번호나 활동 이름으로 먼저 선택해 주세요.",
                    )
                # 선택 활동이 있으면 다른 요리를 찾거나 조건을 밝히는 말이 없는 물음은 그 활동에 대한 질문이다.
                # '따뜻한'의 '뜻', '쉽게 만들 수 있는 요리 추천해줘'의 '쉽게'는 설명 요청이 아니다. 추천을 말했으면 추천이 먼저다.
                elif re.search(r"(설명|비교|차이|원문)", text) or (
                    re.search(r"(쉽게|왜|어떻게|(?<!따)뜻)", text) and "추천" not in text
                ) or (
                    selected
                    and (QUESTION_END.search(text) or INFO_REQUEST.search(text))
                    and not asks_dishes
                    and not STATED_LIMIT.search(text)
                ):
                    diag["intent"] = "explain"
                    items = []
                    candidates = session["candidate_ids"]
                    if "비교" in text or "차이" in text:
                        items = [self.activity(x) for x in candidates[:3]]
                    # 선택 활동이 있으면 후보를 가리킨 말일 때만 그 후보로 설명한다.
                    elif number and 1 <= number <= len(candidates) and (
                            not selected or NAMES_CANDIDATE.match(text, ordinal.end())):
                        items = [self.activity(candidates[number - 1])]
                    elif selected:
                        items = [selected]
                        if not CURRENT_CONTEXT.search(text):
                            # 다른 활동 이름을 물었는지 확인한다. 이름을 직접 말했으므로 세션 조건으로 거르지 않는다.
                            budget.consume('retrieval')
                            found = await asyncio.to_thread(
                                self.retrieval.search, text, session["hobby_key"], {}, 3, "activities"
                            )
                            diag.update(search_performed=True, **found["diagnostics"])
                            if found["items"] and names_activity(text, found["items"][0]):
                                items = found["items"][:1]
                    if not items:
                        apply_constraints(session["constraints"], spoken)
                        # GPU 추론이 이벤트 루프를 막지 않도록 스레드에서 검색한다.
                        budget.consume('retrieval')
                        found = await asyncio.to_thread(
                            self.retrieval.search,
                            text,
                            session["hobby_key"],
                            session["constraints"],
                            3,
                            "activities",
                        )
                        items = found["items"]
                        diag.update(search_performed=True, **found["diagnostics"])
                    if not items:
                        response.update(
                            mode="clarify",
                            text="설명할 활동의 근거를 찾지 못했어요. 활동을 먼저 선택하거나 질문을 구체적으로 적어 주세요.",
                        )
                    else:
                        evidence = [
                            evidence_for(
                                item,
                                (
                                    (wanted or session["current_step"])
                                    if selected
                                    and item["activity_id"] == selected["activity_id"]
                                    and "단계" in text
                                    else None
                                ),
                            )
                            for item in items
                        ]
                        response["sources"] = evidence
                        context = build_issue_context(normalize_dialogue(session["dialogue"], session), session)
                        context["allowed_activity_ids"] = [item["activity_id"] for item in items]
                        result = await AnswerPolicy().answer(text, evidence, context, [], self.llm, budget)
                        response.update({k:v for k,v in result.items() if k not in {"llm_calls", "usage", "status"}})
                        diag["llm_status"] = result["status"]
                # 요리를 찾는 뜻이 드러나지 않은 말은 추천으로 추측하지 않는다. 문제 보고인지, 추천인지, 알 수 없는지 먼저 가린다.
                elif not (spoken or changed or asks_dishes or STATED_LIMIT.search(text)
                          or WANTS_DISH.search(text)) and await self._unruled(
                              text, session, response, user_message_id, selected, budget, problem_step=wanted):
                    pass
                else:
                    recommend = True
                # 대화 처리기가 문제 코칭이 아니라고 판단한 말은 여기서 이어받는다.
                handoff = response.pop("handoff", None)
                if handoff:
                    response.pop("metacognition", None)
                if handoff == "out_of_scope":
                    diag["intent"] = "out_of_scope"
                    response.update(mode="clarify", text=OUT_OF_SCOPE_TEXT)
                elif handoff == "meta":
                    diag["intent"] = "capabilities"
                    response["text"] = CAPABILITIES_TEXT
                elif handoff and handoff != "recommend":
                    diag.update(intent="ambiguous", stop_reason="ambiguous_intent")
                    response.update(mode="clarify", text=UNCLEAR_SELECTED if selected else UNCLEAR_UNSELECTED)
                if recommend or handoff == "recommend":
                    # 2. 새 추천/조건 변경에서만 검색합니다. 조건 변경은 이번 세션에만 저장합니다.
                    # 선택 활동이 있으면 다른 요리를 찾거나 조건을 직접 밝힌 경우에만 반영한다. '10분 끓여도 가능해'는 그 활동에 대한 물음이다.
                    if spoken and (not selected or asks_dishes or STATED_LIMIT.search(text)):
                        apply_constraints(session["constraints"], spoken)
                        changed = True
                    diag["intent"] = "update_constraints" if changed else "recommend"
                    # '칼이 없어', '다른 거'는 조건과 요청이지 검색 주제가 아니다. 남는 주제가 없으면 직전 주제를 이어 쓴다.
                    other = bool(OTHER_DISHES.search(text))
                    topic = OTHER_DISHES.sub(" ", NO_TOOL.sub(" ", text))
                    reuse = (other and not names_topic(topic)
                             or changed and not re.search(r"(추천|레시피|요리)", text))
                    query = (session.get("search_query", "") if reuse else topic) or topic
                    # 후속 시간·도구 조건에서도 직전의 검색 범위를 유지합니다.
                    # 사용자가 레시피 또는 요리 활동을 명시할 때만 범위를 전환합니다.
                    if "레시피" in text:
                        scope = "recipes"
                    elif re.search(r"(요리\s*활동|활동\s*추천)", text):
                        scope = "activities"
                    else:
                        scope = session.get("search_scope", "activities")
                    # 레시피는 scripts.import_recipes로 따로 적재한다. 색인되지 않은 범위에 고정되면 이후 추천이 모두 비므로 활동에서 찾는다.
                    # 문서 수 집계는 레시피 54만 건을 훑으므로 이벤트 루프 밖에서 읽는다.
                    if not (await asyncio.to_thread(self.retrieval.status))["documents"].get(scope):
                        scope = "activities"
                    session["search_scope"] = scope
                    may_fall_back = (bool(WANTS_SUGGESTION.search(text)) or handoff == "recommend") \
                        and not outside_cooking(text)
                    items, found, starter = await self._candidates(session, query, scope, other, may_fall_back, budget)
                    diag.update(search_performed=True, **found["diagnostics"])
                    diag.update(candidate_ids=[x["activity_id"] for x in items], starter_fallback=starter)
                    # 결과가 없으면 직전 추천 목록을 남겨 번호 선택을 이어갈 수 있게 한다.
                    if items:
                        # 맞는 활동을 찾은 주제만 기억한다. 못 찾은 주제가 직전 주제를 덮어쓰면 '다른 거'가 그 주제를 이어 쓴다.
                        if not starter:
                            session["search_query"] = query
                        session["candidate_ids"] = [x["activity_id"] for x in items]
                        session["shown_ids"] =list(dict.fromkeys([*session.get("shown_ids", []), *session["candidate_ids"]]))
                        # 새 후보를 받은 뒤의 '1번'은 이전 확인 질문의 답이 아니라 후보 선택이다.
                        session["candidate_selection_pending"] = True
                        session["last_recommendation"] = {
                            "message_id": response["message_id"], "query": query, "scope": scope,
                            "constraints": dict(session["constraints"]), "candidate_ids": list(session["candidate_ids"]),
                            "ranking": found["diagnostics"].get("ranking", []), "search_mode": found["diagnostics"]["search_mode"]}
        
                    if not items:
                        if found["items"]:
                            reason = "조건에 맞는 활동은 이미 모두 보여드렸거나 완료했어요."
                        elif session["constraints"].get("max_difficulty") is not None:
                            reason = "별도 난이도가 검증된 자료가 없어 해당 조건의 충족을 확인할 수 없어요."
                        elif session["constraints"].get("budget_max") is not None:
                            reason = "검증된 비용 정보가 없어 예산 조건의 충족을 확인할 수 없어요."
                        else:
                            reason = (
                                "현재 자료에서 질문과 조건에 맞는 활동을 찾지 못했어요."
                            )
                        response.update(
                            mode="clarify",
                            text=reason + " 조건을 바꾸거나 다른 활동을 요청해 주세요.",
                        )
                    else:
                        response.update(
                            text=("요청과 직접 맞는 활동은 찾지 못했어요. 지금 조건에서 시간이 짧은 활동부터 보여드릴게요.\n"
                                  if starter else "")
                            + "다음 활동을 찾았어요. 번호로 선택하면 준비물과 순서를 이어서 안내할게요.\n"
                            + "\n".join(
                                f'{i+1}. {x["title"]}' for i, x in enumerate(items)
                            ),
                            activities=items,
                            sources=[
                                {
                                    "activity_id": x["activity_id"],
                                    "title": x["title"],
                                    "kind": x["kind"],
                                    "excerpt": x.get("description") or x["title"],
                                }
                                for x in items
                            ],
                        )
        except (TimeoutError, BudgetExceeded):
            self.dialogue._stop(response, "budget_exhausted")
        except ValueError as error:
            if str(error) != 'context_limit':
                raise
            self.dialogue._stop(response, "context_limit")
        response.pop("handoff", None)
        if response["activities"] and not response["actions"]:
            response["actions"] = [
                {
                    "type": "open_activity",
                    "activity_id": x["activity_id"],
                    "label": x["title"],
                }
                for x in response["activities"]
            ]
        response.update(
            selected_activity_id=session["selected_activity_id"],
            current_step=session["current_step"],
            execution_id=session["execution_id"],
        )
        diag.update(budget.diagnostics())
        diag.pop("query_aliases", None)
        state = normalize_dialogue(session["dialogue"], session)
        response.update(issue_id=state.issue_id, issue_phase=state.phase)
        diag["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        await asyncio.to_thread(
            self.store.save_reply, session, request.request_id, fingerprint, response, text, user_message_id
        )
        return response
