"""에이전트의 판단만 구해(실행 함수는 부르지 않고) 규칙·기대 행동과 비교합니다(설계 7절).

실행: python -B scripts/evaluate_agent.py --out var/agent-entry/eval.json [--limit N] [--concurrency 4]
      [--labels FILE] [--sets rules,labels|rules|labels]
요약만 다시: python -B scripts/evaluate_agent.py --summarize var/agent-entry/eval.json (Claude를 부르지 않는다)
설정은 Settings.from_env()이고, 검색 모델을 끄고 DB와 색인은 임시 폴더에 만듭니다. 실제 Claude를 부릅니다.
사례 두 묶음: 규칙 비교(utterances() × none·selected)와 기대 행동(--labels, 기본 data/agent_eval_labels.jsonl).
"""
import argparse
import asyncio
import copy
import json
import math
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app import agent, confirmation  # noqa: E402
from app.action_executors import Turn  # noqa: E402
from app.actions import ACTION_NAMES, ACTIONS, Action, parse_decision  # noqa: E402
from app.coach import Coach  # noqa: E402
from app.config import Settings  # noqa: E402
from app.dialogue import DialogueCoach  # noqa: E402
from app.dialogue_models import DialogueState, ObservationUpdate, RequirementProposal, TurnInterpretation  # noqa: E402
from app.dialogue_state import apply_transition, serialize_dialogue  # noqa: E402
from app.intent_handlers import context_for  # noqa: E402
from app.intent_rules import classify  # noqa: E402
from app.main import create_app  # noqa: E402
from app.rule_arguments import to_action  # noqa: E402
from app.turn_budget import TurnBudget  # noqa: E402
from scripts.intent_baseline import utterances  # noqa: E402

LABELS = BACKEND / "data" / "agent_eval_labels.jsonl"
ACTIVITIES = BACKEND / "data" / "activities.jsonl"
CONTEXTS = ("none", "selected", "candidates", "question_pending", "question_pending_candidates", "issue_open",
            "completion_pending", "transition_pending")
SETS = ("rules", "labels")
SELECTED_ACTIVITY = "cook-0-2"   # 계량해서 간장 드레싱 만들기(4단계). 3단계가 '설탕이 녹을 때까지 저어요'다.
ISSUE_TEXT = "설탕이 잘 안 녹아요"
ISSUE_STEP = 3
CLARIFICATION = "설탕을 넣고 얼마나 저으셨나요?"
CLARIFICATION_OPTIONS = ["1분 미만", "1분 이상", "잘 모르겠어요"]
PROPOSAL_TEXT = "이제 좀 녹는 것 같아요"
# question_pending_candidates의 예전 후보. 선택한 활동(cook-0-2)은 넣지 않는다.
OLD_CANDIDATES = ("cook-0-1", "cook-0-3", "cook-0-4")
# 비교하는 인자: 후보 번호, 단계, 이동, 시간, 제외 도구, 확인 수락 여부, 안내·되묻기 종류, 다시 시작.
# coach의 transition은 에이전트 쪽에만, coach의 step은 에이전트 쪽에만 있다(규칙은 일부 규칙에서만 채운다).
COMPARED = ("number", "candidate_numbers", "step", "move", "max_minutes", "clear_time_limit", "excluded_tools",
            "accept", "kind", "restart")
FIELDS = {name: set(model.model_fields) for name, model in zip(ACTION_NAMES, ACTIONS)}
STATE_CHANGES = frozenset({"select_candidate", "start_activity", "complete_activity"})
# 공급자가 답하지 못한 상태. 키가 없는 것(not_configured)도 판단을 못 받은 것이다.
UNANSWERED = agent.UNANSWERED | {"not_configured"}
UNANSWERED_LIMIT = 0.02
PROGRESS_EVERY = 50


def make_turn(coach: Coach, session: dict, text: str) -> Turn:
    """Coach._respond_locked가 만드는 것과 같은 Turn. 분류 맥락은 Claude가 해석할 수 있다고 둡니다."""
    selected = coach.activity(session["selected_activity_id"]) if session["selected_activity_id"] else None
    response = {"message_id": "eval-message", "session_id": session["session_id"], "mode": "direct", "text": "",
                "activities": [], "sources": [], "actions": [],
                "diagnostics": {"intent": "unknown", "search_performed": False, "search_mode": "none",
                                "llm_calls": 0, "llm_status": "not_needed"}}
    return Turn(text, session, response, selected, TurnBudget(), "eval-request", "eval-user", None,
                context_for(session, selected, False, True))


def _started(coach: Coach, step: int) -> dict:
    """cook-0-2를 선택하고 수행을 시작한 세션."""
    session = coach.store.create_session("cooking")
    activity = coach.select(session, SELECTED_ACTIVITY)
    execution, _ = coach.store.start_execution(session["session_id"], activity, f"eval-{session['session_id']}")
    coach.attach_execution(session, execution)
    session["current_step"] = step
    return session


def _issue(session: dict) -> DialogueState:
    """관찰 하나('설탕이 잘 안 녹아요')가 있는 열린 문제."""
    interpretation = TurnInterpretation(
        route="dialogue", observation_updates=[ObservationUpdate(quote=ISSUE_TEXT)],
        requirement_proposals=[RequirementProposal(text="설탕을 녹이는 방법", kind="remedy", source_quote=ISSUE_TEXT)])
    state, _ = apply_transition(DialogueState(), interpretation, "eval-issue", ISSUE_TEXT,
                                session["selected_activity_id"], session["execution_id"])
    state.step_number = ISSUE_STEP
    return state


def _just_recommended(coach: Coach, session: dict, ids: list[str]) -> None:
    """바로 앞 코치 답이 ids를 보여 준 추천인 세션으로 만듭니다(추천 한 턴을 저장한 것과 같은 모양)."""
    message_id = "eval-recommendation"
    text = "다음 활동을 찾았어요. 번호로 선택하면 준비물과 순서를 이어서 안내할게요.\n" + "\n".join(
        f"{number}. {coach.activity(aid)['title']}" for number, aid in enumerate(ids, 1))
    response = {"message_id": message_id, "text": text, "selected_activity_id": None, "execution_id": None}
    session.update(candidate_ids=list(ids), shown_ids=list(ids), candidate_selection_pending=True,
                   last_recommendation={"message_id": message_id, "query": "", "scope": "activities",
                                        "constraints": dict(session["constraints"]), "candidate_ids": list(ids),
                                        "ranking": [], "search_mode": "none"})
    session["messages"] = [{"message_id": "eval-recommendation-request", "role": "user", "text": "요리 추천해줘"},
                           {"message_id": message_id, "role": "assistant", "text": text, "response": response}]


def build_sessions(coach: Coach) -> dict[str, dict]:
    """맥락 여덟 가지의 세션. 앞 턴을 실행하지 않고 값을 직접 채웁니다."""
    none = coach.store.create_session("cooking")
    selected = coach.store.create_session("cooking")
    coach.select(selected, SELECTED_ACTIVITY)
    candidates = coach.store.create_session("cooking")
    first = [json.loads(line)["activity_id"] for line in ACTIVITIES.read_text(encoding="utf-8").splitlines()[:3]]
    _just_recommended(coach, candidates, first)
    sessions = {"none": none, "selected": selected, "candidates": candidates}
    for name in ("question_pending", "question_pending_candidates", "issue_open", "transition_pending"):
        session = _started(coach, ISSUE_STEP)
        state = _issue(session)
        if name.startswith("question_pending"):
            DialogueCoach._question(state, {}, CLARIFICATION, CLARIFICATION_OPTIONS)
        if name == "question_pending_candidates":
            # 예전에 받은 후보(선택한 활동은 없다)가 남아 있다. 바로 앞 답에서 보여 준 후보가 아니다.
            session.update(candidate_ids=list(OLD_CANDIDATES), shown_ids=list(OLD_CANDIDATES),
                           candidate_selection_pending=False)
        elif name == "transition_pending":
            DialogueCoach._propose_closing(state, {}, PROPOSAL_TEXT, "eval-proposal", "resolve")
        session["dialogue"] = serialize_dialogue(state)
        sessions[name] = session
    waiting = _started(coach, 4)
    confirmation.ask(make_turn(coach, waiting, ""))
    sessions["completion_pending"] = waiting
    return sessions


def compared_arguments(action: Action, rule: bool = False) -> dict:
    """비교할 인자만 뽑습니다. 규칙 경로의 코칭은 transition을 정하지 않으므로 규칙 쪽에는 넣지 않습니다."""
    values = action.model_dump()
    found = {key: values[key] for key in COMPARED if key in values}
    for key in ("candidate_numbers", "excluded_tools"):
        if key in found:
            found[key] = sorted(found[key])
    if action.action == "coach" and rule:
        found.pop("step")
    elif action.action == "coach":
        found["transition"] = action.transition
    return found


def rule_cases(coach: Coach, sessions: dict[str, dict], texts: list[str]) -> list[dict]:
    """규칙이 행동 하나로 정한 발화. 판정 못 함, 두 요청, 알레르기(에이전트 앞에서 규칙이 처리)는 뺍니다."""
    cases = []
    for text in texts:
        for name in ("none", "selected"):
            turn = make_turn(coach, copy.deepcopy(sessions[name]), text)
            intent = classify(text, turn.context)
            if intent is None or intent.kind == "overlap" or intent.rule == "allergy":
                continue
            action, _ = to_action(intent, turn)
            cases.append({"set": "rules", "context": name, "text": text, "rule_action": action.action,
                          "rule_arguments": compared_arguments(action, rule=True), "expected": None,
                          "expected_arguments": None, "must_not_change": False})
    return cases


def label_cases(path: Path = LABELS) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [{"set": "labels", "context": row["context"], "text": row["text"], "rule_action": None,
             "rule_arguments": None, "expected": row["expected"], "expected_arguments": row.get("arguments"),
             "must_not_change": row["must_not_change"]}
            for row in rows]


def _read_decision(entry: dict, raw: object, turn: Turn) -> None:
    """agent.decide와 같은 순서로 형식 → 인자 검증 → 인용 걸러 내기를 적용합니다. 쓸 수 없으면 reason에 사유."""
    if not isinstance(raw, dict):
        entry["reason"] = agent._rejected(raw)
        return
    entry["action"] = raw.get("action")
    entry["arguments"] = {key: raw[key] for key in COMPARED if key in raw}
    action = parse_decision(raw)
    if action is None:
        entry["reason"] = agent._rejected(raw)
        return
    entry["reason"] = agent.validate(action, turn)
    if action.action == "coach":
        action = agent._trimmed(turn, action)
    entry["arguments"] = compared_arguments(action)


async def judge(coach: Coach, sessions: dict[str, dict], case: dict, gate: asyncio.Semaphore) -> dict:
    """사례 하나에 choose_action을 한 번 부릅니다. 판정까지의 예외는 status에 적고 전체 실행을 멈추지 않습니다.

    decision은 choose_action이 돌려준 행동 dict 원본입니다(topic·target·인용까지 남긴다).
    """
    entry = {**case, "status": None, "decision": None, "action": None, "arguments": None, "reason": None,
             "latency_ms": None, "llm_calls": 0, "usage": {}}
    async with gate:
        try:
            turn = make_turn(coach, copy.deepcopy(sessions[case["context"]]), case["text"])
            context = await agent.build_context(coach, turn)
            started = time.perf_counter()
            result = await coach.llm.choose_action(case["text"], context)
            entry["latency_ms"] = round((time.perf_counter() - started) * 1000)
            entry.update(status=result.get("status"), llm_calls=result.get("llm_calls", 1),
                         usage=result.get("usage") or {})
            raw = entry["decision"] = result.get("decision")
            if raw is None:
                entry["reason"] = entry["status"] or "invalid_decision"
            else:
                _read_decision(entry, raw, turn)
        except Exception as error:  # noqa: BLE001  사례 하나의 실패는 그 사례의 결과로만 남긴다
            entry.update(status=f"exception:{type(error).__name__}", reason=None)
    return entry


def partial_path(out: Path) -> Path:
    """out과 같은 이름의 중간 결과 파일(사례가 끝날 때마다 한 줄)."""
    return out.with_name(f"{out.stem}.partial.jsonl")


async def evaluate(coach: Coach, sessions: dict[str, dict], cases: list[dict], concurrency: int,
                   partial: Path) -> list[dict]:
    """사례마다 판단을 구하고, 끝난 사례를 partial에 한 줄씩 덧붙입니다. partial은 시작할 때 비웁니다."""
    gate = asyncio.Semaphore(concurrency)
    finished = 0
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("", encoding="utf-8")

    async def one(case: dict) -> dict:
        nonlocal finished
        result = await judge(coach, sessions, case, gate)
        # 한 번의 쓰기로 한 줄을 덧붙인다. 이벤트 루프 하나에서 돌아 줄이 섞이지 않는다.
        with partial.open("a", encoding="utf-8") as file:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
        finished += 1
        if finished % PROGRESS_EVERY == 0:
            sys.stderr.write(f"{finished}/{len(cases)} cases\n")
        return result

    return list(await asyncio.gather(*(one(case) for case in cases)))


def unanswered(entry: dict) -> bool:
    """공급자가 답하지 못한 사례(choose_action이 돌려준 상태)."""
    return (entry["status"] or "") in UNANSWERED


def errored(entry: dict) -> bool:
    """판단을 구하거나 읽는 우리 코드에서 예외가 난 사례. 실제 서비스라면 500이다."""
    return (entry["status"] or "").startswith("exception:")


def _state_change(action: str | None, arguments: dict | None) -> bool:
    return action in STATE_CHANGES or (action == "answer_confirmation" and (arguments or {}).get("accept") is True)


def _changes_state(entry: dict) -> bool:
    return _state_change(entry["action"], entry["arguments"])


def _rule_state_change(entry: dict) -> bool:
    """규칙의 행동은 상태 변경이 아닌데 에이전트가 상태를 바꾸는 행동을 고른 규칙 묶음 사례."""
    return (entry["set"] == "rules" and _changes_state(entry)
            and not _state_change(entry["rule_action"], entry["rule_arguments"]))


def _ratio(items: list[dict], test) -> float | None:
    return round(sum(map(test, items)) / len(items), 4) if items else None


def _brief(entry: dict) -> dict:
    keys = ("set", "context", "text", "status", "decision", "action", "arguments", "reason", "rule_action",
            "rule_arguments", "expected", "expected_arguments")
    return {key: entry.get(key) for key in keys}


def _rule_agrees(entry: dict) -> bool:
    return entry["reason"] is None and entry["action"] == entry["rule_action"]


def _label_agrees(entry: dict) -> bool:
    """고른 행동이 expected에 있고, 기대 인자 중 그 행동이 가진 인자는 값이 같아야 일치입니다."""
    if entry["reason"] is not None or entry["action"] not in entry["expected"]:
        return False
    chosen = FIELDS[entry["action"]]
    return all((entry["arguments"] or {}).get(key) == value
               for key, value in (entry.get("expected_arguments") or {}).items() if key in chosen)


def _over_timeout(answered: list[dict], timeout_seconds: float) -> dict:
    """실제 경로에서 시간 초과로 규칙 경로에 갈 판단의 수와, 그 판단을 뺀 일치율."""
    limit_ms = timeout_seconds * 1000
    within = [e for e in answered if e["latency_ms"] is None or e["latency_ms"] <= limit_ms]
    return {"seconds": timeout_seconds, "count": len(answered) - len(within),
            "rule_agreement": _ratio([e for e in within if e["set"] == "rules"], _rule_agrees),
            "label_agreement": _ratio([e for e in within if e["set"] == "labels"], _label_agrees)}


def summarize(entries: list[dict], timeout_seconds: float = 10.0) -> dict:
    """수용 기준의 수치(설계 7절). 비율은 답하지 못한 사례와 예외 사례를 빼고 셉니다.

    예외 사례는 errors에만 들어가고 위반·행동 수에는 넣지 않습니다.
    """
    judged = [e for e in entries if not errored(e)]
    answered = [e for e in judged if not unanswered(e)]
    rules = [e for e in answered if e["set"] == "rules"]
    labels = [e for e in answered if e["set"] == "labels"]
    latencies = sorted(e["latency_ms"] for e in entries if e["latency_ms"] is not None)
    usage: Counter = Counter()
    for e in entries:
        usage.update(e["usage"])
    return {
        "cases": dict(Counter(e["set"] for e in entries)),
        "rule_agreement": _ratio(rules, _rule_agrees),
        "rule_mismatches": [_brief(e) for e in rules if not _rule_agrees(e)],
        "argument_mismatches": [_brief(e) for e in rules if _rule_agrees(e) and any(
            (e["arguments"] or {}).get(key) != value for key, value in e["rule_arguments"].items())],
        "state_change_violations": [_brief(e) for e in judged if e["must_not_change"] and _changes_state(e)],
        "rule_state_changes": [_brief(e) for e in judged if _rule_state_change(e)],
        "label_agreement": _ratio(labels, _label_agrees),
        "label_mismatches": [_brief(e) for e in labels if not _label_agrees(e)],
        "unusable_rate": _ratio(answered, lambda e: e["reason"] is not None),
        "unanswered": len(judged) - len(answered),
        "errors": [_brief(e) for e in entries if errored(e)],
        "over_timeout": _over_timeout(answered, timeout_seconds),
        "latency_ms": {"median": statistics.median(latencies) if latencies else None,
                       "p95": latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None},
        "calls": sum(e["llm_calls"] for e in entries),
        "usage": dict(usage),
        "action_counts": dict(Counter(e["action"] for e in judged if e["action"])),
    }


def failed(summary: dict) -> bool:
    """우리 코드의 예외가 1건이라도 있거나, 답하지 못한 사례가 2%를 넘으면 참입니다."""
    return bool(summary.get("errors")) or summary["unanswered"] > UNANSWERED_LIMIT * sum(summary["cases"].values())


def run(settings: Settings, out: Path, limit: int | None = None, concurrency: int = 4, llm: object = None,
        labels: Path = LABELS, sets: tuple[str, ...] = SETS) -> dict:
    """사례마다 판단을 구해 out에 {"summary", "entries"}를 씁니다. limit은 묶음마다 앞에서부터 셉니다.

    sets에 없는 묶음은 사례를 만들지 않습니다. labels는 기대 행동 자료의 경로입니다.
    끝난 사례는 그때마다 partial_path(out)에 한 줄씩 남습니다(중단되어도 그때까지의 판단이 남는다).
    """
    # Windows에서는 앱이 연 sqlite 핸들이 남아 임시 폴더 삭제가 실패할 수 있어 삭제 오류를 무시한다.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = replace(settings, embedding_enabled=False, reranker_enabled=False,
                           database_path=Path(tmp) / "coach.sqlite", index_dir=Path(tmp) / "index")
        coach = create_app(settings, llm=llm).state.coach
        sessions = build_sessions(coach)
        cases = ((rule_cases(coach, sessions, utterances())[:limit] if "rules" in sets else [])
                 + (label_cases(labels)[:limit] if "labels" in sets else []))
        entries = asyncio.run(evaluate(coach, sessions, cases, concurrency, partial_path(out)))
    # 판단 호출에 실제로 쓴 노력 수준(COACH_AGENT_EFFORT, 비어 있으면 CLAUDE_EFFORT)
    effort = getattr(coach.llm, "agent_effort", None) or getattr(coach.llm, "effort", None)
    summary = {"model": getattr(coach.llm, "model", None), "effort": effort, "labels_file": labels.name if "labels" in sets else None,
               "sets": list(sets), **summarize(entries, settings.agent_timeout_seconds)}
    _write(out, summary, entries)
    return summary


def _write(out: Path, summary: dict, entries: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "entries": entries}, ensure_ascii=False, indent=1),
                   encoding="utf-8")


def resummarize(path: Path) -> dict:
    """저장된 결과의 entries로 summary만 다시 계산해 같은 파일에 씁니다. Claude를 부르지 않습니다.

    모델·노력 수준·시간 제한·자료 파일·묶음은 entries로 알 수 없으므로 저장된 summary의 값을 그대로 둡니다
    (예전 파일에 없는 값은 None).
    """
    saved = json.loads(path.read_text(encoding="utf-8"))
    old = saved.get("summary") or {}
    timeout_seconds = (old.get("over_timeout") or {}).get("seconds", 10.0)
    summary = {"model": old.get("model"), "effort": old.get("effort"), "labels_file": old.get("labels_file"),
               "sets": old.get("sets"), **summarize(saved["entries"], timeout_seconds)}
    _write(path, summary, saved["entries"])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--out", type=Path)
    target.add_argument("--summarize", type=Path, metavar="FILE", help="저장된 결과의 요약만 다시 계산")
    parser.add_argument("--limit", type=int, default=None, help="묶음마다 앞에서부터 N건만")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--labels", type=Path, default=LABELS, metavar="FILE", help="기대 행동 자료의 경로")
    parser.add_argument("--sets", choices=("rules,labels", "rules", "labels"), default="rules,labels",
                        help="잴 묶음")
    args = parser.parse_args(argv)
    if args.concurrency < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--concurrency와 --limit은 1 이상이어야 합니다")
    if args.summarize:
        out, summary = args.summarize, resummarize(args.summarize)
    else:
        out, summary = args.out, run(Settings.from_env(), args.out, args.limit, args.concurrency,
                                     labels=args.labels, sets=tuple(args.sets.split(",")))
    sys.stdout.write(f"{sum(summary['cases'].values())} cases, {summary['calls']} calls, "
                     f"errors {len(summary['errors'])}, unanswered {summary['unanswered']}, "
                     f"over_timeout {summary['over_timeout']['count']}, "
                     f"rule_agreement {summary['rule_agreement']}, label_agreement {summary['label_agreement']}, "
                     f"violations {len(summary['state_change_violations'])}, "
                     f"rule_state_changes {len(summary['rule_state_changes'])} -> {out}\n")
    return 1 if failed(summary) else 0


if __name__ == "__main__":
    sys.exit(main())
