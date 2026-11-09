"""실제 서비스 응답으로 예제 JSON과 OpenAPI 문서를 다시 생성한다.

Run from the project root: .venv/bin/python -m scripts.export_examples
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def main():
    """예제 요청을 테스트 클라이언트로 실행한 뒤 응답과 명세를 UTF-8로 저장한다."""
    root = Path(__file__).resolve().parents[1]
    request = json.loads((root / "examples/request.json").read_text(encoding="utf-8"))
    # 수명주기를 실행해 데이터 검증까지 끝난 실제 앱으로 문서를 생성한다.
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/recommendations", json=request)
        response.raise_for_status()
        outputs = {"examples/response.json": response.json(), "docs/openapi.json": client.get("/openapi.json").json()}
    for file, value in outputs.items():
        (root / file).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Wrote actual API response and OpenAPI schema.")


if __name__ == "__main__":
    main()
