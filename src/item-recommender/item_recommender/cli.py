"""Local administration and batch worker entry point; no force-window escape hatch."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .batch import BatchWorker, utcnow
from .catalog_domain import in_batch_window
from .catalog_store import open_store
from .config import Settings
from .provider import AdpickClient


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _document(path: str) -> dict:
    # Exports can hold many activities, but unrestricted file sizes are not needed.
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("export too large")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("expected JSON object")
    return document


def _query(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError("invalid preparation name")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid preparation name")
    return value.strip()


def _material_queries(document: dict) -> list[str]:
    payload = document.get("result", document)
    if not isinstance(payload, dict) or not isinstance(payload.get("materials"), list):
        raise ValueError("expected exported materials")
    queries = []
    for material in payload["materials"]:
        if not isinstance(material, dict):
            raise ValueError("invalid material")
        queries.append(_query(material.get("name")))
    return queries


async def _worker(settings: Settings, store, once: bool) -> None:
    provider = AdpickClient(settings)
    try:
        worker = BatchWorker(store, provider, settings)
        last_summary = None
        while True:
            summary = await worker.run_once()
            if summary != last_summary or once:
                _emit(summary)
            last_summary = summary
            if once:
                return
            # Throttling never becomes an immediate retry loop. Outside-window
            # waits are bounded so shutdown and clock changes remain responsive.
            delay = max(settings.worker_poll_seconds, summary.get("retryAfterSeconds", 0))
            await asyncio.sleep(min(delay, 60))
    finally:
        await provider.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="하비트리 준비물 상품 수집 작업 관리")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="수집 메타데이터와 상품 저장소 초기화")
    register = commands.add_parser("register", help="BE 내보내기 JSON의 준비물 검색어 등록")
    register.add_argument("--file", required=True)
    fallback = commands.add_parser("fallbacks", help="운영자가 확인한 대체 검색어 등록")
    fallback.add_argument("--file", required=True)
    worker = commands.add_parser("worker", help="DB가 예약한 작업 처리")
    worker.add_argument("--once", action="store_true")
    commands.add_parser("schedule", help="SQLite 로컬 실행용 야간 작업 예약")
    commands.add_parser("status", help="작업 개수와 설정 상태 확인")
    commands.add_parser("purge", help="보관 기한이 지난 상품 삭제")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except Exception:
        _emit({"status": "INVALID_CONFIGURATION"})
        return 2

    store = None
    try:
        if args.command == "schedule" and (
            settings.app_env != "local" or settings.catalog_database_url
        ):
            _emit({"status": "SCHEDULE_MANAGED_BY_DATABASE"})
            return 2
        # Validate a whole import before writing anything; never echo its contents.
        document = _document(args.file) if args.command in {"register", "fallbacks"} else None
        queries = _material_queries(document) if args.command == "register" else None
        if args.command == "fallbacks":
            primary = _query(document.get("primary"))
            raw_fallbacks = document.get("fallbacks")
            if not isinstance(raw_fallbacks, list) or len(raw_fallbacks) > 10:
                raise ValueError("invalid fallback list")
            fallbacks = [_query(value) for value in raw_fallbacks]
        store = open_store(settings)
        now = utcnow()
        if args.command == "init-db":
            store.initialize()
            _emit({"status": "INITIALIZED"})
        elif args.command == "register":
            count = store.register_queries(queries, now)
            _emit({"status": "REGISTERED", "registeredCount": count})
        elif args.command == "fallbacks":
            store.set_fallbacks(primary, fallbacks, now)
            _emit({"status": "FALLBACKS_UPDATED", "fallbackCount": len(fallbacks)})
        elif args.command == "schedule":
            if not in_batch_window(now):
                _emit({"status": "OUTSIDE_BATCH_WINDOW", "enqueuedCount": 0})
            else:
                _emit({"status": "SCHEDULED", "enqueuedCount": store.enqueue_due(now)})
        elif args.command == "status":
            _emit(
                {
                    "status": "OK",
                    "jobs": store.job_counts(),
                    "inBatchWindow": in_batch_window(now),
                    "providerConfigured": bool(settings.adpick_api_key),
                    "filteringApproved": settings.adpick_filtering_approved,
                    "storageApproved": settings.storage_approved,
                }
            )
        elif args.command == "purge":
            _emit({"status": "PURGED", "deletedCount": store.purge_expired(now)})
        elif args.command == "worker":
            asyncio.run(_worker(settings, store, args.once))
        return 0
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        _emit({"status": "INVALID_INPUT"})
        return 2
    except KeyboardInterrupt:
        _emit({"status": "STOPPED"})
        return 0
    except Exception:
        _emit({"status": "COMMAND_FAILED"})
        return 1
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
