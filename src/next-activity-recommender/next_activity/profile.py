"""Build a new recommendation profile from the caller's current tree history."""

from collections.abc import Sequence
from typing import get_args

from .absa_models import (
    CATEGORY_ORDER,
    GroupCategory,
    GroupedHistoryRequest,
    GroupedProfileResult,
    GroupPolarity,
    GroupPreference,
    HistoryRecord,
    HistoryRequest,
    ProfileResult,
)


def _profile_from_newest(
    user_hobby_id: int, records: Sequence[HistoryRecord]
) -> ProfileResult:
    reported_difficulty = None
    seen_categories = set()
    signals = []
    for record in records:
        if reported_difficulty is None:
            reported_difficulty = record.reportedDifficulty
        for aspect in sorted(
            record.aspects, key=lambda item: CATEGORY_ORDER.index(item.category)
        ):
            if aspect.category in seen_categories:
                continue
            # The latest observation supersedes the earlier one, including a null signal.
            seen_categories.add(aspect.category)
            if aspect.signal is not None:
                signals.append(aspect.signal)

    return ProfileResult(
        userHobbyId=user_hobby_id,
        recordCount=len(records),
        preferredDifficulty=reported_difficulty,
        signals=signals,
    )


def build_profile(body: HistoryRequest) -> ProfileResult:
    records = sorted(body.records, key=HistoryRecord.chronology_key, reverse=True)
    return _profile_from_newest(body.userHobbyId, records)


def build_group_profile(body: GroupedHistoryRequest) -> GroupedProfileResult:
    records = sorted(body.records, key=HistoryRecord.chronology_key, reverse=True)
    base = _profile_from_newest(body.userHobbyId, records)
    latest = {}
    for record in records:
        if record.activityGroup is None:
            continue
        for aspect in record.aspects:
            if aspect.category in get_args(GroupCategory):
                # Keep a recent neutral/mixed observation so older opinions cannot reappear.
                latest.setdefault(
                    (record.activityGroup, aspect.category), aspect.polarity
                )

    group_preferences = [
        GroupPreference(activityGroup=group, category=category, polarity=polarity)
        for (group, category), polarity in sorted(latest.items())
        if polarity in get_args(GroupPolarity)
    ]
    return GroupedProfileResult(**base.model_dump(), groupPreferences=group_preferences)
