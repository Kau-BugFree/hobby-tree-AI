"""History is rebuilt from the BE's current, nondeleted records for a single tree."""

import pytest
from pydantic import ValidationError

from next_activity.absa_models import (
    AnalysisResult,
    GroupedHistoryRequest,
    HistoryRequest,
)
from next_activity.profile import build_group_profile, build_profile


def observation(category, polarity="NEGATIVE", signal=None):
    return {"category": category, "polarity": polarity, "signal": signal}


def record(record_id, timestamp, *aspects, difficulty=None):
    return {
        "userHobbyId": 7,
        "recordId": record_id,
        "completedAt": timestamp,
        "reportedDifficulty": difficulty,
        "aspects": list(aspects),
    }


def snapshot(records):
    return HistoryRequest.model_validate({"userHobbyId": 7, "records": records})


def profile(*records):
    return build_profile(snapshot(list(records))).model_dump()


SHORT = observation("DURATION", signal="PREFER_SHORTER")
COST = observation("COST", signal="PREFER_LOWER_COST")
TOOLS = observation("TOOLS", signal="PREFER_FEWER_TOOLS")
ONE_PAN = observation("COOKING_METHOD", "POSITIVE", "PREFER_ONE_PAN")
UTC = "2026-11-21T09:00:00Z"


def test_first_visit_creates_empty_profile():
    assert profile() == {
        "userHobbyId": 7,
        "recordCount": 0,
        "signals": [],
        "preferredDifficulty": None,
    }


@pytest.mark.parametrize("polarity", ["MIXED", "NEUTRAL", "POSITIVE", "NEGATIVE"])
def test_later_null_clears_only_observed_category(polarity):
    old = record(4, UTC, SHORT, COST, difficulty="HARD")
    new = record(1, "2026-11-22T13:30:00+09:00", observation("DURATION", polarity))
    assert profile(old, new) == {
        "userHobbyId": 7,
        "recordCount": 2,
        "preferredDifficulty": "HARD",
        "signals": ["PREFER_LOWER_COST"],
    }
    assert profile(new, old) == profile(old, new)


def test_deletion_and_edit_recompute_instead_of_accumulating():
    older = record(10, UTC, COST, SHORT, difficulty="HARD")
    newer = record(
        20,
        "2026-11-21T09:01:00Z",
        observation("COST", "NEUTRAL"),
        ONE_PAN,
        difficulty="EASY",
    )
    assert profile(older, newer)["signals"] == ["PREFER_ONE_PAN", "PREFER_SHORTER"]
    assert profile(older, newer)["preferredDifficulty"] == "EASY"
    assert profile(older)["signals"] == ["PREFER_SHORTER", "PREFER_LOWER_COST"]
    assert profile(older)["preferredDifficulty"] == "HARD"
    edited = {**newer, "aspects": [TOOLS], "reportedDifficulty": "APPROPRIATE"}
    assert profile(older, edited)["signals"] == [
        "PREFER_FEWER_TOOLS",
        "PREFER_SHORTER",
        "PREFER_LOWER_COST",
    ]
    assert profile(older, edited)["preferredDifficulty"] == "APPROPRIATE"


def test_timezones_nanoseconds_and_equal_instants():
    older = record(20, "2026-11-21T18:00:00.100000001+09:00", SHORT, difficulty="HARD")
    newer = record(1, "2026-11-21T09:00:00.100000002Z", TOOLS, difficulty="EASY")
    tied_higher_id = record(
        3, "2026-11-21T07:30:00.10000000200-01:30", COST, difficulty="APPROPRIATE"
    )
    assert profile(older, tied_higher_id, newer)["signals"] == [
        "PREFER_LOWER_COST",
        "PREFER_FEWER_TOOLS",
        "PREFER_SHORTER",
    ]
    assert profile(newer, older, tied_higher_id)["preferredDifficulty"] == "APPROPRIATE"


def test_same_record_aspect_order_is_canonical():
    assert profile(record(1, UTC, ONE_PAN, TOOLS, COST, SHORT))["signals"] == [
        "PREFER_SHORTER",
        "PREFER_LOWER_COST",
        "PREFER_FEWER_TOOLS",
        "PREFER_ONE_PAN",
    ]


def test_observed_difficulty_never_invents_direct_user_rating():
    assert (
        profile(record(1, UTC, observation("DIFFICULTY", "NEGATIVE")))[
            "preferredDifficulty"
        ]
        is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        observation("DIFFICULTY", signal="PREFER_SHORTER"),
        observation("DURATION", "MIXED", "PREFER_SHORTER"),
        observation("DURATION", "NEUTRAL", "PREFER_SHORTER"),
        observation("COST", signal="PREFER_FEWER_TOOLS"),
        observation("COOKING_METHOD", "NEGATIVE", "PREFER_ONE_PAN"),
        observation("ENJOYMENT", "POSITIVE", "PREFER_ONE_PAN"),
    ],
)
def test_invalid_actionable_claim_is_rejected(bad):
    with pytest.raises(ValidationError):
        snapshot([record(1, UTC, bad)])


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-21T09:00:00",
        "2026-02-31T09:00:00Z",
        "2026-01-02",
        "2026-01-02T10:12:55+09:70",
        1_800_000_000,
    ],
)
def test_requires_valid_calendar_instant_and_explicit_timezone(timestamp):
    with pytest.raises(ValidationError):
        snapshot([record(1, timestamp)])


def test_rejects_duplicate_ids_duplicate_aspects_and_other_tree():
    first = record(1, UTC, SHORT)
    for rows in [
        [first, first],
        [{**first, "userHobbyId": 8}],
        [record(1, UTC, SHORT, SHORT)],
    ]:
        with pytest.raises(ValidationError):
            snapshot(rows)


def test_history_does_not_accept_memo_or_evidence():
    for item in [
        {**record(1, UTC), "memo": "기록 원문"},
        record(1, UTC, {**SHORT, "evidence": []}),
    ]:
        with pytest.raises(ValidationError):
            snapshot([item])


def test_analysis_checks_nonempty_grounded_spans_and_unique_categories():
    span = {"source": "memo", "text": "⏱️", "start": 3, "end": 5}
    aspect = {**SHORT, "evidence": [span]}
    raw = {"recordId": 1, "reportedDifficulty": None, "aspects": [aspect]}
    AnalysisResult.model_validate(raw)
    for aspects in [
        [aspect, aspect],
        [{**aspect, "evidence": []}],
        [{**aspect, "evidence": [{**span, "end": 4}]}],
    ]:
        with pytest.raises(ValidationError):
            AnalysisResult.model_validate({**raw, "aspects": aspects})


def group_profile(*records):
    body = GroupedHistoryRequest.model_validate(
        {"userHobbyId": 7, "records": list(records)}
    )
    return build_group_profile(body).model_dump()


def test_group_ratings_stay_with_observed_activity_group_and_separate_categories():
    pan = {
        **record(
            1,
            UTC,
            observation("ENJOYMENT", "POSITIVE"),
            observation("DIFFICULTY"),
            SHORT,
        ),
        "activityGroup": "원팬",
    }
    baking = {**record(2, UTC, observation("ENJOYMENT")), "activityGroup": "오븐"}
    ungrouped = record(
        3, UTC, observation("DIFFICULTY", "POSITIVE"), TOOLS, difficulty="EASY"
    )
    result = group_profile(pan, baking, ungrouped)
    assert result["groupPreferences"] == [
        {"activityGroup": "오븐", "category": "ENJOYMENT", "polarity": "NEGATIVE"},
        {"activityGroup": "원팬", "category": "DIFFICULTY", "polarity": "NEGATIVE"},
        {"activityGroup": "원팬", "category": "ENJOYMENT", "polarity": "POSITIVE"},
    ]
    assert result["preferredDifficulty"] == "EASY"
    assert result["signals"] == ["PREFER_FEWER_TOOLS", "PREFER_SHORTER"]
    assert group_profile(ungrouped, baking, pan) == result


@pytest.mark.parametrize("cleared", ["NEUTRAL", "MIXED"])
def test_latest_mention_clears_only_its_group_and_category(cleared):
    older = {
        **record(
            1, UTC, observation("ENJOYMENT", "POSITIVE"), observation("DIFFICULTY")
        ),
        "activityGroup": "A",
    }
    newer = {**record(2, UTC, observation("ENJOYMENT", cleared)), "activityGroup": "A"}
    other = {**record(3, UTC, observation("ENJOYMENT")), "activityGroup": "B"}
    assert group_profile(older, newer, other)["groupPreferences"] == [
        {"activityGroup": "A", "category": "DIFFICULTY", "polarity": "NEGATIVE"},
        {"activityGroup": "B", "category": "ENJOYMENT", "polarity": "NEGATIVE"},
    ]
    assert group_profile(older, other)["groupPreferences"][1] == {
        "activityGroup": "A",
        "category": "ENJOYMENT",
        "polarity": "POSITIVE",
    }
    assert group_profile(older)["preferredDifficulty"] is None


def test_grouped_without_metadata_preserves_v2_results():
    row = record(
        1, UTC, observation("ENJOYMENT", "POSITIVE"), SHORT, COST, difficulty="HARD"
    )
    assert group_profile(row) == {**profile(row), "groupPreferences": []}
    assert group_profile() == {**profile(), "groupPreferences": []}
    with pytest.raises(ValidationError):
        snapshot([{**row, "activityGroup": "A"}])


def test_grouped_records_share_tree_uniqueness_and_strict_group_validation():
    first = {**record(1, UTC), "activityGroup": "A"}
    for records in [
        [first, first],
        [{**first, "userHobbyId": 9}],
        [{**first, "activityGroup": "   "}],
    ]:
        with pytest.raises(ValidationError):
            group_profile(*records)
