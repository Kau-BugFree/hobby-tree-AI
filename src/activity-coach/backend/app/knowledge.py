"""Validated, source-traceable coaching knowledge; no invented human review."""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    schema_version: Literal["2.0"] = "2.0"
    knowledge_id: str = Field(min_length=1, max_length=160)
    activity_ids: list[str] = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=250)
    kind: Literal["checks", "steps", "safety_notes", "principle", "remedy"]
    content: str = Field(min_length=1, max_length=6000)
    applies_when: str = Field(default="", max_length=1000)
    related_knowledge_ids: list[str] = Field(default_factory=list, max_length=20)
    source_refs: list[str] = Field(min_length=1, max_length=20)
    review_status: Literal["source_only", "draft", "reviewed"] = "draft"
    reviewed_by: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def review_is_explicit(self):
        if self.review_status == "reviewed" and not self.reviewed_by:
            raise ValueError("reviewed knowledge requires reviewed_by")
        return self


def load_knowledge(data_dir: Path) -> list[dict]:
    path = data_dir / "coaching_knowledge.jsonl"
    if not path.exists():
        return []
    activities = {r["activity_id"]: r for r in (
        json.loads(line) for line in (data_dir / "activities.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())}
    records = [KnowledgeRecord.model_validate_json(line).model_dump()
               for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = {r["knowledge_id"] for r in records}
    if len(ids) != len(records) or ids.intersection(activities):
        raise ValueError("Duplicate knowledge ID or activity ID collision")
    for row in records:
        if not set(row["activity_ids"]).issubset(activities):
            raise ValueError("Unknown activity reference")
        if not set(row["related_knowledge_ids"]).issubset(ids):
            raise ValueError("Unknown knowledge reference")
        if row["review_status"] == "source_only":
            originals = []
            for ref in row["source_refs"]:
                aid, sep, field = ref.partition("/")
                if not sep or aid not in row["activity_ids"] or field not in {"checks", "steps", "safety_notes"}:
                    raise ValueError("Invalid source-only reference")
                originals.extend(activities[aid].get(field, []))
            if row["content"] != "\n".join(originals):
                raise ValueError("Source-only content differs from catalog")
    return records


def index_record(row: dict) -> dict:
    """Adapt to existing hybrid index; activity APIs never expose this synthetic ID."""
    return {**row, "activity_id": row["knowledge_id"], "hobby_key": "cooking",
            "description": row["content"], "steps": [row["content"]], "tools": [],
            "quality": {"status": "quarantined" if row["review_status"] == "draft" else "structural_pass"}}
