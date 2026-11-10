"""SQLite로 대화·진행·완료 기록을 보관합니다. 원문 추적 정보나 API 키는 저장하지 않습니다."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .dialogue_models import new_dialogue


# session_id 없이 온 첫 요청이 만든 세션을 찾는 질의. sessions_origin 색인의 식과 글자까지 같아야 색인을 쓴다.
ORIGIN_LOOKUP = ("SELECT payload FROM sessions WHERE json_extract(payload,'$.origin_request_id')=? "
                 "ORDER BY rowid LIMIT 1")


def now():
    """환경의 시간대와 무관하게 기록 시각을 UTC ISO 문자열로 저장합니다."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StoreError(Exception):
    """HTTP 상태와 사용자 메시지를 함께 전달하는 저장소 예외입니다."""

    def __init__(self, status, message):
        """HTTP 오류 코드와 사용자용 설명을 보관합니다."""
        self.status = status
        self.message = message
        super().__init__(message)


class Store:
    def __init__(self, path):
        """DB가 없으면 생성하고, 실행별 중복 기록을 막는 테이블과 제약을 준비합니다."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,activity_id TEXT NOT NULL,request_id TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(session_id,request_id));
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,execution_id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(session_id TEXT,request_id TEXT,fingerprint TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(session_id,request_id));
            CREATE TABLE IF NOT EXISTS traces(id TEXT PRIMARY KEY,session_id TEXT,created_at TEXT,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS dialogue_issues(issue_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sessions_origin ON sessions(json_extract(payload,'$.origin_request_id'));
            CREATE INDEX IF NOT EXISTS records_session ON records(session_id);
            CREATE INDEX IF NOT EXISTS traces_session ON traces(session_id);
            """)

    @contextmanager
    def connection(self):
        """호출마다 연결을 열고 성공 시 확정, 예외 시 취소한 뒤 연결을 닫습니다."""
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _new_session(hobby_key, origin_request_id=None):
        """취미, 선택 활동, 검색 후보, 임시 조건을 담는 새 대화의 초기 상태입니다."""
        session = {
            "session_id": str(uuid.uuid4()),
            "hobby_key": hobby_key,
            "candidate_ids": [],
            "selected_activity_id": None,
            "current_step": 1,
            "constraints": {
                "max_minutes": None,
                "available_tools": None,
                "excluded_tools": None,
                "max_difficulty": None,
                "budget_max": None,
            },
            "messages": [],
            "execution_id": None,
            "search_query": "",
            "search_scope": "activities",
            "created_at": now(),
            "dialogue": new_dialogue(),
        }
        if origin_request_id:
            session["origin_request_id"] = origin_request_id
        return session

    def create_session(self, hobby_key="cooking"):
        """새로운 대화 공간을 만듭니다."""
        session = self._new_session(hobby_key)
        with self.connection() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?,?)",
                (session["session_id"], json.dumps(session, ensure_ascii=False)),
            )
        return session

    def session_for_origin(self, request_id, hobby_key="cooking"):
        """session_id 없이 온 첫 요청의 세션을 찾고, 없으면 만듭니다. 같은 요청의 재전송은 같은 세션을 받습니다.

        찾기와 만들기가 한 쓰기 트랜잭션입니다. 같은 요청이 여러 worker에 동시에 와도 세션은 하나만 생깁니다.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(ORIGIN_LOOKUP, (request_id,)).fetchone()
            if row:
                return json.loads(row[0])
            session = self._new_session(hobby_key, request_id)
            db.execute(
                "INSERT INTO sessions VALUES (?,?)",
                (session["session_id"], json.dumps(session, ensure_ascii=False)),
            )
        return session

    def get_session(self, sid):
        """저장된 대화를 복원합니다. 잘못된 ID를 새 대화로 조용히 바꾸지 않습니다."""
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        if not row:
            raise StoreError(
                404, "대화 세션을 찾을 수 없습니다. 새 세션을 만들어 주세요."
            )
        session = json.loads(row[0])
        session.setdefault("dialogue", new_dialogue())
        from .dialogue_state import normalize_dialogue, serialize_dialogue
        session['dialogue'] = serialize_dialogue(normalize_dialogue(session['dialogue'], session))
        return session

    def save_session(self, session, issue_snapshots=None):
        """선호 프로필 대신 이번 세션의 조건만 저장하고 최근 100개 메시지를 보관합니다."""
        with self.connection() as db:
            self._store_session(db, session, issue_snapshots)

    def _store_session(self, db, session, issue_snapshots=None):
        """열려 있는 트랜잭션 안에서 세션과 문제 스냅샷을 함께 저장합니다."""
        session["messages"] = session["messages"][-100:]
        self._save_issues(db, session, issue_snapshots)
        self._write_session(db, session)

    @staticmethod
    def _write_session(db, session):
        """세션을 저장합니다. 읽은 뒤 다른 요청이 먼저 저장했으면 덮어쓰지 않고 409로 알립니다.

        세션별 잠금은 한 프로세스 안에서만 유효합니다. worker가 둘 이상이거나 같은 DB를 쓰는 서버가 둘이면
        개정 번호가 어긋나고, 그때는 나중에 온 저장을 거부해 앞선 갱신이 조용히 사라지지 않게 합니다.
        """
        read = session.get("revision", 0)
        saved = db.execute(
            "UPDATE sessions SET payload=? WHERE id=? AND COALESCE(json_extract(payload,'$.revision'),0)=?",
            (json.dumps({**session, "revision": read + 1}, ensure_ascii=False), session["session_id"], read),
        ).rowcount
        if not saved:
            raise StoreError(409, "다른 요청이 이 대화를 먼저 갱신했습니다. 같은 요청을 다시 보내 주세요.")
        session["revision"] = read + 1

    def cached_request(self, sid, rid, fingerprint):
        """동일 요청의 결과를 재사용하고, 같은 요청 ID를 다른 본문에 쓰면 거부합니다."""
        if not rid:
            return None
        with self.connection() as db:
            row = db.execute(
                "SELECT fingerprint,response FROM requests WHERE session_id=? AND request_id=?",
                (sid, rid),
            ).fetchone()
        if not row:
            return None
        if row[0] != fingerprint:
            raise StoreError(409, "같은 request_id를 다른 질문에 사용할 수 없습니다.")
        return json.loads(row[1])

    def save_reply(self, session, request_id, fingerprint, response, user_text, user_message_id=None, issue_snapshots=None):
        """대화 상태와 재전송 응답을 같은 트랜잭션에서 저장합니다."""
        session["messages"].extend(
            [
                {"message_id": user_message_id or str(uuid.uuid4()), "role": "user", "text": user_text},
                {
                    "message_id": response["message_id"],
                    "role": "assistant",
                    "text": response["text"],
                    "response": response,
                },
            ]
        )
        with self.connection() as db:
            self._store_session(db, session, issue_snapshots)
            if request_id:
                db.execute(
                    "INSERT INTO requests VALUES (?,?,?,?)",
                    (
                        session["session_id"],
                        request_id,
                        fingerprint,
                        json.dumps(response, ensure_ascii=False),
                    ),
                )
            db.execute(
                "INSERT INTO traces VALUES (?,?,?,?)",
                (
                    response["diagnostics"]["trace_id"],
                    session["session_id"],
                    now(),
                    json.dumps(response["diagnostics"]),
                ),
            )

    @staticmethod
    def _save_issues(db, session, snapshots):
        snapshots = [*(snapshots or []), *session.pop('_issue_snapshots', [])]
        if session.get('dialogue', {}).get('issue_id'):
            snapshots.append(session['dialogue'])
        for state in snapshots:
            db.execute('INSERT INTO dialogue_issues VALUES (?,?,?,?) ON CONFLICT(issue_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                       (state['issue_id'], session['session_id'], json.dumps(state, ensure_ascii=False), now()))

    def start_execution(self, sid, activity, request_id, attach=None):
        """(실행, 새로 만들었는지)를 반환합니다. 같은 시작 요청은 기존 실행을 재사용하고, 반복 수행은 새 request_id를 사용합니다.

        attach는 새 실행을 세션에 연결하고 그 세션을 돌려주는 함수입니다. 주면 실행과 세션을 한 트랜잭션에서 저장합니다.
        세션 저장이 거부되면 실행도 만들지 않으므로, 세션에 연결되지 않은 실행이 남지 않습니다.
        """
        self.get_session(sid)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT activity_id,payload FROM executions WHERE session_id=? AND request_id=?",
                (sid, request_id),
            ).fetchone()
            if old:
                if old[0] != activity["activity_id"]:
                    raise StoreError(
                        409, "같은 시작 요청 ID를 다른 활동에 사용할 수 없습니다."
                    )
                return json.loads(old[1]), False
            execution = {
                "execution_id": str(uuid.uuid4()),
                "session_id": sid,
                "activity_id": activity["activity_id"],
                "activity_title": activity["title"],
                "done": [False] * len(activity["steps"]),
                "status": "in_progress",
                "record_id": None,
                "created_at": now(),
            }
            db.execute(
                "INSERT INTO executions VALUES (?,?,?,?,?)",
                (
                    execution["execution_id"],
                    sid,
                    activity["activity_id"],
                    request_id,
                    json.dumps(execution, ensure_ascii=False),
                ),
            )
            if attach:
                self._store_session(db, attach(execution))
        return execution, True

    def get_execution(self, eid):
        """한 번의 활동 수행에 속하는 단계 체크와 완료 상태를 읽습니다."""
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM executions WHERE id=?", (eid,)
            ).fetchone()
        if not row:
            raise StoreError(404, "활동 실행 기록을 찾을 수 없습니다.")
        return json.loads(row[0])

    def update_progress(self, eid, done):
        """전체 단계 체크를 원자적으로 저장하고 완료된 실행을 소급 변경하지 않습니다."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM executions WHERE id=?", (eid,)
            ).fetchone()
            if not row:
                raise StoreError(404, "활동 실행 기록을 찾을 수 없습니다.")
            execution = json.loads(row[0])
            if len(done) != len(execution["done"]):
                raise StoreError(
                    422, "진행 상태의 개수는 활동의 전체 단계 수와 같아야 합니다."
                )
            if execution["status"] == "completed" and done != execution["done"]:
                raise StoreError(
                    409, "이미 완료된 실행의 진행 상태는 변경할 수 없습니다."
                )
            execution["done"] = done
            db.execute(
                "UPDATE executions SET payload=? WHERE id=?",
                (json.dumps(execution, ensure_ascii=False), eid),
            )
        return execution

    def complete(self, activity_id, eid, request_id, note=""):
        """실행 건당 기록 1개를 DB UNIQUE 제약으로 보장합니다. 모든 단계 완료 후에만 저장합니다."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM executions WHERE id=?", (eid,)
            ).fetchone()
            if not row:
                raise StoreError(404, "활동 실행 기록을 찾을 수 없습니다.")
            execution = json.loads(row[0])
            if execution["activity_id"] != activity_id:
                raise StoreError(409, "실행 중인 활동과 기록 대상이 다릅니다.")
            old = db.execute(
                "SELECT payload FROM records WHERE execution_id=?", (eid,)
            ).fetchone()
            if old:
                return {**json.loads(old[0]), "created": False}
            if not all(execution["done"]):
                raise StoreError(409, "모든 단계를 마친 뒤 완료를 기록해 주세요.")
            record = {
                "record_id": str(uuid.uuid4()),
                "execution_id": eid,
                "activity_id": activity_id,
                "activity_title": execution["activity_title"],
                "session_id": execution["session_id"],
                "completed_at": now(),
                "note": note,
            }
            execution.update(status="completed", record_id=record["record_id"])
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?)",
                (
                    record["record_id"],
                    eid,
                    execution["session_id"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            db.execute(
                "UPDATE executions SET payload=? WHERE id=?",
                (json.dumps(execution, ensure_ascii=False), eid),
            )
        return {**record, "created": True}

    def records(self, sid):
        """한 세션의 완료 기록을 최근 저장 순서로 반환합니다. 세션 범위 없는 전체 조회는 두지 않습니다."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM records WHERE session_id=? ORDER BY rowid DESC", (sid,)
            ).fetchall()
        return [json.loads(x[0]) for x in rows]

    def traces(self, sid, limit=100):
        """한 세션의 검색 모드·호출 수·시간 진단을 조회합니다. 질문 원문과 API 키는 들어 있지 않습니다."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM traces WHERE session_id=? ORDER BY rowid DESC LIMIT ?", (sid, limit)
            ).fetchall()
        return [json.loads(x[0]) for x in rows]
