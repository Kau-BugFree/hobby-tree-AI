"""분류 결과를 처리 함수로 보내는 연결: 진단 기록과 단계 이동."""
import pytest
from fastapi.testclient import TestClient

from test_api import app_factory, create_session, message, select_activity  # noqa: F401


@pytest.fixture
def client(app_factory):
    with TestClient(app_factory()) as test_client:
        yield test_client


def test_reply_records_intent_category_and_rule(client):
    sid = create_session(client)

    reply = message(client, sid, "안녕", "greet-1")

    assert reply["diagnostics"]["intent_category"] == "notice"
    assert reply["diagnostics"]["intent_rule"] == "greeting"
    assert reply["diagnostics"]["intent"] == "greeting"


def test_unclassified_reply_records_unknown_category(client):
    sid = create_session(client)

    reply = message(client, sid, "음", "unknown-1")

    assert reply["diagnostics"]["intent_category"] == "unknown"
    assert reply["diagnostics"]["intent_rule"] is None


def test_next_step_request_moves_current_step(client):
    sid = create_session(client)
    select_activity(client, sid)

    reply = message(client, sid, "다음 단계 알려줘", "next-1")

    assert reply["current_step"] == 2


def test_next_word_without_step_does_not_move_current_step(client):
    sid = create_session(client)
    select_activity(client, sid)

    reply = message(client, sid, "다음 재료 알려줘", "materials-1")

    assert reply["diagnostics"]["intent"] == "activity_lookup"
    assert reply["current_step"] == 1


def test_every_rule_kind_converts_to_an_executable_action():
    from app import intent_rules
    from app.action_executors import EXECUTORS
    from app.actions import ACTION_NAMES
    from app.rule_arguments import CONVERTERS

    kinds = {rule.kind for rule in intent_rules.RULES if rule.kind is not None}
    kinds |= {intent_rules.CONDITION_ONLY.kind, intent_rules.OVERLAP.kind}

    assert kinds <= CONVERTERS.keys(), kinds - CONVERTERS.keys()
    assert set(ACTION_NAMES) <= EXECUTORS.keys(), set(ACTION_NAMES) - EXECUTORS.keys()


def test_registry_hobby_without_vocabulary_fails_at_startup(app_factory, monkeypatch):
    from app import vocabulary

    monkeypatch.delitem(vocabulary.VOCABULARY, "cooking")

    with pytest.raises(RuntimeError, match="cooking"):
        app_factory()


OVERLAP = "요청이 두 가지로 읽혀요. 한 번에 한 가지씩 말씀해 주세요."


def test_lookup_and_start_together_is_asked_back_without_model(client):
    from app.intent_handlers import OVERLAP_TEXT

    sid = create_session(client)
    select_activity(client, sid)
    before = client.get(f"/sessions/{sid}").json()

    reply = message(client, sid, "준비물 보여주고 시작할게", "both-1")

    assert OVERLAP_TEXT == OVERLAP
    assert reply["mode"] == "clarify" and reply["text"] == OVERLAP
    assert reply["diagnostics"]["intent"] == "ambiguous"
    assert reply["diagnostics"]["stop_reason"] == "ambiguous_intent"
    assert reply["diagnostics"]["intent_category"] == "overlap"
    assert reply["diagnostics"]["intent_rule"] == "overlap"
    assert reply["diagnostics"]["search_performed"] is False
    assert reply["execution_id"] == before["execution_id"]
    after = client.get(f"/sessions/{sid}").json()
    assert {k: v for k, v in after.items() if k not in {"messages", "updated_at", "revision"}} == {
        k: v for k, v in before.items() if k not in {"messages", "updated_at", "revision"}}


def test_candidate_number_with_start_is_asked_back_without_model(client):
    # 후보 번호를 말한 시작은 시작 규칙에서 빠져 겹침이 아니다. 어느 규칙에도 맞지 않는 말(unknown)로 되묻는다.
    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "list-1")

    reply = message(client, sid, "2번으로 하고 바로 시작할게", "both-2")

    assert reply["mode"] == "clarify"
    assert reply["selected_activity_id"] is None
    assert reply["diagnostics"]["intent_category"] == "unknown"
    assert reply["text"] != OVERLAP


class NoInterpretation:
    """해석 모델이 있지만 불리면 안 되는 가짜 모델."""
    configured = True
    model = "fake"

    async def interpret_turn(self, text: str, context: dict) -> dict:
        raise AssertionError("interpret_turn must not be called for an overlap")

    async def decide(self, question: str, context: dict, evidence: list) -> dict:
        raise AssertionError("decide must not be called for an overlap")


def test_overlap_is_asked_back_with_a_model_without_calling_it(app_factory):
    with TestClient(app_factory(llm=NoInterpretation(), api_key="x")) as model_client:
        sid = create_session(model_client)
        select_activity(model_client, sid)

        reply = message(model_client, sid, "준비물 보여주고 시작할게", "both-3")

        assert reply["text"] == OVERLAP and reply["diagnostics"]["intent_category"] == "overlap"
        assert reply["execution_id"] is None and reply["diagnostics"]["llm_calls"] == 0


def test_deictic_without_selection_asks_which_activity(client):
    sid = create_session(client)

    reply = message(client, sid, "이거 어떻게 해?", "deictic-1")

    assert reply["mode"] == "clarify"
    assert reply["text"] == "어느 활동을 말하는지 알 수 없어요. 추천 목록의 번호나 활동 이름으로 먼저 선택해 주세요."


def test_unclear_sentence_without_model_does_not_search(client):
    from app.intent_handlers import UNCLEAR_UNSELECTED

    sid = create_session(client)
    message(client, sid, "요리 활동 추천해줘", "list-2")
    candidates = client.get(f"/sessions/{sid}").json()["candidate_ids"]

    reply = message(client, sid, "음 글쎄 그게", "unclear-1")

    assert reply["text"] == UNCLEAR_UNSELECTED
    assert reply["diagnostics"]["search_performed"] is False
    assert client.get(f"/sessions/{sid}").json()["candidate_ids"] == candidates
