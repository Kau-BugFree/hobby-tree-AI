"""HTTP contract between the Spring backend and the next-activity service."""

import json
from pathlib import Path

import httpx
import pytest

from next_activity.api import create_app


MODULE_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.anyio

SERVICE_TOKEN = "a" * 32
HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
ANALYZE = "/internal/v1/activity-feedback/analyze"
ABSA = "/internal/v2/activity-feedback/analyze"
PROFILE = "/internal/v2/activity-feedback/profile"
RANK = "/internal/v1/next-activities/rank"
HOME = "/internal/v2/next-activities/rank"
GROUPS = "/internal/v3/activity-feedback/profile"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    app = create_app(service_token=SERVICE_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as session:
        yield session


def feedback_payload():
    return {
        "recordId": 401,
        "status": "COMPLETED",
        "memo": "재미있었지만 시간이 오래 걸렸어요. 다음에는 짧은 활동을 하고 싶어요.",
        "satisfactionScore": 4,
        "perceivedDifficulty": "APPROPRIATE",
    }


def candidate(activity_id, minutes):
    return {
        "activityId": activity_id,
        "hobbyId": 10,
        "stageId": 101,
        "stageNumber": 1,
        "title": f"활동 {activity_id}",
        "durationMinutes": minutes,
        "published": True,
        "startable": True,
    }


def rank_payload():
    return {
        "treeStatus": "ACTIVE",
        "currentStageNumber": 1,
        "hobbyId": 10,
        "completedActivityIds": [903],
        "availableMinutes": 20,
        "candidates": [candidate(902, 45), candidate(903, 10), candidate(901, 15)],
    }


async def test_feedback_to_rank(client):
    analyzed = await client.post(ANALYZE, headers=HEADERS, json=feedback_payload())
    assert analyzed.status_code == 200, analyzed.text
    result = analyzed.json()
    assert result["recordId"] == 401
    assert result["difficulty"] == "APPROPRIATE"
    assert result["sentiment"] == "POSITIVE"
    assert "PREFER_SHORTER" in result["signals"]
    assert result["evidence"]

    payload = rank_payload()
    payload["signals"] = result["signals"]
    ranked = await client.post(RANK, headers=HEADERS, json=payload)
    assert ranked.status_code == 200, ranked.text
    response = ranked.json()
    assert response["resumeRecordId"] is None
    assert [item["activityId"] for item in response["items"]] == [901, 902]
    item = response["items"][0]
    assert item["stageId"] == 101
    assert item["stageNumber"] == 1
    assert item["title"] == "활동 901"
    assert item["durationMinutes"] == 15
    assert item["reason"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer incorrect-token"}])
@pytest.mark.parametrize(
    "endpoint,payload", [(ANALYZE, feedback_payload), (RANK, rank_payload)]
)
async def test_internal_routes_require_bearer_token(client, endpoint, payload, headers):
    response = await client.post(endpoint, json=payload(), headers=headers)
    assert response.status_code == 401


async def test_duplicate_candidate_ids_are_rejected(client):
    payload = rank_payload()
    payload["candidates"].append(candidate(901, 12))
    response = await client.post(RANK, headers=HEADERS, json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("stage", [0, 7, "1", True])
@pytest.mark.parametrize("field", ["currentStageNumber", "stageNumber"])
async def test_api_stage_requires_integer_one_to_six(client, stage, field):
    payload = rank_payload()
    if field == "stageNumber":
        payload["candidates"][0][field] = stage
    else:
        payload[field] = stage
    response = await client.post(RANK, headers=HEADERS, json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL"])
@pytest.mark.parametrize("field", ["satisfactionScore", "perceivedDifficulty"])
async def test_completion_needs_explicit_score_and_difficulty(client, status, field):
    payload = feedback_payload()
    payload["status"] = status
    del payload[field]
    response = await client.post(ANALYZE, headers=HEADERS, json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("reason", [None, "", " ", "가" * 201])
async def test_not_done_requires_a_nonblank_reason_within_200_characters(
    client, reason
):
    payload = {"recordId": 402, "status": "NOT_DONE"}
    if reason is not None:
        payload["notDoneReason"] = reason
    response = await client.post(ANALYZE, headers=HEADERS, json=payload)
    assert response.status_code == 422


async def test_not_done_accepts_reason_without_rating(client):
    payload = {
        "recordId": 402,
        "status": "NOT_DONE",
        "notDoneReason": "준비물이 없었어요.",
    }
    response = await client.post(ANALYZE, headers=HEADERS, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["recordId"] == 402


async def test_validation_errors_do_not_expose_feedback_text(client):
    marker = "PRIVATE-MEMO-SENTINEL"
    payload = feedback_payload()
    payload["memo"] = marker * 100
    response = await client.post(ANALYZE, headers=HEADERS, json=payload)
    assert response.status_code == 422
    assert marker not in response.text


async def test_health_is_public(client):
    response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.parametrize("field", ["memo", "notDoneReason"])
async def test_stored_text_must_not_contain_nul(client, field):
    payload = feedback_payload()
    payload[field] = "기록\x00텍스트"
    response = await client.post(ANALYZE, headers=HEADERS, json=payload)
    assert response.status_code == 422


def test_missing_service_token_blocks_startup(monkeypatch):
    monkeypatch.delenv("HOBBYTREE_NEXT_ACTIVITY_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        create_app()


async def test_published_openapi_matches_served_schema(client):
    published = json.loads(
        (MODULE_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8")
    )
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json() == published


async def test_documented_feedback_and_rank_examples(client):
    examples = MODULE_ROOT / "examples"
    feedback = json.loads((examples / "feedback.json").read_text(encoding="utf-8"))
    payload = json.loads((examples / "rank.json").read_text(encoding="utf-8"))
    analyzed = await client.post(ANALYZE, headers=HEADERS, json=feedback)
    assert analyzed.status_code == 200, analyzed.text
    result = analyzed.json()
    assert payload["signals"] == result["signals"]
    assert payload["preferredDifficulty"] == result["difficulty"]
    ranked = await client.post(RANK, headers=HEADERS, json=payload)
    assert ranked.status_code == 200, ranked.text
    assert [item["activityId"] for item in ranked.json()["items"]] == [1005, 1004]


def load_example(name):
    return json.loads((MODULE_ROOT / "examples" / name).read_text(encoding="utf-8"))


async def test_documented_absa_to_profile_to_rank(client):
    feedback = load_example("absa-feedback.json")
    history = load_example("absa-profile.json")
    snapshot = load_example("absa-rank.json")

    response = await client.post(ABSA, headers=HEADERS, json=feedback)
    assert response.status_code == 200, response.text
    analysis = response.json()
    record = history["records"][0]
    assert record["recordId"] == analysis["recordId"]
    assert record["reportedDifficulty"] == analysis["reportedDifficulty"]
    summaries = []
    for aspect in analysis["aspects"]:
        for span in aspect["evidence"]:
            assert feedback[span["source"]][span["start"] : span["end"]] == span["text"]
        summaries.append(
            {key: aspect[key] for key in ("category", "polarity", "signal")}
        )
    assert summaries == record["aspects"]
    record["aspects"] = summaries

    response = await client.post(PROFILE, headers=HEADERS, json=history)
    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["recordCount"] == 2
    for key in ("signals", "preferredDifficulty"):
        assert snapshot[key] == profile[key]
        snapshot[key] = profile[key]
    response = await client.post(RANK, headers=HEADERS, json=snapshot)
    assert response.status_code == 200, response.text
    assert [item["activityId"] for item in response.json()["items"]] == [1005, 1004]


@pytest.mark.parametrize(
    "route,name", [(ABSA, "absa-feedback.json"), (PROFILE, "absa-profile.json")]
)
async def test_absa_routes_use_service_auth(client, route, name):
    assert (await client.post(route, json=load_example(name))).status_code == 401


async def test_profile_rejects_unredacted_records_without_echoing_memo(client):
    payload = load_example("absa-profile.json")
    payload["records"][0]["memo"] = "private record content"
    response = await client.post(PROFILE, headers=HEADERS, json=payload)
    assert response.status_code == 422
    assert "private record content" not in response.text


async def test_unrated_record_does_not_promote_inferred_difficulty(client):
    response = await client.post(
        ABSA,
        headers=HEADERS,
        json={
            "recordId": 402,
            "status": "NOT_DONE",
            "notDoneReason": "너무 어려웠어요.",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["reportedDifficulty"] is None
    assert [(a["category"], a["polarity"], a["signal"]) for a in result["aspects"]] == [
        ("DIFFICULTY", "NEGATIVE", None)
    ]
    assert all(e["source"] == "notDoneReason" for e in result["aspects"][0]["evidence"])


async def test_grouped_feedback_to_full_home_pool(client):
    history = load_example("group-profile.json")
    completed = feedback_payload()
    completed.update(
        recordId=405, memo="재미있었지만 어려웠어요.", perceivedDifficulty="APPROPRIATE"
    )
    analyzed = await client.post(ABSA, headers=HEADERS, json=completed)
    assert analyzed.status_code == 200, analyzed.text
    result = analyzed.json()
    compact = [
        {k: v for k, v in a.items() if k != "evidence"} for a in result["aspects"]
    ]
    assert history["records"][0]["aspects"] == compact
    history["records"][0]["aspects"] = compact
    profile = await client.post(GROUPS, headers=HEADERS, json=history)
    assert profile.status_code == 200, profile.text

    payload = load_example("fallback-rank.json")
    for key in ("signals", "preferredDifficulty", "groupPreferences"):
        assert profile.json()[key] == payload[key]
        payload[key] = profile.json()[key]
    ranked = await client.post(HOME, headers=HEADERS, json=payload)
    assert ranked.status_code == 200, ranked.text
    home = ranked.json()
    assert home["status"] == "RECOMMENDED" and home["priceFallback"]
    assert home["evaluatedCount"] == 4
    assert [
        (c["activityId"], c["estimatedCost"], c["score"]) for c in home["items"]
    ] == [(1004, 6500, 88), (1005, 9000, 88)]
    assert all(c["budgetStatus"] == "OVER" and not c["isRepeat"] for c in home["items"])


@pytest.mark.parametrize(
    "tree,status",
    [("ACTIVE", "SELF_GUIDED"), ("PAUSED", "INACTIVE"), ("ENDED", "INACTIVE")],
)
async def test_home_without_catalog_has_displayable_activity(client, tree, status):
    payload = rank_payload()
    payload.update(treeStatus=tree, candidates=[])
    response = await client.post(HOME, headers=HEADERS, json=payload)
    assert response.status_code == 200, response.text
    home = response.json()
    assert (
        home["status"] == status and not home["items"] and home["evaluatedCount"] == 0
    )
    assert (
        home["alternative"]["kind"] == "SELF_GUIDED"
        and home["alternative"]["instruction"]
    )
    assert "activityId" not in home["alternative"]


@pytest.mark.parametrize(
    "route,name", [(HOME, "fallback-rank.json"), (GROUPS, "group-profile.json")]
)
async def test_home_and_grouped_history_require_service_auth(client, route, name):
    assert (await client.post(route, json=load_example(name))).status_code == 401


async def test_home_rejects_conflicting_duplicates_for_one_group(client):
    payload = load_example("fallback-rank.json")
    payload["groupPreferences"].append(
        dict(payload["groupPreferences"][0], polarity="POSITIVE")
    )
    assert (await client.post(HOME, headers=HEADERS, json=payload)).status_code == 422
