"""전처리한 레시피 두 파일을 보조 검색 범위 recipes로 적재하는 오프라인 명령.

backend 폴더에서 실행하고, 서버를 재시작하지 않아도 다음 검색부터 사용된다.
    python -m scripts.import_recipes "../../2025-11-05/hobbytree_recipe_data_2026-11-03/preprocessed/data"
BM25 색인만 만든다. 활동·지식 범위는 건드리지 않으며 격리 자료와 리뷰는 적재하지 않는다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.retrieval import build_lexical_index

RECIPE_FILES = ("recipes_list.normalized.jsonl.gz", "recipes_food.normalized.jsonl.gz")


def main(argv: list[str] | None = None) -> dict[str, Any]:
    """두 파일이 모두 있을 때만 recipes 범위를 한 트랜잭션으로 교체하고 적재 요약을 반환한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path, help="전처리 결과의 data 폴더")
    parser.add_argument("--index-dir", type=Path, default=Settings.from_env().index_dir)
    args = parser.parse_args(argv)
    paths = [args.data_dir / name for name in RECIPE_FILES]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        # 한 파일만 적재하면 기존 범위가 일부 자료로 바뀌므로 색인을 열기 전에 멈춘다.
        raise SystemExit("레시피 파일이 없습니다: " + ", ".join(missing))
    summary = build_lexical_index(paths, args.index_dir, "recipes")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


if __name__ == "__main__":
    main()
