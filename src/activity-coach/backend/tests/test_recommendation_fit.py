"""추천이 사용자의 진행 상황과 말한 조건을 반영하는지 검증합니다.

실제 카탈로그와 임시 SQLite를 쓰고, 리랭커는 점수를 고정한 대역으로 바꿉니다.
"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.retrieval import Retrieval, excluded_tools_match
from app.turn_budget import TurnBudget
from test_api import app_factory, client, create_session, message, select_activity  # noqa: F401

DATA = Path(__file__).resolve().parents[1] / "data"
BROWSE = "오늘 20분 안에 할 요리 활동 추천해줘"


class RecordingReranker:
    """받은 질문을 기록하고 모든 문서에 같은 점수를 주는 cross-encoder 대역."""

    name = "recording-reranker"

    def __init__(self, score):
        self.fixed, self.queries = score, []

    def score(self, query, documents):
        self.queries.append(query)
        return [self.fixed] * len(documents)


def reranked(tmp_path, score):
    settings = Settings(data_dir=DATA, index_dir=tmp_path / "index", database_path=tmp_path / "coach.sqlite",
                        embedding_enabled=False, reranker_enabled=True, anthropic_api_key="",
                        allowed_hosts=("testserver",))
    reranker = RecordingReranker(score)
    return settings, Retrieval(settings, reranker=reranker), reranker


def ids(reply):
    return [item["activity_id"] for item in reply["activities"]]


def complete_first(client, sid):
    done = ids(message(client, sid, BROWSE, "browse-1"))[0]
    message(client, sid, "1번 할게", "pick-1")
    assert message(client, sid, "활동 완료했어", "done-1")["record"]["created"]
    return done


def test_completed_activity_is_not_offered_again_when_browsing(client):
    sid = create_session(client)
    done = complete_first(client, sid)

    again = message(client, sid, BROWSE, "browse-2")

    assert len(again["activities"]) == 3 and done not in ids(again)
    assert again["diagnostics"]["candidate_ids"] == ids(again)


def test_completed_activity_is_still_found_by_its_topic(client):
    sid = create_session(client)
    select_activity(client, sid, "cook-0-2")
    assert message(client, sid, "활동 완료했어", "done-1")["record"]["created"]

    again = message(client, sid, "간장 드레싱 추천해줘", "topic-1")

    assert "cook-0-2" in ids(again)


def test_other_request_returns_activities_not_shown_before(client):
    sid = create_session(client)
    shown = ids(message(client, sid, BROWSE, "browse-1"))

    other = message(client, sid, "다른 거 추천해줘", "other-1")

    assert len(other["activities"]) == 3 and not set(ids(other)) & set(shown)


def test_other_request_with_new_topic_searches_that_topic(client):
    sid = create_session(client)
    message(client, sid, BROWSE, "browse-1")

    other = message(client, sid, "다른 요리로 된장찌개 추천해줘", "other-1", constraints={"max_minutes": None})

    assert "cook-2-4" in ids(other)


def test_other_request_says_so_when_everything_was_shown(client):
    sid = create_session(client)
    message(client, sid, "된장찌개 추천해줘", "topic-1")
    for turn in range(3):
        last = message(client, sid, "다른 거 추천해줘", f"other-{turn}")
        if not last["activities"]:
            break

    assert not last["activities"] and "이미" in last["text"]


def test_missing_tool_statement_excludes_activities_that_need_it(client):
    sid = create_session(client)

    reply = message(client, sid, "칼이 없어. 할 수 있는 요리 추천해줘", "tool-1")

    session = client.get(f"/sessions/{sid}").json()
    assert session["constraints"]["excluded_tools"] == ["칼"]
    assert reply["activities"] and reply["diagnostics"]["search_mode"] == "catalog_browse"
    assert all(excluded_tools_match(json.dumps(item["tools"]), json.dumps(["칼"])) for item in reply["activities"])


def test_missing_tool_report_during_activity_is_not_a_search_condition(client):
    sid = create_session(client)
    select_activity(client, sid)

    message(client, sid, "계량스푼이 없어", "tool-1")

    assert not client.get(f"/sessions/{sid}").json()["constraints"]["excluded_tools"]


def test_reranker_scores_the_topic_not_the_spoken_request(tmp_path):
    _, retrieval, reranker = reranked(tmp_path, 5.0)

    retrieval.search("오븐 없어. 빵 만들기 추천해줘", constraints={"excluded_tools": ["오븐"]})

    assert reranker.queries == ["빵"]


def test_recommendation_request_without_topic_match_offers_starter_activities(tmp_path):
    settings, retrieval, _ = reranked(tmp_path, -5.0)
    with TestClient(create_app(settings, retrieval=retrieval)) as vague_client:
        sid = create_session(vague_client)

        reply = message(vague_client, sid, "요리 완전 처음인데 뭐부터 하면 좋을까? 추천해줘", "vague-1")

    assert len(reply["activities"]) == 3 and "직접 맞는 활동은 찾지 못" in reply["text"]


def test_unrelated_question_is_not_answered_with_starter_activities(tmp_path):
    settings, retrieval, _ = reranked(tmp_path, -5.0)
    with TestClient(create_app(settings, retrieval=retrieval)) as vague_client:
        sid = create_session(vague_client)

        reply = message(vague_client, sid, "영화 추천해줘", "movie-1")

    assert not reply["activities"]


@pytest.mark.parametrize("text", ["다른 거 추천해 주세요", "다른 거 보여줘", "다른 거 좀 추천해줘", "다른 건 없어?"])
def test_other_request_in_everyday_wording_continues_previous_topic(client, text):
    sid = create_session(client)
    shown = ids(message(client, sid, BROWSE, "browse-1"))

    other = message(client, sid, text, "other-1")

    assert len(other["activities"]) == 3 and not set(ids(other)) & set(shown)
    assert client.get(f"/sessions/{sid}").json()["search_query"] == BROWSE


def test_topic_without_match_does_not_replace_previous_topic(client):
    sid = create_session(client)
    message(client, sid, "된장찌개 추천해줘", "topic-1")

    message(client, sid, "곰팡이 요리 추천해줘", "topic-2")

    assert client.get(f"/sessions/{sid}").json()["search_query"] == "된장찌개 추천해줘"


def test_relaxed_condition_searches_the_topic_that_found_nothing(client):
    """조건 때문에 못 찾은 주제는 조건을 풀면 다시 찾습니다. '시간 제한 없이'라는 문장을 검색어로 쓰지 않습니다."""
    sid = create_session(client)
    assert not message(client, sid, "1분 이내 된장찌개 추천해줘", "tight")["activities"]

    relaxed = message(client, sid, "시간 제한 없이", "relaxed")

    assert ids(relaxed) == ["cook-2-4"]


def test_relaxed_condition_applies_to_the_latest_request_not_an_earlier_topic(client):
    """앞서 찾은 주제가 있어도, 조건만 바꾼 말은 방금 못 찾은 요청에 적용합니다."""
    sid = create_session(client)
    message(client, sid, "달걀 요리 추천해줘", "egg")
    message(client, sid, "1분 이내 된장찌개 추천해줘", "tight")

    relaxed = message(client, sid, "시간 제한 없이", "relaxed")

    assert ids(relaxed) == ["cook-2-4"]


def test_relaxed_condition_after_starter_activities_searches_the_asked_topic(client):
    """주제가 조건에 걸려 입문 목록을 대신 보여 준 뒤에도, 조건을 풀면 물었던 주제를 찾습니다."""
    sid = create_session(client)
    starter = message(client, sid, "20분 안에 된장찌개 추천해줘", "tight")
    assert starter["diagnostics"]["starter_fallback"] and "cook-2-4" not in ids(starter)

    relaxed = message(client, sid, "시간 제한 없이", "relaxed")

    assert ids(relaxed) == ["cook-2-4"]


def test_other_request_after_unmatched_topic_continues_the_matched_topic(client):
    """못 찾은 주제 뒤의 '다른 거'는 못 찾은 주제가 아니라 직전에 찾은 주제를 이어 갑니다."""
    sid = create_session(client)
    shown = ids(message(client, sid, "달걀 요리 추천해줘", "egg"))
    assert not message(client, sid, "양자역학 강의 추천해줘", "none")["activities"]

    other = message(client, sid, "다른 거 추천해줘", "other")

    assert other["activities"] and not set(ids(other)) & set(shown)
    assert all("달걀" in item["title"] for item in other["activities"])


def test_matched_topic_replaces_the_unmatched_one(client):
    """못 찾은 주제는 다음 추천이 성공하면 잊습니다. 이후의 조건 변경은 성공한 주제에 적용합니다."""
    sid = create_session(client)
    message(client, sid, "1분 이내 된장찌개 추천해줘", "tight")
    message(client, sid, "달걀 요리 추천해줘", "egg", constraints={"max_minutes": None})

    narrowed = message(client, sid, "30분 안에", "narrowed")

    assert narrowed["activities"] and all("달걀" in item["title"] for item in narrowed["activities"])


def test_choosing_an_activity_drops_the_unmatched_topic(client):
    """앞 목록에서 활동을 고르면 못 찾은 주제는 접은 것입니다. 이후의 조건 변경은 찾은 주제에 적용합니다."""
    sid = create_session(client)
    message(client, sid, "달걀 요리 추천해줘", "egg")
    message(client, sid, "1분 이내 된장찌개 추천해줘", "tight")
    message(client, sid, "1번 할게", "pick")

    narrowed = message(client, sid, "30분 안에 할 수 있는 거", "narrowed")

    assert narrowed["activities"] and all("달걀" in item["title"] for item in narrowed["activities"])


def test_other_request_without_topic_keeps_the_unmatched_topic(client):
    """못 찾은 직후의 '다른 거'에는 새 주제가 없습니다. 조건을 풀면 못 찾았던 주제를 찾습니다."""
    sid = create_session(client)
    message(client, sid, "1분 이내 된장찌개 추천해줘", "tight")
    assert not message(client, sid, "다른 거 추천해줘", "other")["activities"]

    relaxed = message(client, sid, "시간 제한 없이", "relaxed")

    assert ids(relaxed) == ["cook-2-4"]


def test_topic_missing_from_the_catalog_is_not_retried_when_the_condition_changes(client):
    """조건 없이도 못 찾은 주제는 조건을 바꿔도 못 찾습니다. 조건만 바꾼 말은 직전에 찾은 주제에 적용합니다."""
    sid = create_session(client)
    message(client, sid, "달걀 요리 추천해줘", "egg")
    assert not message(client, sid, "양자역학 강의 추천해줘", "none")["activities"]

    narrowed = message(client, sid, "30분 안에", "narrowed")

    assert narrowed["activities"] and all("달걀" in item["title"] for item in narrowed["activities"])


@pytest.mark.parametrize("constraints, expected", [
    ({"max_minutes": None, "available_tools": None, "excluded_tools": None}, False),
    ({"max_minutes": None, "excluded_tools": []}, False),
    ({"max_minutes": 20, "excluded_tools": None}, True),
    ({"max_minutes": None, "excluded_tools": ["칼"]}, True),
    ({"max_minutes": None, "available_tools": []}, True),
])
def test_only_narrowing_conditions_count_as_conditions(constraints, expected):
    """빈 제외 목록은 아무것도 거르지 않습니다. 빈 사용 가능 도구 목록은 도구가 필요한 활동을 모두 거릅니다."""
    from app.coach import has_conditions

    assert has_conditions(constraints) is expected


def test_starter_fallback_is_skipped_when_retrieval_budget_is_spent(tmp_path):
    settings, retrieval, _ = reranked(tmp_path, -5.0)
    app = create_app(settings, retrieval=retrieval)
    budget = TurnBudget()
    budget.counts["retrieval"] = TurnBudget.limits["retrieval"] - 1
    session = {"session_id": app.state.store.create_session()["session_id"], "hobby_key": "cooking", "constraints": {}}

    items, found, starter = asyncio.run(
        app.state.coach._candidates(session, "요리 완전 처음", "activities", False, True, budget))

    assert not items and not starter and found["diagnostics"]["eligible_documents"]
