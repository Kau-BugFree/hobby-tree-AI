"""Compares two intent_baseline.py outputs entry by entry, matched on (context, text).

Usage: python -B scripts/compare_baseline.py OLD.json NEW.json
Entries only in NEW are ignored. Exits 1 when any OLD entry differs or is missing in NEW.
"""
import json
import sys
from pathlib import Path

SHOWN = 20


def load(path: Path) -> dict[tuple[str, str], dict]:
    return {(entry["context"], entry["text"]): entry for entry in json.loads(path.read_text(encoding="utf-8"))}


def differences(old: dict, new: dict) -> list[tuple[dict, dict | None]]:
    return [(entry, new.get(key)) for key, entry in old.items() if new.get(key) != entry]


def main(old_path: Path, new_path: Path) -> int:
    old = load(old_path)
    found = differences(old, load(new_path))
    sys.stdout.write(f"entries: {len(old)}, differences: {len(found)}\n")
    for before, after in found[:SHOWN]:
        sys.stdout.write(json.dumps({"old": before, "new": after}, ensure_ascii=False) + "\n")
    return 1 if found else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: compare_baseline.py OLD.json NEW.json")
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
