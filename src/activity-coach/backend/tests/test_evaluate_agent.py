"""에이전트 판단 평가 스크립트: 집계, 사례 만들기, 맥락, 기대 행동 자료, 가짜 모델 실행."""

import asyncio
import json
from pathlib import Path

import pytest

from app import action_executors, agent
from app.actions import ACTION_NAMES, ACTIONS
from app.config import Settings
from scripts import evaluate_agent
from scripts.evaluate_agent import (COMPARED, CONTEXTS, LABELS, build_sessions, failed, judge, label_cases, main,
                                    make_turn, rule_cases, run, summarize)
from test_api import app_factory  # noqa: F401

BACKEND = Path(__file__).resolve().parents[1]


def entry(set_name: str, *, action: str | None = "notice", status: str = "planned", rule_action: str | None = None,
          expected: list[str] | None = None, must_not_change: bool = False, arguments: dict | None = None,
          rule_arguments: dict | None = None, reason: str | None = None, expected_arguments: dict | None = None,
          latency_ms: int = 100, llm_calls: int = 1) -> dict:
    return {"set": set_name, "context": "none", "text": "말", "status": status, "action": action,
            "arguments": arguments or {}, "reason": reason, "latency_ms": latency_ms, "llm_calls": llm_calls,
            "usage": {"input_tokens": 10, "output_tokens": 2}, "rule_action": rule_action,
            "rule_arguments": rule_arguments or {}, "expected": expected, "expected_arguments": expected_arguments,
            "must_not_change": must_not_change}


def test_summary_counts_agreement_and_violations():
    entries = [entry("rules", action="start_activity", rule_action="start_activity"),
               entry("rules", action="recommend", rule_action="explain"),
               entry("labels", action="complete_activity", expected=["notice"], must_not_change=True),
               entry("labels", action="ask_user", expected=["ask_user", "coach"]),
               entry("labels", status="timeout", action=None, expected=["coach"])]
    summary = summarize(entries)
    assert summary["rule_agreement"] == 0.5 and len(summary["rule_mismatches"]) == 1
    assert len(summary["state_change_violations"]) == 1 and summary["label_agreement"] == 0.5
    assert summary["unanswered"] == 1 and summary["calls"] == 5
    assert summary["usage"] == {"input_tokens": 50, "output_tokens": 10}
    assert summary["latency_ms"] == {"median": 100, "p95": 100}
    assert summary["cases"] == {"rules": 2, "labels": 3}
    assert summary["action_counts"]["start_activity"] == 1


def test_summary_separates_argument_mismatches_and_unusable_decisions():
    entries = [entry("rules", action="select_candidate", rule_action="select_candidate",
                     arguments={"number": 2}, rule_arguments={"number": 3}),
               # 규칙 쪽 coach에는 transition이 없어 비교하지 않는다.
               entry("rules", action="coach", rule_action="coach", arguments={"step": None, "transition": "resolve"},
                     rule_arguments={"step": None}),
               entry("labels", action="answer_confirmation", arguments={"accept": True}, expected=["coach"],
                     must_not_change=True, reason="no_pending_confirmation"),
               entry("labels", action="answer_confirmation", arguments={"accept": False},
                     expected=["answer_confirmation"], must_not_change=True)]
    summary = summarize(entries)
    assert summary["rule_agreement"] == 1.0
    assert [m["rule_arguments"] for m in summary["argument_mismatches"]] == [{"number": 3}]
    assert len(summary["state_change_violations"]) == 1   # 거절(accept=false)은 상태를 바꾸지 않는다
    assert summary["unusable_rate"] == 0.25 and summary["label_agreement"] == 0.5


def test_label_agreement_checks_the_arguments_the_chosen_action_has():
    entries = [entry("labels", action="answer_confirmation", arguments={"accept": False},
                     expected=["answer_confirmation"], expected_arguments={"accept": True}),
               entry("labels", action="answer_confirmation", arguments={"accept": True},
                     expected=["answer_confirmation"], expected_arguments={"accept": True}),
               # ask_user에는 accept가 없어 이름만 본다.
               entry("labels", action="ask_user", arguments={"kind": "unclear"},
                     expected=["ask_user", "answer_confirmation"], expected_arguments={"accept": False}),
               entry("labels", action="select_candidate", arguments={"number": 2}, expected=["select_candidate"],
                     expected_arguments={"number": 3})]
    summary = summarize(entries)
    assert summary["label_agreement"] == 0.5
    assert [m["arguments"] for m in summary["label_mismatches"]] == [{"accept": False}, {"number": 2}]


def test_over_timeout_counts_slow_answers_and_agreement_without_them():
    entries = [entry("rules", action="explain", rule_action="explain", latency_ms=12000),
               entry("rules", action="recommend", rule_action="explain", latency_ms=500),
               entry("labels", action="notice", expected=["notice"], latency_ms=10001),
               entry("labels", action="coach", expected=["notice"], latency_ms=300),
               entry("labels", action="notice", expected=["notice"], latency_ms=10000),
               entry("labels", status="timeout", action=None, expected=["notice"], latency_ms=10500)]
    over = summarize(entries, timeout_seconds=10)["over_timeout"]
    assert over == {"seconds": 10, "count": 2, "rule_agreement": 0.0, "label_agreement": 0.5}


def test_calls_count_the_model_calls_not_the_cases():
    entries = [entry("labels", expected=["notice"], llm_calls=1), entry("labels", expected=["notice"], llm_calls=0),
               entry("labels", status="exception:RuntimeError", action=None, expected=["notice"], llm_calls=0)]
    summary = summarize(entries)
    assert (summary["calls"], summary["cases"]) == (1, {"labels": 3})


def test_more_than_two_percent_unanswered_fails_the_run():
    assert failed({"unanswered": 3, "cases": {"rules": 90, "labels": 10}})
    assert not failed({"unanswered": 2, "cases": {"rules": 90, "labels": 10}})


def test_exception_in_our_code_is_an_error_not_an_unanswered_case():
    entries = [entry("labels", status="exception:KeyError", action="complete_activity", expected=["notice"],
                     must_not_change=True),
               entry("labels", status="timeout", action=None, expected=["notice"]),
               entry("labels", action="notice", expected=["notice"])]
    summary = summarize(entries)
    assert [e["status"] for e in summary["errors"]] == ["exception:KeyError"]
    assert summary["unanswered"] == 1   # 공급자 실패만
    assert summary["state_change_violations"] == [] and "complete_activity" not in summary["action_counts"]
    assert summary["label_agreement"] == 1.0
    assert failed(summary)   # 우리 코드의 예외는 1건이라도 실패
    assert not failed(summarize(entries[2:]))


def test_rule_set_state_changes_are_listed_apart():
    entries = [entry("rules", action="start_activity", rule_action="show_materials"),
               entry("rules", action="start_activity", rule_action="start_activity"),
               entry("rules", action="answer_confirmation", arguments={"accept": True}, rule_action="notice"),
               entry("rules", action="answer_confirmation", arguments={"accept": False}, rule_action="notice"),
               entry("rules", status="exception:KeyError", action="select_candidate", rule_action="recommend"),
               entry("labels", action="complete_activity", expected=["coach"])]
    changes = summarize(entries)["rule_state_changes"]
    assert [(c["rule_action"], c["action"]) for c in changes] == [("show_materials", "start_activity"),
                                                                  ("notice", "answer_confirmation")]


def test_summarize_option_recomputes_a_saved_result_without_calling_claude(tmp_path):
    saved = [entry("labels", status="exception:RuntimeError", action=None, expected=["notice"]),
             entry("labels", action="notice", expected=["notice"])]
    for e in saved:
        e.pop("expected_arguments")   # 이전 결과 파일에는 없을 수 있는 키
    path = tmp_path / "eval.json"
    path.write_text(json.dumps({"summary": {"model": "claude-haiku-5-5", "effort": "low", "unanswered": 1,
                                            "over_timeout": {"seconds": 5}},
                                "entries": saved}, ensure_ascii=False), encoding="utf-8")
    code = main(["--summarize", str(path)])
    written = json.loads(path.read_text(encoding="utf-8"))
    assert code == 1 and written["entries"] == saved
    summary = written["summary"]
    assert (summary["model"], summary["effort"], summary["over_timeout"]["seconds"]) == ("claude-haiku-5-5", "low", 5)
    assert summary["unanswered"] == 0 and len(summary["errors"]) == 1 and summary["label_agreement"] == 1.0


@pytest.fixture
def coach(app_factory):  # noqa: F811
    return app_factory().state.coach


def test_rule_cases_skip_unknown_and_overlap(coach):
    sessions = build_sessions(coach)
    # 요구서의 예 '2번으로 하고 바로 시작할게'는 후보가 없는 두 맥락에서 규칙이 start로 정한다. 두 요청의 예는
    # test_intent_rules.py의 겹침 문장을 쓴다.
    cases = rule_cases(coach, sessions, ["김치볶음밥", "준비물 보여주고 시작할게", "요리 추천해줘", "활동 시작할게",
                                         "땅콩 알레르기 있는데 괜찮아?"])
    found = {(case["context"], case["text"]): case for case in cases}
    assert ("none", "김치볶음밥") not in found
    assert all(text not in {"준비물 보여주고 시작할게", "땅콩 알레르기 있는데 괜찮아?"} for _, text in found)
    assert found[("none", "요리 추천해줘")]["rule_action"] == "recommend"
    assert found[("selected", "활동 시작할게")]["rule_arguments"] == {"restart": False}
    assert all(case["set"] == "rules" and case["expected"] is None for case in cases)


def test_rule_coach_arguments_leave_out_the_step_and_notices_carry_their_kind(coach):
    sessions = build_sessions(coach)
    cases = rule_cases(coach, sessions, ["3단계가 너무 어려워요", "넌 뭘 할 수 있어?"])
    found = {(case["context"], case["text"]): case for case in cases}
    coaching = found[("selected", "3단계가 너무 어려워요")]
    assert (coaching["rule_action"], coaching["rule_arguments"]) == ("coach", {})
    notice = found[("none", "넌 뭘 할 수 있어?")]
    assert (notice["rule_action"], notice["rule_arguments"]) == ("notice", {"kind": "capabilities"})


def test_every_context_builds_without_models(coach):
    sessions = build_sessions(coach)
    assert set(sessions) == set(CONTEXTS)
    contexts = {name: asyncio.run(agent.build_context(coach, make_turn(coach, session, "말")))
                for name, session in sessions.items()}
    assert all(isinstance(context, dict) for context in contexts.values())
    assert contexts["none"]["selected"] is None and contexts["none"]["candidates"] == []
    assert contexts["selected"]["selected"]["activity_id"] == "cook-0-2"
    assert [c["number"] for c in contexts["candidates"]["candidates"]] == [1, 2, 3]
    assert contexts["question_pending"]["dialogue"]["pending_question"]["purpose"] == "clarification"
    assert len(contexts["issue_open"]["dialogue"]["observations"]) == 1
    assert contexts["issue_open"]["dialogue"]["pending_question"] is None
    assert contexts["completion_pending"]["pending_confirmation"]["kind"] == "complete"
    assert contexts["completion_pending"]["selected"]["execution_status"] == "in_progress"
    waiting = contexts["transition_pending"]["dialogue"]["pending_question"]
    assert (waiting["purpose"], waiting["proposed_transition"]) == ("transition", "resolve")


class OneDecision:
    configured = True

    def __init__(self, decision: dict | None, status: str = "planned") -> None:
        self.decision, self.status = decision, status

    async def choose_action(self, text: str, context: dict) -> dict:
        return {"decision": self.decision, "status": self.status, "llm_calls": 1, "usage": {}}


@pytest.mark.parametrize("context, decision, status, reason, arguments", [
    ("none", {"action": "answer_confirmation", "accept": True}, "planned", "no_pending_confirmation",
     {"accept": True}),
    ("completion_pending", {"action": "answer_confirmation", "accept": True}, "planned", None, {"accept": True}),
    ("none", {"action": "select_candidate"}, "planned", "invalid_arguments", {}),
    ("none", {"action": "fly"}, "planned", "unknown_action", {}),
    ("issue_open", {"action": "coach", "transition": "resolve", "step": 3}, "planned", None,
     {"step": 3, "transition": "resolve"}),
    ("none", None, "invalid_decision", "invalid_decision", None),
    ("none", None, "timeout", "timeout", None),
])
def test_judge_applies_the_agent_checks(coach, context, decision, status, reason, arguments):
    coach.llm = OneDecision(decision, status)
    sessions = build_sessions(coach)
    case = {"set": "labels", "context": context, "text": "말", "rule_action": None, "rule_arguments": None,
            "expected": ["coach"], "must_not_change": True}
    result = asyncio.run(judge(coach, sessions, case, asyncio.Semaphore(1)))
    assert (result["status"], result["reason"], result["arguments"]) == (status, reason, arguments)
    assert result["action"] == (decision or {}).get("action")
    assert result["latency_ms"] is not None
    assert result["decision"] == decision   # choose_action이 돌려준 원본 그대로


def test_judge_keeps_the_decision_when_our_check_raises(coach, monkeypatch):
    def broken(action: object, turn: object) -> None:
        raise KeyError("broken check")
    monkeypatch.setattr(agent, "validate", broken)
    decision = {"action": "recommend", "topic": "계란", "excluded_tools": []}
    coach.llm = OneDecision(decision)
    case = {"set": "labels", "context": "none", "text": "말", "rule_action": None, "rule_arguments": None,
            "expected": ["recommend"], "must_not_change": False}
    result = asyncio.run(judge(coach, build_sessions(coach), case, asyncio.Semaphore(1)))
    assert result["status"] == "exception:KeyError" and result["decision"] == decision
    assert summarize([result])["errors"] and summarize([result])["unanswered"] == 0


def test_question_pending_candidates_context(coach):
    sessions = build_sessions(coach)
    context = asyncio.run(agent.build_context(
        coach, make_turn(coach, sessions["question_pending_candidates"], "2번으로 할게요")))
    waiting = context["dialogue"]["pending_question"]
    assert waiting["purpose"] == "clarification" and waiting["options"]
    assert context["selected"]["activity_id"] == "cook-0-2"
    assert [c["number"] for c in context["candidates"]] == [1, 2, 3]
    # 선택한 활동은 후보에 없다. 예전 추천이라 바로 앞 답에서 보여 준 후보가 아니다.
    assert [c["activity_id"] for c in context["candidates"]] == ["cook-0-1", "cook-0-3", "cook-0-4"]
    assert context["candidates_just_shown"] is False


def test_candidates_context_marks_fresh_candidates(coach):
    sessions = build_sessions(coach)
    context = asyncio.run(agent.build_context(coach, make_turn(coach, sessions["candidates"], "말")))
    assert context["candidates_just_shown"] is True
    assert len(context["candidates"]) == 3


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=BACKEND / "data", index_dir=tmp_path / "index", database_path=tmp_path / "db.sqlite",
                    embedding_enabled=False, reranker_enabled=False, anthropic_api_key="")


def test_labels_only_run_makes_no_rule_cases(tmp_path, monkeypatch):
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("규칙 비교 사례를 만들었다")

    monkeypatch.setattr(evaluate_agent, "rule_cases", refuse)
    summary = run(_settings(tmp_path), tmp_path / "eval.json", limit=3, llm=OneDecision(
        {"action": "notice", "kind": "greeting"}), sets=("labels",))
    entries = json.loads((tmp_path / "eval.json").read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 3 and {e["set"] for e in entries} == {"labels"}
    assert summary["sets"] == ["labels"] and summary["cases"] == {"labels": 3}


def test_labels_file_option_reads_another_file(tmp_path, monkeypatch):
    labels = tmp_path / "holdout.jsonl"
    rows = [{"context": "question_pending_candidates", "text": "2번으로 할게요", "expected": ["coach"],
             "must_not_change": True, "note": "질문의 답"},
            {"context": "candidates", "text": "2번으로 할게요", "expected": ["select_candidate"],
             "arguments": {"number": 2}, "must_not_change": False, "note": "새 후보 선택"}]
    labels.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    summary = run(_settings(tmp_path), tmp_path / "eval.json", llm=OneDecision({"action": "show_records"}),
                  labels=labels, sets=("labels",))
    assert summary["cases"] == {"labels": 2} and summary["labels_file"] == "holdout.jsonl"
    assert [case["text"] for case in label_cases(labels)] == ["2번으로 할게요"] * 2
    # 명령줄의 --labels·--sets가 run에 그대로 간다.
    passed = {}

    def fake_run(settings: Settings, out: Path, limit: int | None, concurrency: int, **options: object) -> dict:
        passed.update(options)
        return summary

    monkeypatch.setattr(evaluate_agent, "run", fake_run)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: _settings(tmp_path)))
    main(["--out", str(tmp_path / "x.json"), "--labels", str(labels), "--sets", "labels"])
    assert passed == {"labels": labels, "sets": ("labels",)}


def test_default_run_records_both_sets_and_the_labels_file(tmp_path):
    summary = run(_settings(tmp_path), tmp_path / "eval.json", limit=1, llm=OneDecision({"action": "show_records"}))
    assert summary["sets"] == ["rules", "labels"] and summary["labels_file"] == LABELS.name


def assert_labels_well_formed(path: Path) -> list[dict]:
    """기대 행동 자료 한 파일의 줄 형식을 확인하고 줄들을 돌려줍니다. 어느 자료 파일에나 씁니다."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    fields = {name: set(model.model_fields) - {"action"} for name, model in zip(ACTION_NAMES, ACTIONS)}
    for row in rows:
        assert {"context", "text", "expected", "must_not_change", "note"} <= set(row) <= {
            "context", "text", "expected", "arguments", "must_not_change", "note"}, row
        assert row["context"] in CONTEXTS and row["text"].strip() and row["note"].strip(), row
        assert row["expected"] and set(row["expected"]) <= set(ACTION_NAMES), row
        assert isinstance(row["must_not_change"], bool), row
        arguments = row.get("arguments", {})
        # 인자 이름은 기대 행동 중 하나가 가진 필드이고, 판단 기록에 남는 비교 인자다
        # (coach의 transition은 compared_arguments가 에이전트 쪽에 남긴다).
        assert set(arguments) <= {*COMPARED, "transition"} and all(
            any(key in fields[name] for name in row["expected"]) for key in arguments), row
        if "answer_confirmation" in row["expected"]:
            assert isinstance(arguments.get("accept"), bool), row
        if "select_candidate" in row["expected"]:
            assert isinstance(arguments.get("number"), int), row
    assert len({(row["context"], row["text"]) for row in rows}) == len(rows)
    return rows


def test_labels_file_is_well_formed():
    rows = assert_labels_well_formed(LABELS)
    assert len(rows) >= 80
    assert sum(row.get("arguments", {}).get("accept") is True for row in rows) == 8
    # question_pending_candidates는 이 자료 뒤에 더한 맥락이다(사례는 새 자료 파일에 둔다).
    counts = {name: sum(row["context"] == name for row in rows) for name in CONTEXTS
              if name != "question_pending_candidates"}
    assert min(counts.values()) >= 6, counts


def test_holdout_file_is_well_formed_and_new():
    """지시문을 맞추는 데 쓰지 않은 새 사례. 기존 자료와 발화가 겹치지 않아야 한다."""
    rows = assert_labels_well_formed(BACKEND / "data" / "agent_eval_holdout.jsonl")
    assert len(rows) >= 90
    counts = {name: sum(row["context"] == name for row in rows) for name in CONTEXTS}
    assert min(counts.values()) >= 10, counts
    old_texts = {json.loads(line)["text"] for line in LABELS.read_text(encoding="utf-8").splitlines() if line.strip()}
    assert [row["text"] for row in rows if row["text"] in old_texts] == []
    # coach의 transition 기대값이 평가에서 실제로 비교된다(해결·중단이 핵심인 줄).
    resolve = evaluate_agent.label_cases(BACKEND / "data" / "agent_eval_holdout.jsonl")
    assert any((case["expected_arguments"] or {}).get("transition") == "resolve" for case in resolve)
    assert not evaluate_agent._label_agrees({"reason": None, "action": "coach", "arguments": {"transition": "continue"},
                                             "expected": ["coach"], "expected_arguments": {"transition": "resolve"}})


class FakeAgent:
    """첫 호출은 예외를 내고, 그 뒤로는 인사 안내를 고르는 가짜 모델."""
    configured = True
    model = "fake-model"
    effort = "low"

    def __init__(self) -> None:
        self.calls = 0

    async def choose_action(self, text: str, context: dict) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("provider exploded")
        return {"decision": {"action": "notice", "kind": "greeting"}, "status": "planned", "llm_calls": 1,
                "usage": {"input_tokens": 7, "output_tokens": 3}}


def test_run_records_one_entry_per_case_with_a_fake_model(tmp_path, monkeypatch):
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("실행 함수가 불렸다")

    for name in action_executors.EXECUTORS:
        monkeypatch.setitem(action_executors.EXECUTORS, name, refuse)
    monkeypatch.setattr(action_executors, "execute", refuse)
    monkeypatch.setattr(agent, "decide", refuse)
    checked = []
    validate = agent.validate

    def validate_once_broken(action: object, turn: object) -> str | None:
        checked.append(action)
        if len(checked) == 1:
            raise KeyError("broken check")
        return validate(action, turn)

    monkeypatch.setattr(agent, "validate", validate_once_broken)
    settings = Settings(data_dir=BACKEND / "data", index_dir=tmp_path / "index", database_path=tmp_path / "db.sqlite",
                        embedding_enabled=False, reranker_enabled=False, anthropic_api_key="")
    llm = FakeAgent()
    out = tmp_path / "eval.json"
    partial = tmp_path / "eval.partial.jsonl"
    partial.write_text("지난 실행의 줄\n", encoding="utf-8")
    summary = run(settings, out, limit=5, concurrency=2, llm=llm)
    written = json.loads(out.read_text(encoding="utf-8"))
    entries = written["entries"]
    assert len(entries) == 10 and llm.calls == 10 and len(checked) == 9
    assert [e["set"] for e in entries] == ["rules"] * 5 + ["labels"] * 5
    assert written["summary"] == summary
    assert (summary["model"], summary["effort"], summary["cases"]) == ("fake-model", "low", {"rules": 5, "labels": 5})
    statuses = [e["status"] for e in entries]
    assert statuses.count("exception:RuntimeError") == 1 and statuses.count("exception:KeyError") == 1
    # choose_action의 예외와 검증의 예외는 모두 우리 코드의 예외다(공급자 실패는 상태로 돌아온다).
    assert summary["unanswered"] == 0 and len(summary["errors"]) == 2 and summary["calls"] == 9
    assert failed(summary)
    assert summary["usage"] == {"input_tokens": 63, "output_tokens": 27}
    # 실행을 시작할 때 비우고 사례마다 한 줄씩 덧붙인다. 끝난 뒤에도 남긴다.
    lines = [json.loads(line) for line in partial.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 10
    assert sorted((e["set"], e["text"], e["context"]) for e in lines) == sorted(
        (e["set"], e["text"], e["context"]) for e in entries)
    keys = {"set", "context", "text", "status", "action", "arguments", "latency_ms", "usage", "rule_action",
            "rule_arguments", "expected", "must_not_change", "decision"}
    assert all(keys <= set(e) for e in entries)
