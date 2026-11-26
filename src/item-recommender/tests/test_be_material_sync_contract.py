"""Replay the BE team's unchanged synthetic files through the AI CLI on one catalog."""

import json
from pathlib import Path

from item_recommender import cli
from item_recommender.config import Settings
from test_material_export_sync import NOW, state
from test_material_reference_store import EPOCH, SOURCE

FIXTURES = Path(__file__).parent / "fixtures" / "be-item-sync-v1"


def read_fixture(filename):
    return json.loads((FIXTURES / filename).read_bytes())


def check_source_case(db, sync, case):
    filename = case["file"]
    doc = read_fixture(filename)
    expected = case["expectedAi"]
    expected_ids = case["expectedMaterialIds"]
    assert [row["materialId"] for row in doc["payload"]["materials"]] == expected_ids, filename
    before = state(db)

    preview = sync(filename, code=0, status=expected["decision"])
    assert preview["deactivationCandidates"] == expected["deactivationCandidates"], filename
    assert state(db) == before, filename

    if expected["decision"] == "HOLD_EMPTY":
        sync(filename, "--apply", code=3, status="HOLD_EMPTY")
        assert state(db) == before, filename
        assert {r["query_key"] for r in before[0] if r["active"]} == set(
            expected["activeKeysUnchanged"]
        ), filename
        return

    approval = []
    if expected["decision"] == "REVIEW_DEACTIVATION":
        sync(filename, "--apply", code=3, status="REVIEW_DEACTIVATION")
        assert state(db) == before, filename
        # The manifest lists exactly the candidates approved for this synthetic case.
        approval = ["--approve-plan", preview["planSha256"]]
    sync(filename, "--apply", *approval, code=0, status="APPLIED")

    queries, refs, receipts = state(db)
    active = {r["query_key"] for r in queries if r["active"]}
    assert active == set(expected["activeKeysAfterApprovedApply"]), filename
    assert set(expected["sharedQueriesPreserved"]) <= active, filename
    assert {r["query_key"] for r in queries if r["origin"] == "MANUAL"} == set(
        expected["manualOriginsPreserved"]
    ), filename
    assert [str(r["material_id"]) for r in refs if r["is_current"]] == expected_ids, filename
    assert len(receipts) == len(before[2]) + 1, filename
    assert receipts[-1]["source_sequence"] == int(doc["sourceSequence"]), filename
    assert receipts[-1]["payload_sha256"] == doc["payloadSha256"], filename


def test_be_scenarios(material_catalog_factory, monkeypatch, capsys):
    manifest = read_fixture("scenarios.json")
    assert manifest["syntheticOnly"] is True
    assert len(manifest["sourceCases"]) == 9
    assert len(manifest["deliveryCases"]) == 6
    monkeypatch.setattr(cli.Settings, "from_env", lambda: Settings())
    monkeypatch.setattr(
        cli, "open_store", lambda settings, **kwargs: material_catalog_factory(**kwargs)
    )

    def sync(filename, *flags, code, status):
        result = cli.main(
            [
                "sync-material-export",
                "--file",
                str(FIXTURES / filename),
                "--source-id",
                SOURCE,
                "--source-epoch",
                EPOCH,
                *flags,
            ]
        )
        report = json.loads(capsys.readouterr().out)
        assert (result, report["status"]) == (code, status), (filename, flags, report)
        return report

    db = material_catalog_factory()
    try:
        db.register_queries(manifest["initialManualQueries"], NOW)
        for case in manifest["sourceCases"]:
            check_source_case(db, sync, case)

        baseline = state(db)
        accepted = baseline[2][-1]
        last_export = read_fixture(manifest["deliveryBaseline"])
        assert str(accepted["source_sequence"]) == last_export["sourceSequence"] == "8"
        assert accepted["payload_sha256"] == last_export["payloadSha256"]
        for case in manifest["deliveryCases"]:
            filename = case["file"]
            decision = case["expectedDecision"]
            sync(filename, "--apply", code=0 if decision == "NOOP_REPLAY" else 2, status=decision)
            assert case["catalogUnchanged"] is True
            current = state(db)
            assert current == baseline, filename
            assert str(current[2][-1]["source_sequence"]) == case["acceptedSequenceUnchanged"], (
                filename
            )
    finally:
        db.close()
