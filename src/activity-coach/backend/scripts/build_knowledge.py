"""Export exact catalog excerpts and their schemas; no generated cooking claims."""
import json
from pathlib import Path
from app.knowledge import KnowledgeRecord, load_knowledge
from app.dialogue_models import Decision


def build(data_dir: Path) -> int:
    records = []
    for line in (data_dir / "activities.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        activity = json.loads(line)
        aid = activity["activity_id"]
        fields = [f for f in ("checks", "steps", "safety_notes") if activity.get(f)]
        for field in fields:
            records.append(KnowledgeRecord(
                knowledge_id=f"{aid}:{field}", activity_ids=[aid],
                title=activity["title"] + " · " + field, kind=field,
                content="\n".join(activity[field]), source_refs=[f"{aid}/{field}"],
                review_status="source_only", applies_when="원문 활동의 참고 안내; 원인 확정 불가",
                related_knowledge_ids=[f"{aid}:{other}" for other in fields if other != field],
            ).model_dump())
    (data_dir / "coaching_knowledge.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n", encoding="utf-8")
    for name, model in (("coaching_knowledge.schema.json", KnowledgeRecord), ("decision.schema.json", Decision)):
        (data_dir / name).write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    load_knowledge(data_dir)
    return len(records)


if __name__ == "__main__":
    print(f"Validated {build(Path(__file__).resolve().parents[1] / 'data')} source-only records")
