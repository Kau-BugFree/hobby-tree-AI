"""Records how the current router labels each known utterance, so a refactor can be compared against it.

Usage: python -B scripts/intent_baseline.py var/intent-rules/baseline.json
No Claude calls: the app is built with an empty API key, no embeddings and no reranker.
"""
import ast
import json
import logging
import re
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

logger = logging.getLogger(__name__)
DEV_CASES = BACKEND / "data" / "dialogue_eval_dev.jsonl"
SELECTED_ACTIVITY = "cook-0-2"
HANGUL = re.compile(r"[가-힣]")
MAX_PARAM_LENGTH = 200


def _call_name(node: ast.Call) -> str | None:
    """Returns the called function's last name: message(...) and api.message(...) both give 'message'."""
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def test_sentences(source: str) -> list[str]:
    """Third positional string of message(...) calls, plus short Hangul strings inside parametrize(...), in source order."""
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "message" and len(node.args) > 2:
            text = node.args[2]
            if isinstance(text, ast.Constant) and isinstance(text.value, str):
                found.append((text.lineno, text.col_offset, text.value))
        elif name == "parametrize":
            found += [(item.lineno, item.col_offset, item.value)
                      for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
                      for item in ast.walk(argument)
                      if isinstance(item, ast.Constant) and isinstance(item.value, str)
                      and len(item.value) <= MAX_PARAM_LENGTH and HANGUL.search(item.value)]
    return [text for _, _, text in sorted(found)]


def utterances() -> list[str]:
    """Dev-set turns in file order, then test sentences in sorted file order, first occurrence kept."""
    found = [turn for line in DEV_CASES.read_text(encoding="utf-8").splitlines() if line.strip()
             for turn in json.loads(line)["turns"]]
    for path in sorted((BACKEND / "tests").glob("*.py")):
        found += test_sentences(path.read_text(encoding="utf-8"))
    return list(dict.fromkeys(text for text in found if text.strip()))


def classify(client: TestClient, text: str, context: str, index: int) -> dict:
    """Sends one utterance in a fresh session and returns the baseline entry."""
    response = client.post("/sessions", json={"hobby_key": "cooking"})
    response.raise_for_status()
    session_id = response.json()["session_id"]
    if context == "selected":
        client.post(f"/sessions/{session_id}/select", json={"activity_id": SELECTED_ACTIVITY}).raise_for_status()
    response = client.post("/coach/messages", json={
        "session_id": session_id, "message": text, "request_id": f"baseline-{context}-{index}"})
    if response.is_client_error:
        # '0분 기다려도 가능해?'처럼 입력 검증에서 거절되는 문장도 기준선의 일부다. 형식을 지키려고 상태 코드를 이름으로 쓴다.
        logger.warning("%s rejected with %d in context %s", text, response.status_code, context)
        rejected = f"http_{response.status_code}"
        return {"context": context, "text": text, "intent": rejected, "mode": rejected, "search_performed": False}
    response.raise_for_status()
    body = response.json()
    return {"context": context, "text": text, "intent": body["diagnostics"]["intent"], "mode": body["mode"],
            "search_performed": body["diagnostics"]["search_performed"]}


def run(output: Path) -> None:
    """Writes one entry per (utterance, context) to output as UTF-8 JSON."""
    texts = utterances()
    # Windows에서는 앱이 연 retrieval.sqlite 핸들이 남아 임시 폴더 삭제가 실패할 수 있어 삭제 오류를 무시한다.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = Settings(
            data_dir=BACKEND / "data",
            index_dir=Path(tmp) / "index",
            database_path=Path(tmp) / "coach.sqlite",
            embedding_enabled=False,
            reranker_enabled=False,
            anthropic_api_key="",
            allowed_hosts=("testserver",),
        )
        with TestClient(create_app(settings)) as client:
            entries = [classify(client, text, context, index)
                       for index, text in enumerate(texts) for context in ("none", "selected")]
    logger.info("classified %d utterances", len(texts))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: intent_baseline.py OUTPUT.json")
    target = Path(sys.argv[1])
    run(target)
    sys.stdout.write(f"{len(json.loads(target.read_text(encoding='utf-8')))} entries -> {target}\n")
