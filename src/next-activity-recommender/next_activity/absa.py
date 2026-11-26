"""Korean aspect-category sentiment baseline for a single activity record.

Evidence offsets are half-open Unicode code point offsets in the original
field. Rules distinguish the user's evaluation from a request for next time.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    polarity: str
    pattern: re.Pattern[str]
    signal: str | None = None
    request: bool = False


def rule(
    polarity: str, phrase: str, signal: str | None = None, *, request: bool = False
) -> Rule:
    # An incomplete stem inside a quote, hypothesis or longer word is not evidence.
    return Rule(
        polarity, re.compile(r"(?:" + phrase + r")(?![가-힣])"), signal, request
    )


# Common completed or connective endings. Quoted/hypothetical forms are omitted.
PART = r"(?:이|가|은|는|도|을|를)?\s*"
DEGREE = r"(?:(?:너무|정말|꽤|많이|좀|조금|매우)\s*)?"
PAST = r"(?:어요|습니다|지만|는데|고|더라고요|다)"
DID = r"(?:했(?:어요|습니다|지만|는데|고|다)|해요|해서|합니다|하다)"
NEGATED = r"(?:지(?:는|도)?\s*않(?:았(?:어요|습니다|지만|고|다)|아요|습니다|아|고|다))"
WANT = (
    r"(?:하고\s*싶(?:어요|습니다|다)|원(?:해요|합니다|했어요)|좋겠(?:어요|습니다|다))"
)
TIME = r"(?:소요\s*시간|활동\s*시간|시간)" + PART
COST = r"(?:재료비|비용|가격|예산|재료\s*가격|준비물\s*가격)" + PART
PRICED_ITEM = rf"(?:{COST}|(?:재료|준비물|도구){PART})"
LIKED = rf"\s*(?:오히려\s*)?(?:좋(?:았{PAST}|아요)|마음에\s*들(?:었{PAST}|어요))"
TOOLS = r"(?:설거지|도구|준비물|뒷정리|정리할\s*것)" + PART
PAN = r"(?:팬\s*(?:하나|한\s*개)로\s*(?:(?:끝나는|하는|만드는)\s*(?:걸|것을|거|요리|활동|레시피))?|원팬(?:\s*(?:요리|활동|레시피))?)\s*(?:를|을|가|이|는|도)?\s*"

# Each match is about its named aspect. For example, an expensive tool is
# evidence about cost and supplies no opinion about the number of tools.
RULES = {
    "ENJOYMENT": (
        rule(
            "NEGATIVE",
            rf"(?:재미(?:가\s*)?있|재밌){NEGATED}|안\s*(?:재미있|재밌)(?:었{PAST}|어요)",
        ),
        rule(
            "NEGATIVE",
            rf"재미(?:가\s*)?없(?:었{PAST}|어요|고|지만|다)|(?:지루|실망){DID}|질렸{PAST}",
        ),
        rule(
            "POSITIVE",
            rf"(?:재미|흥미)(?:가\s*)?있(?:었{PAST}|어요|네요|고|지만|다)|재밌(?:었{PAST}|어요|네요|고|지만|다)|즐거웠{PAST}|뿌듯{DID}",
        ),
    ),
    "DIFFICULTY": (
        rule("NEGATIVE", rf"너무\s*쉬워서\s*(?:지루|심심){DID}"),
        rule(
            "POSITIVE",
            rf"{DEGREE}어려워서\s*(?:좋(?:았{PAST}|아요)|즐거웠{PAST})|쉬워서\s*(?:좋(?:았{PAST}|아요)|만족{DID})",
        ),
        rule("POSITIVE", rf"난이도{PART}(?:딱\s*)?(?:적당|적절){DID}"),
        rule("NEUTRAL", rf"(?:어렵|쉽){NEGATED}"),
        rule(
            "NEGATIVE",
            rf"{DEGREE}(?:어려웠{PAST}|어려워(?:요|서)|어렵(?:네요|습니다|지만|다))",
        ),
        rule("NEUTRAL", rf"쉬웠{PAST}|쉬워(?:요|서)|쉽(?:네요|습니다|지만|다)"),
    ),
    "DURATION": (
        rule("POSITIVE", rf"(?:{TIME})?{DEGREE}(?:오래\s*걸려서|길어서){LIKED}"),
        rule("NEUTRAL", rf"(?:{TIME})?{DEGREE}오래\s*걸리{NEGATED}"),
        rule(
            "NEGATIVE",
            rf"(?:{TIME})?{DEGREE}오래\s*걸(?:렸{PAST}|려요|려서|린다)|{TIME}{DEGREE}(?:부족{DID}|모자(?:라요|라서|랐{PAST})|없(?:어서|어요|습니다|었{PAST})|길(?:었{PAST}|어요|어서|고|다))",
            "PREFER_SHORTER",
        ),
        rule(
            "POSITIVE",
            rf"{TIME}(?:딱\s*)?(?:적당|충분){DID}|{TIME}괜찮(?:았{PAST}|아요|다)",
        ),
        rule(
            "POSITIVE",
            rf"(?:더\s*)?짧은\s*(?:활동|요리|연습|걸|것)(?:을|이|으로)?\s*{WANT}",
            "PREFER_SHORTER",
            request=True,
        ),
        rule(
            "NEUTRAL",
            rf"(?:{TIME})?\d+\s*(?:시간|분)(?:\s*정도)?\s*(?:이|이랑)?\s*걸(?:렸{PAST}|려요|린다)",
        ),
    ),
    "COST": (
        rule("POSITIVE", rf"{PRICED_ITEM}{DEGREE}비싸서{LIKED}"),
        rule("NEUTRAL", rf"{PRICED_ITEM}비싸{NEGATED}|{COST}부담스럽{NEGATED}"),
        rule(
            "NEGATIVE",
            rf"{PRICED_ITEM}{DEGREE}비(?:쌌{PAST}|싸요|싸서|싸고|싸다)"
            rf"|{COST}{DEGREE}(?:부담(?:스럽(?:네요|습니다|다)|스러(?:웠{PAST}|워요|워서)|됐{PAST}|돼요|됩니다)|많이\s*들(?:었{PAST}|어요))",
            "PREFER_LOWER_COST",
        ),
        rule(
            "POSITIVE",
            rf"{PRICED_ITEM}{DEGREE}(?:저렴{DID}|쌌{PAST}|싸요)|{COST}괜찮(?:았{PAST}|아요)",
        ),
        rule(
            "POSITIVE",
            rf"(?:저렴한|더\s*싼)\s*(?:활동|요리|걸|것)(?:을|이)?\s*{WANT}",
            "PREFER_LOWER_COST",
            request=True,
        ),
        rule(
            "NEUTRAL",
            rf"(?:재료비|가격|비용){PART}[\d,]+\s*원(?:이었{PAST}|입니다|이에요|이다)",
        ),
    ),
    "TOOLS": (
        rule("POSITIVE", rf"{TOOLS}{DEGREE}많아서{LIKED}"),
        rule("NEUTRAL", rf"{TOOLS}많{NEGATED}"),
        rule(
            "NEGATIVE",
            rf"{TOOLS}{DEGREE}많(?:았{PAST}|아요|아서|고|다)|(?:설거지|뒷정리){PART}{DEGREE}(?:힘들(?:었{PAST}|어요|어서|다)|번거로(?:웠{PAST}|워요|워서))",
            "PREFER_FEWER_TOOLS",
        ),
        rule(
            "POSITIVE",
            rf"(?:설거지|뒷정리){PART}(?:간단|편리){DID}|{TOOLS}적어서\s*좋(?:았{PAST}|아요)",
        ),
        rule(
            "POSITIVE",
            rf"(?:도구|준비물){PART}적은\s*(?:활동|요리|걸|것)(?:을|이)?\s*{WANT}",
            "PREFER_FEWER_TOOLS",
            request=True,
        ),
    ),
    "COOKING_METHOD": (
        rule(
            "NEGATIVE",
            rf"{PAN}(?:하고\s*싶{NEGATED}|싫(?:었{PAST}|어요|다)|별로(?:였{PAST}|예요|다))",
        ),
        rule(
            "POSITIVE",
            rf"{PAN}(?:{WANT}|만들고\s*싶(?:어요|습니다|다))",
            "PREFER_ONE_PAN",
            request=True,
        ),
        rule(
            "POSITIVE",
            rf"{PAN}(?:좋(?:았{PAST}|아요|다)|편(?:했{PAST}|해요))",
            "PREFER_ONE_PAN",
        ),
        rule("NEUTRAL", rf"{PAN}(?:요리|진행){DID}"),
    ),
}

_NEGATED_BEFORE = re.compile(r"(?<!\w)(?:안|못)\s*$")
_DISCLAIMED_AFTER = re.compile(
    r"""^\s*["'”’]?\s*(?:(?:라는|다는|다고|라고)"""
    r"|(?:던|다는|는)?\s*(?:건|게|것은|것이|뜻은|뜻이)\s*아니)"
)
_LOGISTICS_BEFORE = re.compile(
    r"(?:구하|구입하|사|준비하|예약하|시간\s*(?:내|맞추)|(?:재료|도구|준비물)(?:을|를)?\s*찾)기(?:가|는|도)?\s*$"
)


def _matches(category: str, rules: tuple[Rule, ...], text: str):
    hits = []
    for item in rules:
        for match in item.pattern.finditer(text):
            before, after = text[: match.start()], text[match.end() :]
            if _NEGATED_BEFORE.search(before) or _DISCLAIMED_AFTER.match(after):
                continue
            if category == "DIFFICULTY" and _LOGISTICS_BEFORE.search(before):
                continue
            hits.append((match, item))
    # A full construction ("어려워서 좋았어요") supersedes an overlapping
    # shorter one ("어려워서"). Keep distinct mentions within the same field.
    hits.sort(key=lambda hit: (hit[0].start(), -len(hit[0].group())))
    end = 0
    for match, item in hits:
        if match.start() >= end:
            yield match, item
            end = match.end()


def analyze_aspects(memo: str, not_done_reason: str) -> list[dict]:
    """Return one grounded observation per mentioned, supported category."""
    aspects = []
    for category, rules in RULES.items():
        evidence = []
        evaluations = set()
        requests = set()
        signals = set()
        for source, text in (("memo", memo), ("notDoneReason", not_done_reason)):
            for match, item in _matches(category, rules, text):
                evidence.append(
                    {
                        "source": source,
                        "text": match.group(),
                        "start": match.start(),
                        "end": match.end(),
                    }
                )
                (requests if item.request else evaluations).add(item.polarity)
                if item.signal:
                    signals.add(item.signal)
        if not evidence:
            continue
        # Requests describe a future option. A request for a shorter activity
        # reinforces "took too long" without turning that experience into MIXED.
        opinions = evaluations - {"NEUTRAL"}
        # For one-pan preference, liking and disliking refer to the same
        # option. Positive and negative evidence can genuinely conflict.
        if category == "COOKING_METHOD":
            opinions.update(requests)
        opinions = opinions or requests or {"NEUTRAL"}
        polarity = "MIXED" if len(opinions) > 1 else next(iter(opinions))
        signal = (
            next(iter(signals))
            if len(signals) == 1 and polarity not in {"MIXED", "NEUTRAL"}
            else None
        )
        aspects.append(
            {
                "category": category,
                "polarity": polarity,
                "evidence": evidence,
                "signal": signal,
            }
        )
    return aspects
