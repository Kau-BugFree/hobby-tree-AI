"""발화에서 값을 꺼내는 추출기: 단계 번호, 후보 서수, 시간·도구 조건."""

import re
from functools import lru_cache
from .store import StoreError
from .retrieval import query_terms
from .vocabulary import Vocabulary, vocabulary_for

# '세 번째 단계'처럼 단계를 한글 서수로 말한 경우의 번호다.
KOREAN_ORDINALS = {"첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
# 숫자는 네 자리까지, 숫자 열의 처음에서만 읽는다. '\d+'로 두면 긴 숫자 열에서 시작 위치마다 끝까지 읽어 제곱 시간이 걸린다.
STEP_NUMBER = re.compile(r"(?<!\d)(\d{1,4})\s*(?:번째|번)?\s*단계|(첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*번째\s*단계")
STEP_NEXT = re.compile(r"다음\s*단계")
STEP_PREV = re.compile(r"이전\s*단계")
MAX_MINUTES = 43200  # 가용 시간 상한(30일)


def requested_step(text: str) -> int | None:
    """'3단계', '세 번째 단계'처럼 단계 번호를 직접 말했으면 그 번호를, 아니면 None을 반환합니다."""
    match = STEP_NUMBER.search(text)
    if not match:
        return None
    return int(match[1]) if match[1] else KOREAN_ORDINALS[match[2]]


def step_move(text: str) -> str | None:
    """'다음 단계'면 "next", '이전 단계'면 "prev", 단계 이동이 아니면 None을 반환합니다. '다음 재료'는 이동이 아닙니다."""
    if STEP_NEXT.search(text):
        return "next"
    if STEP_PREV.search(text):
        return "prev"
    return None


# 후보 선택 서수. 뒤에 단계·재료·과정이 오거나 '1번 더', '1번만', '2번 정도'처럼 횟수를 말하면 후보 선택이 아니다.
ORDINAL = re.compile(
    r"(?:(첫|두|세|네)\s*번째|(?<!\d)(\d{1,4})\s*번(?:째)?(?!째))(?!\s*(?:단계|재료|준비물|도구|과정|순서|더|만|정도|씩))"
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


def candidate_number(text: str) -> int | None:
    """첫 서수가 가리키는 후보 번호를, 서수가 없으면 None을 반환합니다."""
    ordinal = ORDINAL.search(text)
    return (KOREAN_ORDINALS[ordinal[1]] if ordinal[1] else int(ordinal[2])) if ordinal else None


def selects_candidate(text: str) -> bool:
    """서수 하나로 후보를 고르는 말이면 True를 반환합니다."""
    ordinal = ORDINAL.search(text)
    # '1번이랑 2번 비교해줘', '두 번째 거 설명해줘'는 선택이 아니라 설명이다.
    return bool(
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


# '냉장고 안에 30분'의 '안에'는 장소이므로 시간 숫자 바로 뒤의 '안에'만 시간 조건으로 본다.
TIME_LIMIT = re.compile(r"(오늘|가능|이내|이하|추천|만\s*할|\d\s*(?:분|시간)\s*(?:만|안에|안으로|내에|내로))")
# 받는 값('1.5시간', '90분')은 그대로 두고 숫자 열의 처음에서만 읽어 긴 숫자 열의 제곱 시간을 막는다.
TIME_AMOUNT = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*시간(?:\s*(\d+)\s*분)?|(?<!\d)(\d+(?:\.\d+)?)\s*분")
# '30분 이상', '3분만 더'는 쓸 수 있는 시간의 상한이 아니다.
NOT_A_LIMIT = re.compile(r"(이상|넘게|\d\s*분\s*(?:만\s*)?더)")
# '시간 없어서'는 시간이 부족하다는 뜻이다. 명시적 해제 표현만 해제로 본다.
NO_TIME_LIMIT = re.compile(r"시간\s*(?:은|는|이)?\s*(?:제한\s*(?:은|이)?\s*(?:없|해제|풀)|상관\s*없|관계\s*없)")
@lru_cache
def no_tool_pattern(vocab: Vocabulary) -> re.Pattern:
    """없다고 말한 도구. 뒤의 어미까지 잡아 검색 주제에서 통째로 뺀다. 한 글자 도구는 '전체가 없어'처럼
    다른 낱말 끝에 걸리지 않게 앞 글자를 본다."""
    names = [re.escape(tool) for tool in vocab.tools if len(tool) > 1]
    short = [re.escape(tool) for tool in vocab.tools if len(tool) == 1]
    if short:
        names.append(rf"(?<![가-힣])(?:{'|'.join(short)})")
    return re.compile(rf"({'|'.join(names)})\s*(?:이|가|은|는|도)?\s*(?:없|못\s*써|안\s*써)\S*")


@lru_cache
def other_dishes_pattern(vocab: Vocabulary) -> re.Pattern:
    """이미 본 후보 말고 다른 것을 찾는 말. 뒤의 조사까지 잡아 검색 주제에서 뺀다. 거·것·종류는 취미와 무관한 말이다."""
    nouns = "|".join(re.escape(noun) for noun in vocab.dish_nouns)
    return re.compile(rf"(?:다른|딴)\s*(?:거|것|걸|건|게|{nouns}|종류)(?:으로|로|을|를|은|는|도)?")


# 요리의 패턴. 예전 가져오기 경로(from app.slots import NO_TOOL)를 유지한다.
NO_TOOL = no_tool_pattern(vocabulary_for("cooking"))
OTHER_DISHES = other_dishes_pattern(vocabulary_for("cooking"))
# '다른 거 추천해 주세요', '다른 거 보여줘'에서 주제가 아닌 채 남는 말. 한 글자 낱말(좀·또·해·요)은 길이로 거른다.
REQUEST_FILLER = {"주세요", "줄래", "줄래요", "주라", "보여", "보여줘", "보여줘요", "보여주세요", "하나", "부탁", "부탁해", "부탁해요",
                  "없나", "없나요", "없을까", "없을까요", "있나", "있나요", "있을까", "있을까요", "추천해"}


def names_topic(text: str) -> bool:
    """요청 표현을 빼고도 검색할 주제어가 남으면 True를 반환합니다."""
    return any(len(word) > 1 and word not in REQUEST_FILLER for word in query_terms(text, "activities")[0].split())


def spoken_constraints(text: str, constraints: dict, finding_dishes: bool = True, *, hobby: str = "cooking") -> dict:
    """문장의 시간·도구 조건을 세션 조건에 덮어쓸 값으로 반환합니다. 세션은 바꾸지 않고, 값 검증은 반영할 때 합니다.

    key_tools(요리는 오븐) 말고 다른 도구는 요리를 찾는 말일 때만 조건으로 봅니다.
    활동 중의 '계량스푼이 없어'는 그 활동의 문제 보고입니다.
    """
    vocab = vocabulary_for(hobby)
    updates = {}
    # '1시간 30분'은 시간과 분을 더한다. 첫 숫자만 읽으면 60분이 된다.
    match = TIME_AMOUNT.search(text)
    if match and TIME_LIMIT.search(text) and not NOT_A_LIMIT.search(text):
        updates["max_minutes"] = float(match[1]) * 60 + float(match[2] or 0) if match[1] else float(match[3])
    elif NO_TIME_LIMIT.search(text):
        updates["max_minutes"] = None
    missing = [tool for tool in no_tool_pattern(vocab).findall(text) if tool in vocab.key_tools or finding_dishes]
    if missing:
        excluded = constraints.get("excluded_tools") or []
        updates["excluded_tools"] = list(dict.fromkeys([*excluded, *missing]))
    return updates


def apply_constraints(constraints: dict, updates: dict) -> None:
    """말로 한 조건을 세션 조건에 반영합니다. 범위 밖 시간은 반영하지 않고 거부합니다."""
    minutes = updates.get("max_minutes")
    if minutes is not None and not 0 < minutes <= MAX_MINUTES:
        raise StoreError(422, "가용 시간은 0보다 크고 30일 이하여야 합니다.")
    constraints.update(updates)


def has_conditions(constraints: dict) -> bool:
    """검색 대상을 줄이는 조건이 걸려 있으면 True를 반환합니다. 빈 제외 목록은 아무것도 거르지 않으므로 조건이 아닙니다."""
    return bool(constraints.get("excluded_tools")) or any(
        value is not None for name, value in constraints.items() if name != "excluded_tools")
