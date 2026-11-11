"""Paid integration check: V1 credentials in memory, V2 data/index, isolated sessions.

Run from backend: python -m scripts.evaluate_live_dialogue
No scripted model decisions. Reports contain only synthetic conversations and diagnostics.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import torch
import argparse

from app.ask import RagAsk
from app.claude import ClaudeService
from app.coach import Coach
from app.config import ROOT, Settings
from app.models import CoachRequest
from app.retrieval import Retrieval
from app.store import Store


class TracedClaude(ClaudeService):
    def __init__(self, settings):
        super().__init__(settings)
        self.decisions = []
        self.generation_calls = 0

    async def _generate_once(self, *args, **kwargs):
        self.generation_calls += 1
        return await super()._generate_once(*args, **kwargs)

    async def decide(self, question, context, evidence):
        result = await super().decide(question, context, evidence)
        self.decisions.append({"question": question, "current_step": context["current_step"],
                               "observations": list(context["dialogue"]["observations"]),
                               "available_evidence": [e["evidence_id"] for e in evidence], **result})
        return result


class TracedRetrieval(Retrieval):
    def __init__(self, settings):
        self.searches = []
        super().__init__(settings)

    def search(self, *args, **kwargs):
        result = super().search(*args, **kwargs)
        self.searches.append({"query": args[0] if args else kwargs["query"],
                              "ids": [r["activity_id"] for r in result["items"]],
                              "diagnostics": result["diagnostics"]})
        return result


def rag_answer_cites(result, activity_id):
    """The RAG answer, a source reference or a verified generated answer, cites the expected activity.

    A withheld answer or a failed generation is not an answer.
    """
    return (result["status"] in {"reference", "generated", "generated_repaired"}
            and activity_id in result["cited_activity_ids"])


def dependent_hops(trace):
    """Every later hop must depend on evidence actually returned by an earlier hop."""
    seen = set()
    if len(trace) < 2:
        return False
    for index, hop in enumerate(trace):
        if index and (not hop["depends_on"] or not set(hop["depends_on"]).issubset(seen)):
            return False
        if not set(hop["evidence_ids"]) - seen:
            return False
        seen.update(hop["evidence_ids"])
    return True


async def evaluate():
    settings = Settings.from_env()
    assert settings.anthropic_api_key, "Set this version's ANTHROPIC_API_KEY"
    assert torch.cuda.is_available(), "CUDA is required; CPU fallback is not a live GPU pass"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "var/live" / run_id
    run_dir.mkdir(parents=True)
    from dataclasses import replace
    settings = replace(settings, device="cuda", database_path=run_dir / "coach.sqlite")
    report = {"created_at": run_id, "model": settings.claude_model,
              "gpu": torch.cuda.get_device_name(0), "turns": [], "checks": {}}
    output = run_dir / "report.json"

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"report": str(output), "model": report["model"], "gpu": report["gpu"]}), flush=True)
    retrieval = TracedRetrieval(settings)
    report["retrieval"] = retrieval.status()
    assert retrieval._encoder and retrieval._reranker, "Both GPU models must load"
    report["devices"] = {"embedding": str(retrieval._encoder._model.device),
                          "reranker": str(retrieval._reranker._model.model.device)}
    assert all(v.startswith("cuda") for v in report["devices"].values())
    llm = TracedClaude(settings)
    store = Store(settings.database_path)
    coach = Coach(store, retrieval, llm)

    async def turn(sid, text, label):
        start = time.perf_counter()
        before = len(llm.decisions)
        searches = len(retrieval.searches)
        activity_id = None if store.get_session(sid)["selected_activity_id"] else "cook-0-2"
        request = CoachRequest(session_id=sid, activity_id=activity_id, message=text, request_id=label)
        result = await coach.respond(request)
        row = {"label": label, "message": text, "seconds": round(time.perf_counter() - start, 2),
               "response": result, "decisions": llm.decisions[before:],
               "searches": retrieval.searches[searches:],
               "dialogue": store.get_session(sid)["dialogue"]}
        report["turns"].append(row)
        save()
        print(json.dumps({"turn": label, "mode": result["mode"], "seconds": row["seconds"],
                          "diagnostics": result["diagnostics"]}, ensure_ascii=False), flush=True)
        return result, request

    sid = store.create_session()["session_id"]
    first, request = await turn(sid, "2단계 계량이 잘 안돼", "multi-turn-question")
    report["checks"]["asks_observation"] = first["mode"] == "clarify"
    before = llm.generation_calls
    replay = await coach.respond(request)
    report["checks"]["replay_no_api"] = replay["message_id"] == first["message_id"] and llm.generation_calls == before
    # Recreate services to check restoration from SQLite rather than in-memory state.
    store = Store(settings.database_path)
    coach = Coach(store, retrieval, llm)
    second, _ = await turn(sid, "1번", "multi-turn-option-after-restart")
    state = store.get_session(sid)["dialogue"]
    options = first.get("clarification", {}).get("options", [])
    report["checks"]["option_context_restored"] = bool(options and state["observations"][-1]["answer"] == options[0])
    report["checks"]["option_reply_continues"] = second["mode"] in {"reference", "generated", "clarify"}
    third, _ = await turn(sid, "그럼 액체 계량과 남은 드레싱 보관 방법을 원문에서 같이 확인해줘.", "multi-turn-followup")
    report["checks"]["followup_answered"] = third["mode"] in {"reference", "generated"}
    correction, _ = await turn(sid, "새 문제야. 이제 남은 드레싱 보관이 헷갈려. 보관 장소와 기간을 알려줘.", "correction")
    report["checks"]["new_issue_has_separate_observations"] = len(store.get_session(sid)["dialogue"]["observations"]) == 1
    report["checks"]["correction_answered"] = correction["mode"] in {"reference", "generated"}
    sid = store.create_session()["session_id"]
    multi, _ = await turn(sid, "계량 방법과 완료 확인 기준, 남은 드레싱 보관 방법이 헷갈려. 가루와 액체를 어떻게 계량하고, 잘했는지 무엇으로 확인하고, 남은 건 어디에 얼마나 보관하는지 자료를 연결해서 알려줘.", "multi-hop")
    report["checks"]["bundle_avoids_redundant_search"] = multi["diagnostics"].get("retrieval_calls") == 1
    report["checks"]["multi_hop_answered"] = multi["mode"] in {"reference", "generated"}
    report["checks"]["bundle_cites_expected_sources_only"] = set(multi.get("cited_evidence_ids", [])) == {
        "cook-0-2:steps", "cook-0-2:checks", "cook-0-2:safety_notes"}
    sid = store.create_session()["session_id"]
    uncertain, _ = await turn(sid, "계량이 잘 안돼", "uncertain-question")
    report["checks"]["uncertain_asks_observation"] = uncertain["mode"] == "clarify"
    unknown, _ = await turn(sid, "모르겠어요", "uncertain-unknown")
    report["checks"]["unknown_reply_continues"] = unknown["mode"] in {"clarify", "reference", "generated"}
    resolved, _ = await turn(sid, "큰술과 작은술의 차이가 헷갈려. 물로 확인하는 방법을 알려줘.", "uncertain-resolved")
    report["checks"]["uncertain_resolved"] = resolved["mode"] in {"reference", "generated"} and "cook-0-2:steps" in resolved.get("cited_evidence_ids", [])
    report["checks"]["no_activity_side_effect"] = all(
        r["response"]["selected_activity_id"] == "cook-0-2" and r["response"]["execution_id"] is None
        and r["response"]["current_step"] == 1 and r["dialogue"]["clarification_count"] <= 2
        for r in report["turns"])
    legacy = await RagAsk(retrieval, llm).answer("계량해서 간장 드레싱 만들기에서 큰술과 작은술의 차이를 알려줘.", "cooking", {}, 1, True)
    report["legacy"] = legacy
    report["checks"]["rag_answer_cites_source"] = rag_answer_cites(legacy, "cook-0-2")
    report["checks"]["gpu_hybrid_search"] = bool(retrieval.searches) and all(
        s["diagnostics"]["search_mode"] == "hybrid_bm25_dense_rrf" and s["diagnostics"]["reranker"] == "cross_encoder"
        for s in retrieval.searches)
    report["llm_calls"] = sum(r["response"]["diagnostics"]["llm_calls"] for r in report["turns"]) + legacy["llm"]["calls"]
    report["checks"]["all_calls_accounted"] = report["llm_calls"] == llm.generation_calls
    report["usage"] = {key: sum(r["response"]["diagnostics"].get("usage", {}).get(key, 0) for r in report["turns"]) + legacy["llm"]["usage"].get(key, 0)
                       for key in ("input_tokens", "output_tokens", "cache_read_input_tokens")}
    save()
    print(json.dumps({"checks": report["checks"], "llm_calls": report["llm_calls"], "usage": report["usage"]}), flush=True)
    assert all(report["checks"].values()), f"Live checks failed; inspect {output}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Legacy smoke checks; not semantic quality evaluation")
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for paid API/GPU checks; use evaluate_dialogue for offline fixture validation')
    asyncio.run(evaluate())
