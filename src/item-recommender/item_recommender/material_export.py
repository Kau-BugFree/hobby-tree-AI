"""Validate the BE full material export v1 before any catalog operation."""

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from uuid import UUID

MAX_BYTES = 1_048_576
MAX_ROWS = 10_000
MAX_BIGINT = 2**63 - 1
SCHEMA = "hobbytree.item-material-export.v1"
SCOPE = "all-supported-published-activity-materials"
HEADER = {
    "exportId",
    "sourceId",
    "sourceEpoch",
    "sourceSequence",
    "generatedAt",
    "complete",
    "materialCount",
    "activityCount",
    "payloadSha256",
    "payload",
}
ROW = {"materialId", "activityId", "hobbyId", "activityContentVersion", "name"}


class ExportError(ValueError):
    """Stable reason code suitable for CLI output."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("REJECT_DUPLICATE_FIELD")
        result[key] = value
    return result


def _canonical(obj):
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _integer(text):
    if not isinstance(text, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", text):
        raise ExportError("REJECT_INTEGER")
    value = int(text)
    if value > MAX_BIGINT:
        raise ExportError("REJECT_INTEGER")
    return value


def _uuid(text):
    if not isinstance(text, str):
        return False
    try:
        return str(UUID(text)) == text
    except ValueError:
        return False


def _fields(obj, expected):
    if not isinstance(obj, dict) or obj.keys() != expected:
        raise ExportError("REJECT_FIELDS")


def _check_header(doc, source_id, source_epoch):
    _fields(doc, HEADER)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", source_id) or doc["sourceId"] != source_id:
        raise ExportError("REJECT_SOURCE")
    if not _uuid(source_epoch) or doc["sourceEpoch"] != source_epoch:
        raise ExportError("REJECT_EPOCH")
    if not _uuid(doc["exportId"]):
        raise ExportError("REJECT_EXPORT_ID")
    _integer(doc["sourceSequence"])
    timestamp = doc["generatedAt"]
    if not isinstance(timestamp, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|\+00:00)",
        timestamp,
    ):
        raise ExportError("REJECT_TIMESTAMP")
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        raise ExportError("REJECT_TIMESTAMP") from None
    if doc["complete"] is not True:
        raise ExportError("REJECT_INCOMPLETE")
    for key in ("activityCount", "materialCount"):
        if type(doc[key]) is not int or not 0 <= doc[key] <= MAX_ROWS:
            raise ExportError("REJECT_COUNT")
    _fields(doc["payload"], {"schemaVersion", "scope", "materials"})
    if doc["payload"]["schemaVersion"] != SCHEMA or doc["payload"]["scope"] != SCOPE:
        raise ExportError("REJECT_CONTRACT")


def _check_rows(doc):
    rows = doc["payload"]["materials"]
    if not isinstance(rows, list) or len(rows) != doc["materialCount"]:
        raise ExportError("REJECT_COUNT")
    last_id = 0
    activities = {}
    for row in rows:
        _fields(row, ROW)
        material_id = _integer(row["materialId"])
        if material_id <= last_id:
            raise ExportError("REJECT_MATERIAL_ORDER")
        last_id = material_id
        activity_id = _integer(row["activityId"])
        identity = (_integer(row["hobbyId"]), _integer(row["activityContentVersion"]))
        if activities.setdefault(activity_id, identity) != identity:
            raise ExportError("REJECT_ACTIVITY")
        name = row["name"]
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 160
            or not name.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
        ):
            raise ExportError("REJECT_NAME")
    if len(activities) != doc["activityCount"]:
        raise ExportError("REJECT_COUNT")


def validate_export(path: Path, source_id: str, source_epoch: str) -> dict:
    """Read one final regular file. The trusted identity comes from the caller."""
    if path.name.startswith(".material-export-"):
        raise ExportError("REJECT_TEMP_FILE")
    try:
        # Do not follow a link to the staging file or block on a non-regular file.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb", buffering=0) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ExportError("REJECT_FILE")
            data = stream.read(MAX_BYTES + 1)
    except OSError:
        raise ExportError("REJECT_FILE") from None
    if len(data) > MAX_BYTES:
        raise ExportError("REJECT_SIZE")
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
        _check_header(doc, source_id, source_epoch)
        _check_rows(doc)
        digest = hashlib.sha256(_canonical(doc["payload"])).hexdigest()
        if doc["payloadSha256"] != digest:
            raise ExportError("REJECT_HASH")
        if data != _canonical(doc) + b"\n":
            raise ExportError("REJECT_ENCODING")
    except ExportError:
        raise
    except (ValueError, RecursionError):
        raise ExportError("REJECT_JSON") from None
    return {
        "status": "VALIDATED" if doc["materialCount"] else "HOLD_EMPTY",
        "materialCount": doc["materialCount"],
        "activityCount": doc["activityCount"],
        "sourceSequence": doc["sourceSequence"],
        "payloadSha256": digest,
    }
