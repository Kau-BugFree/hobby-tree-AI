"""Rehearse the initial BE synthetic export, or serve its catalog for a local BE check."""

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smoke_local import running  # noqa: E402
from item_recommender.batch import BatchWorker  # noqa: E402
from item_recommender.catalog_domain import SEOUL  # noqa: E402
from item_recommender.catalog_store import SQLiteCatalogStore  # noqa: E402
from item_recommender.config import Settings  # noqa: E402
from item_recommender.main import create_app  # noqa: E402
from item_recommender.material_export import read_reference_snapshot  # noqa: E402
from item_recommender.provider import AdpickClient  # noqa: E402

SOURCE = "synthetic-hobbytree"
EPOCH = "11111111-1111-4111-8111-111111111111"
INITIAL = ROOT / "tests/fixtures/be-item-sync-v1/01-initial.json"
TITLE = "합성 계량스푼"
NOW = datetime(2026, 11, 25, 4, tzinfo=SEOUL)
SELECTION = {"materialId": 101, "budgetKrw": 6000}
PUBLIC_BODY = {
    "activityContentVersion": 1,
    "materialSelections": [SELECTION],
    "maxProductsPerMaterial": 1,
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def export_identity(path, *, require_new_file):
    snapshot = read_reference_snapshot(path, SOURCE, EPOCH)
    initial = read_reference_snapshot(INITIAL, SOURCE, EPOCH)
    require(
        snapshot.source_sequence == 1 and snapshot.payload_sha256 == initial.payload_sha256,
        "expected sequence 1 and the synthetic 01-initial payload",
    )
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if require_new_file:
        require(
            digest != hashlib.sha256(INITIAL.read_bytes()).hexdigest(),
            "use the newly generated BE export for the joint run",
        )
    return snapshot, digest


def purchase_link(digest):
    return "https://shop.example.com/qa/spoon/" + digest


async def populate(store, settings, url):
    calls = []

    def upstream(request):
        query = request.url.params["q"]
        calls.append(query)
        rows = []
        if query == "계량스푼":
            rows.append(
                {
                    "title": TITLE,
                    "price": 7500,
                    "commissionlink": url,
                    "cp_code": "QA",
                    "cp_name": "합성몰",
                }
            )
        return httpx.Response(200, json={"success": True, "data": rows})

    local = replace(settings, adpick_api_key="synthetic-only", adpick_filtering_approved=True)
    client = AdpickClient(local, httpx.MockTransport(upstream), clock=NOW.timestamp)
    try:
        worker = BatchWorker(store, client, local, clock=lambda: NOW)
        require(store.enqueue_due(NOW) == 3, "expected three imported queries")
        for _ in range(3):
            result = await worker.run_once()
            require(result["status"] == "COMPLETED", "synthetic batch did not finish")
        require(sorted(calls) == sorted(["계량스푼", "피크", "책갈피"]), "unexpected query set")
    finally:
        await client.aclose()


def check_product(response, url):
    require(response.status_code == 200, f"HTTP {response.status_code}")
    envelope = response.json()
    require(envelope["isSuccess"] is True, "unsuccessful response")
    result = envelope["result"]
    require(
        (result["activityId"], result["activityContentVersion"]) == (11, 1),
        "wrong activity/version",
    )
    rows = result["materialResults"]
    require(len(rows) == 1 and rows[0]["material"]["materialId"] == 101, "wrong material")
    require(
        rows[0]["status"] == "AVAILABLE" and len(rows[0]["products"]) == 1,
        "expected one available product",
    )
    product = rows[0]["products"][0]
    require(
        (product["title"], product["priceKrw"], product["purchaseUrl"]) == (TITLE, 7500, url),
        "wrong product, price or link",
    )
    comparison = product["budgetComparison"]
    require(
        (comparison["status"], comparison["differenceKrw"]) == ("OVER_BUDGET", 1500),
        "wrong budget comparison",
    )


def run_ai(path, port):
    snapshot, digest = export_identity(path, require_new_file=port is not None)
    url = purchase_link(digest)
    token = os.getenv("JOINT_SERVICE_TOKEN") if port else secrets.token_hex(24)
    require(bool(token), "set JOINT_SERVICE_TOKEN in this test session")
    with TemporaryDirectory(prefix="hobbytree-joint-qa-") as tmp:
        settings = Settings(
            catalog_db_path=Path(tmp) / "catalog.sqlite3",
            state_db_path=Path(tmp) / "state.sqlite3",
            storage_approved=True,
            allowed_purchase_hosts=("shop.example.com",),
            service_token=token,
        )
        store = SQLiteCatalogStore(settings.catalog_db_path)
        try:
            store.initialize()
            plan = store.sync_material_export(snapshot, NOW)
            require(
                plan["status"] == "APPLY"
                and plan["referenceCount"] == 4
                and plan["uniqueQueryCount"] == 3,
                "unexpected import plan",
            )
            for expected in ("APPLIED", "NOOP_REPLAY"):
                require(
                    store.sync_material_export(snapshot, NOW, apply=True)["status"] == expected,
                    f"expected {expected}",
                )
            asyncio.run(populate(store, settings, url))
            app = create_app(settings, store=store, clock=lambda: NOW)
            first = next(row for row in snapshot.references if row.material_id == 101)
            internal = {
                **PUBLIC_BODY,
                "activityId": first.activity_id,
                "reviewStatus": "PUBLISHED",
                "materials": [{"materialId": first.material_id, "name": first.raw_name}],
            }
            with running(app) as client:
                check_product(
                    client.post(
                        "/internal/v1/item-recommendations",
                        headers={"X-Service-Token": token},
                        json=internal,
                    ),
                    url,
                )
            print(
                json.dumps(
                    {
                        "status": "AI_LOCAL_PASS",
                        "sourceSequence": "1",
                        "payloadSha256": snapshot.payload_sha256,
                        "referenceCount": 4,
                        "uniqueQueryCount": 3,
                        "priceKrw": 7500,
                        "fileSha256": digest,
                        "purchaseUrl": url,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if port:
                print(f"Joint QA AI: http://127.0.0.1:{port}", flush=True)
                uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ai = commands.add_parser("ai")
    ai.add_argument("--file", required=True, type=Path)
    ai.add_argument("--serve-port", type=int)
    be = commands.add_parser("check-be")
    be.add_argument("--file", required=True, type=Path)
    be.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    port = args.serve_port if args.command == "ai" else args.port
    if port is not None and not 1 <= port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.command == "ai":
        run_ai(args.file, args.serve_port)
    else:
        _, digest = export_identity(args.file, require_new_file=True)
        token = os.getenv("JOINT_USER_TOKEN")
        require(bool(token), "set JOINT_USER_TOKEN for the active local test account")
        with httpx.Client(
            base_url=f"http://127.0.0.1:{args.port}", trust_env=False, timeout=15
        ) as client:
            response = client.post(
                "/api/v1/activities/11/item-recommendations",
                json=PUBLIC_BODY,
                headers={"Authorization": f"Bearer {token}"},
            )
            check_product(response, purchase_link(digest))
        print(
            json.dumps(
                {
                    "status": "BE_PUBLIC_PASS",
                    "fileSha256": digest,
                    "priceKrw": 7500,
                    "differenceKrw": 1500,
                }
            )
        )


if __name__ == "__main__":
    main()
