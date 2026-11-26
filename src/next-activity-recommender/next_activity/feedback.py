"""Conservative Korean phrase baseline for one activity record.

Unknown or ambiguous input produces no difficulty inference. Evidence is copied
from the original input so the caller can inspect what triggered a result.
"""

import re


def _pattern(*phrases: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{phrase})" for phrase in phrases))


# Complete endings keep reported or hypothetical difficulty ("어렵다고",
# "어려웠을") from looking like the user's own experience.
def _difficulty_pattern(past: str, present: str, stem: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:{past}(?:어요|습니다|지만|는데|고|다|더라고요)"
        rf"|{present}(?:요|서|도)|{stem}(?:네요|습니다|더라고요|지만|다))(?![가-힣])"
    )


_DIFFICULTIES = {
    "EASY": _difficulty_pattern("쉬웠", "쉬워", "쉽"),
    "APPROPRIATE": _pattern(
        r"난이도(?:가|는|도)?\s*(?:딱\s*)?(?:적당|적절)(?:했어요|했지만|해요|했습니다|했다)(?![가-힣])"
    ),
    "HARD": _difficulty_pattern("어려웠", "어려워", "어렵"),
}

_SENTIMENTS = {
    "POSITIVE": _pattern(
        r"(?:재미|흥미)(?:가\s*)?있(?:었어요|었습니다|었지만|었고|었는데|어요|네요|고|지만)",
        r"재밌(?:었어요|어요|네요|지만|고|는데|었지만|었습니다)",
        r"즐거웠(?:어요|습니다|지만|고)",
        r"뿌듯(?:했어요|했지만|해요|했습니다)",
        r"마음에\s*(?:들었어요|들어요|들었습니다)",
    ),
    "NEGATIVE": _pattern(
        r"재미(?:가\s*)?없(?:었어요|어요|네요|고|었지만|었습니다)",
        r"(?:지루|답답|실망)(?:했어요|했습니다|해요|했지만|했고)",
        r"별로였(?:어요|습니다|지만)",
    ),
}

# Signals represent explicit requests or concrete burdens in this record.
# A comparison or a mere mention of a tool is insufficient.
_SIGNALS = {
    "PREFER_SHORTER": _pattern(
        r"(?:시간(?:이|은|도)?\s*)?(?:너무\s*)?오래\s*걸(?:렸어요|렸습니다|렸지만|렸고|려요|려서|린다)",
        r"시간(?:이|은|도)?\s*(?:너무\s*)?(?:부족(?:해요|했어요|해서|합니다|했지만)|모자(?:라요|라서|랐어요)|없(?:어요|었어요|어서|습니다))",
        r"(?:짧은|더\s*짧은)\s*(?:활동|요리|연습)(?:을|이|으로)?\s*(?:원해요|하고\s*싶어요|좋겠어요)",
    ),
    "PREFER_LOWER_COST": _pattern(
        r"(?:준비물|재료|도구|재료비|준비물\s*가격|비용)(?:이|가|은|는|도)?\s*(?:너무\s*)?비(?:싸요|쌌어요|싸서|쌌습니다)",
        r"(?:재료비|비용|가격)(?:이|가|은|는|도)?\s*(?:너무\s*)?부담(?:됐어요|돼요|스러워요|됩니다)",
    ),
    "PREFER_FEWER_TOOLS": _pattern(
        r"(?:설거지|도구|준비물)(?:이|가|은|는|도)?\s*(?:너무\s*)?많(?:았어요|았지만|았고|았는데|아요|아서|았습니다)",
        r"뒷정리(?:가|는|도)?\s*(?:너무\s*)?힘(?:들었어요|들어요|들어서|들었습니다)",
    ),
    "PREFER_ONE_PAN": _pattern(
        r"(?:팬\s*(?:하나|한\s*개)로|원팬)\s*(?:(?:끝나는|하는|만드는)\s*(?:걸|것을|거|요리|활동|레시피)(?:를|가|이)?\s*|요리(?:가|를)?\s*)?(?:하고\s*싶어요|만들고\s*싶어요|원해요|좋겠어요)",
    ),
}

_NEGATED_BEFORE = re.compile(r"(?<!\w)(?:안|못)\s*$")
_NEGATED_AFTER = re.compile(
    r"^\s*(?:(?:(?:던|다는|는)\s*)?(?:건|게|것은|것이|뜻은|뜻이)\s*아니"
    r"|고\s*(?:생각하지|느끼지)\s*않)"
)
# A procurement or scheduling problem alone does not rate the activity's difficulty.
_LOGISTICS = re.compile(
    r"(?:구하|구입하|사|준비하|예약하|시간\s*(?:내|맞추)|(?:재료|도구|준비물)(?:을|를)?\s*찾)"
    r"기(?:가|는|도)?\s*(?:(?:너무|좀|조금)\s*)?$"
)


def _evidence(
    texts: tuple[str, str], rules: dict[str, re.Pattern[str]], difficulty: bool = False
) -> dict[str, str]:
    found = {}
    for label, pattern in rules.items():
        for text in texts:
            for match in pattern.finditer(text):
                before = text[: match.start()]
                if _NEGATED_BEFORE.search(before) or _NEGATED_AFTER.match(
                    text[match.end() :]
                ):
                    continue
                if difficulty and _LOGISTICS.search(before):
                    continue
                found[label] = match.group()
                break
            if label in found:
                break
    return found


def analyze_feedback(
    memo: str, not_done_reason: str, perceived_difficulty: str | None
) -> dict:
    """Return transient recommendation signals. The user's explicit rating wins."""
    texts = (memo, not_done_reason)
    difficulty_spans = _evidence(texts, _DIFFICULTIES, difficulty=True)
    sentiment_spans = _evidence(texts, _SENTIMENTS)
    signal_spans = _evidence(texts, _SIGNALS)

    difficulty = perceived_difficulty
    if difficulty is None and len(difficulty_spans) == 1:
        difficulty = next(iter(difficulty_spans))
    sentiment = (
        "MIXED" if len(sentiment_spans) == 2 else next(iter(sentiment_spans), "UNKNOWN")
    )

    spans = []
    if difficulty in difficulty_spans:
        spans.append(difficulty_spans[difficulty])
    spans.extend(sentiment_spans.values())
    spans.extend(signal_spans.values())
    return {
        "difficulty": difficulty,
        "signals": list(signal_spans),
        "sentiment": sentiment,
        "evidence": list(dict.fromkeys(spans)),
    }
