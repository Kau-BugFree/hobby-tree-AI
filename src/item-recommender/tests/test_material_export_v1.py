"""Contract tests for validation of the BE material export, without a database."""

import hashlib
import json
from pathlib import Path

import pytest

from item_recommender import cli

FIXTURE = Path(__file__).parent / "fixtures" / "item-material-export-v1.json"
SOURCE = "synthetic-hobbytree"
EPOCH = "11111111-1111-4111-8111-111111111111"
FIXTURE_HASH = "2cda742a5f265534e9f17f1ee4ef3e82d6c0f956aa86b3977eda0921cefca83f"
MAX_BYTES = 1_048_576


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def document():
    return json.loads(FIXTURE.read_bytes())


def encode(document):
    document["payloadSha256"] = hashlib.sha256(canonical(document["payload"])).hexdigest()
    return canonical(document) + b"\n"


@pytest.fixture
def invoke(tmp_path, capsys, monkeypatch):
    def forbid(*args, **kwargs):
        pytest.fail("file validation must not load service settings or open the catalog")

    monkeypatch.setattr(cli.Settings, "from_env", forbid)
    monkeypatch.setattr(cli, "open_store", forbid)

    def run(raw, filename="input.json", *, source=SOURCE, epoch=EPOCH):
        path = tmp_path / filename
        path.write_bytes(raw)
        code = cli.main(
            [
                "validate-material-export",
                "--file",
                str(path),
                "--source-id",
                source,
                "--source-epoch",
                epoch,
            ]
        )
        captured = capsys.readouterr()
        return code, json.loads(captured.out or captured.err)

    return run


def test_pinned_be_fixture_uses_raw_payload_hash(invoke, document):
    assert hashlib.sha256(canonical(document["payload"])).hexdigest() == FIXTURE_HASH
    code, result = invoke(FIXTURE.read_bytes())
    assert code == 0
    assert result["status"] == "VALIDATED"
    assert result["materialCount"] == 4


def test_empty_complete_export_held(invoke, document):
    document["payload"]["materials"] = []
    document.update(materialCount=0, activityCount=0)
    code, result = invoke(encode(document))
    assert (code, result["status"]) == (0, "HOLD_EMPTY")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sourceSequence", "0"),
        ("sourceSequence", "9223372036854775808"),
        ("sourceSequence", 1),
        ("exportId", "bad-uuid"),
        ("generatedAt", "2026-11-25"),
        ("generatedAt", "2026-02-30T00:00:00Z"),
        ("materialCount", 3),
        ("materialCount", True),
        ("activityCount", 3),
    ],
)
def test_invalid_envelope(invoke, document, field, value):
    document[field] = value
    assert invoke(encode(document))[0] == 2


@pytest.mark.parametrize(("field", "value"), [("scope", "partial"), ("schemaVersion", "v2")])
def test_unsupported_payload(invoke, document, field, value):
    document["payload"][field] = value
    assert invoke(encode(document))[0] == 2


def test_incomplete_corrupt_or_untrusted_deliveries(invoke, document):
    document["complete"] = False
    code, result = invoke(encode(document))
    assert (code, result["status"]) == (2, "REJECT_INCOMPLETE")
    document["complete"] = True
    document["payload"]["materials"][0]["name"] = "private-material-for-test"
    code, result = invoke(canonical(document) + b"\n")
    assert (code, result["status"]) == (2, "REJECT_HASH")
    assert "private-material-for-test" not in str(result)
    code, result = invoke(FIXTURE.read_bytes(), epoch="33333333-3333-4333-8333-333333333333")
    assert (code, result["status"]) == (2, "REJECT_EPOCH")
    assert invoke(FIXTURE.read_bytes(), source="other-source")[0] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("materialId", 101),
        ("materialId", "102"),  # duplicate
        ("materialId", "200"),  # unsorted despite being distinct
        ("materialId", "0101"),
        ("activityContentVersion", "０"),
        ("activityContentVersion", "9223372036854775808"),
        ("hobbyId", "0"),
        ("name", ""),
        ("name", "　 "),
        ("name", "앞\t뒤"),
        ("name", "앞\x7f뒤"),
        ("name", "가" * 161),
    ],
)
def test_invalid_row_with_valid_hash(invoke, document, field, value):
    document["payload"]["materials"][0][field] = value
    assert invoke(encode(document))[0] == 2


def test_numeric_order_and_code_point_length(invoke, document):
    rows = document["payload"]["materials"]
    for row, material_id in zip(rows, ("2", "9", "10", "9223372036854775807"), strict=True):
        row["materialId"] = material_id
    rows[0]["name"] = "🎨" * 160
    rows[1]["name"] = "가"
    document["sourceSequence"] = "9223372036854775807"
    assert invoke(encode(document))[0] == 0


@pytest.mark.parametrize("field", ["activityContentVersion", "hobbyId"])
def test_same_activity_requires_consistent_version_and_hobby(invoke, document, field):
    first, second, *_ = document["payload"]["materials"]
    for key in ("activityId", "activityContentVersion", "hobbyId"):
        second[key] = first[key]
    second[field] = "99"
    document["activityCount"] = 3
    assert invoke(encode(document))[0] == 2


def test_reject_staging_file_and_noncanonical_or_malformed_input(invoke):
    raw = FIXTURE.read_bytes()
    assert invoke(raw, filename=".material-export-part")[0] == 2
    for invalid in (
        raw[:-1],
        raw + b"\n",
        b"\xef\xbb\xbf" + raw,
        b"\xff\n",
        b"[]\n",
        raw.replace(b'"complete":true', b'"complete":true,"complete":true'),
        raw.replace(b'"complete":true', b'"complete": true'),
    ):
        assert invoke(invalid)[0] == 2


def set_rows(document, count):
    base = document["payload"]["materials"][0]
    rows = [dict(base, materialId=str(i), name="x") for i in range(1, count + 1)]
    document["payload"]["materials"] = rows
    document.update(materialCount=count, activityCount=1)
    return rows


def test_row_limit_independent_of_file_size(invoke, document):
    for count, expected_code in [(10_000, 0), (10_001, 2)]:
        set_rows(document, count)
        raw = encode(document)
        assert len(raw) < MAX_BYTES
        assert invoke(raw)[0] == expected_code


def test_file_size_limit_includes_trailing_lf(invoke, document):
    rows = set_rows(document, 5000)
    remaining = MAX_BYTES - len(encode(document))
    assert 0 < remaining < len(rows) * 159
    for row in rows:
        extra = min(159, remaining)
        row["name"] += "x" * extra
        remaining -= extra
        if not remaining:
            break
    raw = encode(document)
    assert len(raw) == MAX_BYTES
    assert invoke(raw)[0] == 0
    rows[-1]["name"] += "x"
    oversized = encode(document)
    assert len(oversized) == MAX_BYTES + 1
    assert invoke(oversized)[0] == 2
