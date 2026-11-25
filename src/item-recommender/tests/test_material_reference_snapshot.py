"""Validated BE source references retain identities and share catalog query keys."""

import hashlib
import json
from pathlib import Path

import pytest

from item_recommender.material_export import ExportError, read_reference_snapshot, validate_export

FIXTURE = Path(__file__).parent / "fixtures" / "item-material-export-v1.json"
SOURCE = "synthetic-hobbytree"
EPOCH = "11111111-1111-4111-8111-111111111111"


def write_export(path, doc):
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    doc["payloadSha256"] = hashlib.sha256(canonical(doc["payload"])).hexdigest()
    path.write_bytes(canonical(doc) + b"\n")


def test_all_source_references_survive_shared_query_keys():
    snapshot = read_reference_snapshot(FIXTURE, SOURCE, EPOCH)
    assert (snapshot.source_id, snapshot.source_epoch, snapshot.source_sequence) == (
        SOURCE,
        EPOCH,
        1,
    )
    assert (
        snapshot.payload_sha256
        == "2cda742a5f265534e9f17f1ee4ef3e82d6c0f956aa86b3977eda0921cefca83f"
    )
    assert len({ref.query_key for ref in snapshot.references}) == 3
    assert [
        (
            r.material_id,
            r.activity_id,
            r.hobby_id,
            r.activity_content_version,
            r.raw_name,
            r.query_key,
        )
        for r in snapshot.references
    ] == [
        (101, 11, 1, 2, '계량 "스푼"', '계량 "스푼"'),
        (102, 12, 2, 1, "  Ｓtraße  ", "strasse"),
        (103, 13, 3, 1, "책갈피", "책갈피"),
        (104, 14, 1, 1, '계량 "스푼"', '계량 "스푼"'),
    ]


@pytest.mark.parametrize("suffix", ["", "a"], ids=["normalized-200", "normalized-201"])
def test_reject_whole_snapshot_if_even_one_normalized_key_overflows(tmp_path, suffix):
    doc = json.loads(FIXTURE.read_bytes())
    doc["payload"]["materials"][-1]["name"] = "ß" * 100 + suffix
    path = tmp_path / "export.json"
    write_export(path, doc)
    assert validate_export(path, SOURCE, EPOCH)["status"] == "VALIDATED"
    if suffix:
        with pytest.raises(ExportError, match="^REJECT_QUERY_KEY$"):
            read_reference_snapshot(path, SOURCE, EPOCH)
    else:
        assert read_reference_snapshot(path, SOURCE, EPOCH).references[-1].query_key == "s" * 200


def test_reference_builder_rejects_rewrite_even_if_key_would_be_identical(tmp_path):
    raw = FIXTURE.read_bytes().replace("  Ｓtraße  ".encode(), b"strasse")
    path = tmp_path / "export.json"
    path.write_bytes(raw)
    with pytest.raises(ExportError, match="^REJECT_HASH$"):
        read_reference_snapshot(path, SOURCE, EPOCH)
