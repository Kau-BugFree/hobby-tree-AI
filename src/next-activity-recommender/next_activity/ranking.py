"""Rank every startable card before limiting the home response."""

from collections import defaultdict
from dataclasses import dataclass

_NUMERIC_SIGNALS = {
    "PREFER_SHORTER": "durationMinutes",
    "PREFER_LOWER_COST": "estimatedCost",
    "PREFER_FEWER_TOOLS": "requiredToolCount",
}
# Descending cutoffs fill empty home slots; zero admits every eligible card.
_TIERS = ((75, "STANDARD"), (55, "RELAXED"), (0, "OPEN"))


@dataclass(frozen=True)
class ScoredCard:
    card: dict
    score: float
    budget_status: str
    previous_after_hard: bool


def _budget_status(cost: int | None, budget: int | None) -> str:
    if budget is None:
        return "UNSET"
    if cost is None:
        return "UNKNOWN"
    return "WITHIN" if cost <= budget else "OVER"


def _time_points(duration: int | None, available: int | None) -> int:
    if available is None:
        return 16
    if duration is None:
        return 6
    return 16 if duration <= available else 0


def _score_pool(cards: list[dict], payload: dict) -> list[ScoredCard]:
    # The comparison set is the entire eligible new-card (or replay) pool.
    signals = set(payload["signals"])
    ranges = {}
    for signal, field in _NUMERIC_SIGNALS.items():
        if signal in signals:
            known = [card[field] for card in cards if card[field] is not None]
            if known:
                ranges[field] = (min(known), max(known))

    # Upstream validation supplies at most one observation per group/category.
    group_scores = defaultdict(int)
    for observation in payload.get("groupPreferences", []):
        group_scores[observation["activityGroup"]] += (
            1 if observation["polarity"] == "POSITIVE" else -1
        )

    scores = []
    for card in cards:
        distance = payload["currentStageNumber"] - card["stageNumber"]
        previous_after_hard = payload["preferredDifficulty"] == "HARD" and distance == 1
        stage_distance = 0 if previous_after_hard else distance
        points = 72 - 9 * stage_distance
        points += _time_points(card["durationMinutes"], payload["availableMinutes"])
        budget_status = _budget_status(card["estimatedCost"], payload["budgetKrw"])
        if budget_status == "WITHIN":
            points += 2
        points += group_scores.get(card.get("activityGroup"), 0)
        if "PREFER_ONE_PAN" in signals and card["onePan"] is True:
            points += 1
        for field, (low, high) in ranges.items():
            value = card[field]
            if value is not None:
                points += 1 if high == low else (high - value) / (high - low)
        scores.append(
            ScoredCard(card, round(points, 6), budget_status, previous_after_hard)
        )
    return scores


def _price_order(scored: ScoredCard) -> tuple:
    # Compare prices only within the same 5-point fit band. Unknown ≠ free.
    cost = scored.card["estimatedCost"]
    return (
        -int(scored.score // 5),
        cost is None,
        cost,
        -scored.score,
        not scored.previous_after_hard,
    )


def _item(scored: ScoredCard, tier: str, repeat: bool, payload: dict) -> dict:
    card = scored.card
    duration = card["durationMinutes"]
    cost = card["estimatedCost"]
    stage = (
        "현재 단계"
        if card["stageNumber"] == payload["currentStageNumber"]
        else "이미 해금한 이전 단계"
    )
    activity = "다시 연습할 수 있는 활동" if repeat else "미완료 활동"
    reasons = [f"{stage}의 {activity}입니다."]
    signals = payload["signals"]
    if "PREFER_ONE_PAN" in signals and card["onePan"] is True:
        reasons.append("팬 하나로 진행할 수 있습니다.")
    if payload["availableMinutes"] is not None and duration is None:
        reasons.append("예상 시간은 아직 확인되지 않았습니다.")
    elif (
        duration is not None
        and payload["availableMinutes"] is not None
        and duration > payload["availableMinutes"]
    ):
        reasons.append(f"예상 시간은 {duration}분으로 설정한 시간을 초과합니다.")
    elif "PREFER_SHORTER" in signals and duration is not None:
        reasons.append(f"예상 시간은 {duration}분입니다.")
    if scored.budget_status == "OVER":
        reasons.append(f"예상 비용은 {cost:,}원으로 설정한 예산을 초과합니다.")
    elif scored.budget_status == "UNKNOWN":
        reasons.append("예상 비용은 아직 확인되지 않았습니다.")
    elif cost is not None and (
        "PREFER_LOWER_COST" in signals or scored.budget_status == "WITHIN"
    ):
        reasons.append(f"예상 비용은 {cost:,}원입니다.")
    if "PREFER_FEWER_TOOLS" in signals and card["requiredToolCount"] is not None:
        reasons.append(f"필수 도구는 {card['requiredToolCount']}개입니다.")
    return {
        "activityId": card["activityId"],
        "stageId": card["stageId"],
        "stageNumber": card["stageNumber"],
        "title": card["title"],
        "durationMinutes": duration,
        "estimatedCost": cost,
        "budgetStatus": scored.budget_status,
        "isRepeat": repeat,
        "score": scored.score,
        "tier": tier,
        "reason": " ".join(reasons),
    }


def _offline_activity(inactive: bool) -> dict:
    if inactive:
        title = "내 취미 돌아보기"
        instruction = "이 취미에서 기억에 남거나 시도하고 싶은 장면 하나를 떠올려, 그 이유를 한 문장으로 말해 보세요."
    else:
        title = "다음 연습의 첫 동작 정하기"
        instruction = "이 취미에서 해 보고 싶은 일 하나를 고르고, 준비물 없이 지금 할 수 있는 첫 동작을 한 문장으로 말해 보세요."
    return {"kind": "SELF_GUIDED", "title": title, "instruction": instruction}


def rank_home(payload: dict) -> dict:
    """Return a real card, a record to resume, or a standalone offline activity."""
    if payload["treeStatus"] != "ACTIVE":
        return {
            "status": "INACTIVE",
            "resumeRecordId": None,
            "items": [],
            "evaluatedCount": 0,
            "priceFallback": False,
            "alternative": _offline_activity(True),
        }
    if payload["currentRecordId"] is not None:
        return {
            "status": "RESUME",
            "resumeRecordId": payload["currentRecordId"],
            "items": [],
            "evaluatedCount": 0,
            "priceFallback": False,
            "alternative": None,
        }

    eligible = [
        card
        for card in payload["candidates"]
        if card["hobbyId"] == payload["hobbyId"]
        and card["published"]
        and card["startable"]
        and card["stageNumber"] <= payload["currentStageNumber"]
    ]
    if not eligible:
        return {
            "status": "SELF_GUIDED",
            "resumeRecordId": None,
            "items": [],
            "evaluatedCount": 0,
            "priceFallback": False,
            "alternative": _offline_activity(False),
        }

    completed = set(payload["completedActivityIds"])
    new_cards = [card for card in eligible if card["activityId"] not in completed]
    scored = _score_pool(new_cards or eligible, payload)
    price_fallback = payload["budgetKrw"] is not None and all(
        row.budget_status != "WITHIN" for row in scored
    )
    if price_fallback:
        scored.sort(key=_price_order)
    else:
        scored.sort(key=lambda row: (-row.score, not row.previous_after_hard))

    # Both orderings descend through the cutoffs (5-point band boundaries).
    # Lower the active cutoff as soon as a slot cannot be filled at its level.
    limit = payload.get("limit", 2)
    cursor = 0
    items = []
    for cutoff, tier in _TIERS:
        while (
            cursor < len(scored)
            and len(items) < limit
            and scored[cursor].score >= cutoff
        ):
            items.append(_item(scored[cursor], tier, not new_cards, payload))
            cursor += 1
        if cursor == len(scored) or len(items) == limit:
            break
    return {
        "status": "RECOMMENDED",
        "resumeRecordId": None,
        "items": items,
        "evaluatedCount": len(scored),
        "priceFallback": price_fallback,
        "alternative": None,
    }


def recommend(payload: dict) -> dict:
    """Project the shared ranking onto the original v1 response contract."""
    result = rank_home(payload)
    fields = (
        "activityId",
        "stageId",
        "stageNumber",
        "title",
        "durationMinutes",
        "reason",
    )
    return {
        "status": "NO_MATCH" if result["status"] == "SELF_GUIDED" else result["status"],
        "resumeRecordId": result["resumeRecordId"],
        "items": [{key: row[key] for key in fields} for row in result["items"]],
    }
