"""Export the local contract without credentials or a provider connection."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from item_recommender.config import Settings  # noqa: E402
from item_recommender.main import create_app  # noqa: E402

if __name__ == "__main__":
    target = ROOT / "docs" / "openapi.json"
    target.write_text(
        json.dumps(create_app(Settings()).openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(target)
