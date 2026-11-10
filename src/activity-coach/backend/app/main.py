"""Swagger로 바로 실행할 수 있는 활동코치 API. 로컬 시연은 단일 worker로 실행합니다.

모듈 전역 app을 두지 않습니다. import만으로 실제 .env·운영 DB·검색 모델을 읽지 않도록
서버는 `uvicorn app.main:create_app --factory`로 시작합니다.
"""

import asyncio
import json
from fastapi import FastAPI, Body, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from .config import Settings
from .models import (
    SessionCreate,
    CoachRequest,
    SearchRequest,
    SelectRequest,
    ExecutionCreate,
    ProgressUpdate,
    RecordCreate,
    AskRequest,
)
from .store import Store, StoreError
from .retrieval import Retrieval
from .claude import ClaudeService
from .coach import Coach
from .ask import RagAsk
from .backend_bridge import backend_router


def public_session(session):
    """세션 응답에서 서버 내부 값을 뺍니다. origin_request_id는 세션을 다시 찾는 열쇠이고 revision은 저장 충돌 검사용입니다."""
    return {key: value for key, value in session.items() if key not in {"origin_request_id", "revision"}}


def without_session_id(payload):
    """실행·완료 기록 응답에서 session_id를 뺍니다. 실행 ID만 아는 사람이 대화 전체의 열쇠를 얻지 못하게 합니다."""
    return {key: value for key, value in payload.items() if key != "session_id"}


def create_app(settings=None, retrieval=None, llm=None):
    """테스트에서는 별도 DB와 가짜 외부 HTTP 클라이언트를 주입할 수 있습니다."""
    settings = settings or Settings.from_env()
    store = Store(settings.database_path)
    retrieval = retrieval or Retrieval(settings)
    llm = llm or ClaudeService(settings)
    coach = Coach(store, retrieval, llm)
    asker = RagAsk(retrieval, llm)
    registry_path = settings.data_dir / "hobbies.json"
    registry = (
        json.loads(registry_path.read_text(encoding="utf-8"))
        if registry_path.exists()
        else {"cooking": {"label": "요리"}}
    )
    api = FastAPI(
        title="HobbyTree 활동코치",
        version="3.0.0",
        description="요리부터 시작하는 검색 우선 활동코치. **로컬 단일 사용자 시연용**입니다. ① 세션 생성 ② 추천 ③ 선택 ④ 설명 ⑤ 완료 기록 순서로 시연합니다. Claude API 키는 서버 .env에만 입력합니다. RAG 검증은 06 RAG 검증의 /rag/ask를 사용합니다.",
        openapi_tags=[
            {"name": "01 준비", "description": "상태와 대화 세션"},
            {"name": "02 코치", "description": "검색·후속 질문·조건부 생성"},
            {"name": "03 활동", "description": "활동 선택과 단계 진행"},
            {"name": "04 기록", "description": "저장과 재전송 검증"},
            {"name": "05 진단", "description": "실제 호출 수와 검색 모드"},
            {"name": "06 RAG 검증", "description": "검색 순위·근거·Claude 답변을 한 번에 확인"},
            {"name": "07 백엔드 연동", "description": "Spring 백엔드 전용. X-Coach-Service-Token 헤더 필요"},
        ],
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type"],
    )
    # 허용한 Host로 온 요청만 받는다. 다른 사이트가 이름만 이 서버로 돌려(DNS 리바인딩) 응답을 읽는 것을 막는다.
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    api.state.settings = settings
    api.state.store = store
    api.state.retrieval = retrieval
    api.state.coach = coach

    @api.exception_handler(StoreError)
    async def store_error(_request: Request, error: StoreError):
        """업무 오류를 앱과 Swagger가 공통으로 읽는 detail 메시지로 변환합니다."""
        return JSONResponse(status_code=error.status, content={"detail": error.message})

    @api.get("/health", tags=["01 준비"], summary="서버·검색·Claude 설정 상태 확인")
    def health():
        """키 원문은 제외하고 설정 여부, 색인 수, 실제 검색 준비 상태를 반환합니다."""
        return {
            "status": "ok",
            "version": "3.0.0",
            "mode": "local_single_user_demo",
            "llm_configured": bool(settings.anthropic_api_key),
            "llm_model": settings.claude_model,
            "retrieval": retrieval.status(),
            "supported_hobbies": list(registry),
        }

    @api.get("/hobbies", tags=["01 준비"])
    def hobbies():
        """hobbies.json에 등록된 취미를 나열합니다. 첫 출시에는 요리만 등록합니다."""
        return {"items": [{"hobby_key": k, **v} for k, v in registry.items()]}

    @api.post("/sessions", tags=["01 준비"], summary="1. 새 대화 세션 생성")
    def session_create(body: SessionCreate):
        """허용된 취미의 새 대화를 만들고 후속 요청에 필요한 session_id를 반환합니다."""
        if body.hobby_key not in registry:
            raise StoreError(422, "등록되지 않은 취미입니다.")
        return store.create_session(body.hobby_key)

    @api.get(
        "/sessions/{session_id}", tags=["01 준비"], summary="세션·대화·선택 활동 복원"
    )
    def session_get(session_id: str):
        """앱 재실행 시 대화, 추천 후보, 현재 단계와 실행 ID를 복원합니다."""
        return public_session(store.get_session(session_id))

    @api.post(
        "/coach/messages",
        tags=["02 코치"],
        summary="2. 질문 보내기 (추천·선택·설명·완료)",
    )
    async def message(
        body: CoachRequest = Body(
            openapi_examples={
                "recommend": {
                    "summary": "추천: 생성 LLM 호출 0회",
                    "value": {
                        "session_id": "세션 생성 응답의 session_id",
                        "request_id": "demo-1",
                        "message": "오늘 20분 안에 할 요리 활동 추천해줘",
                    },
                },
                "select": {
                    "summary": "직전 추천의 두 번째 선택",
                    "value": {
                        "session_id": "같은 session_id",
                        "request_id": "demo-2",
                        "message": "두 번째 걸로 할게",
                    },
                },
                "explain": {
                    "summary": "근거 기반 설명 (생성·인용·의미 점검)",
                    "value": {
                        "session_id": "같은 session_id",
                        "request_id": "demo-3",
                        "message": "현재 단계를 쉽게 설명해줘",
                    },
                },
                "complete": {
                    "summary": "명시적 완료 선언과 기록",
                    "value": {
                        "session_id": "같은 session_id",
                        "request_id": "demo-4",
                        "message": "완료했어",
                    },
                },
                "troubleshoot": {
                    "summary": "V2: 문제 상태와 자료 묶음 조회",
                    "value": {"session_id": "같은 session_id", "request_id": "demo-v2-1",
                              "activity_id": "cook-0-2", "message": "2단계 계량이 잘 안돼"},
                },
                "clarification_reply": {
                    "summary": "V2: 직전 확인 질문에 대한 답변",
                    "value": {"session_id": "같은 session_id", "request_id": "demo-v2-2", "message": "1번"},
                },
            }
        )
    ):
        """질문을 코치 처리기에 전달합니다. 추천과 조회는 생성 API 없이 처리합니다."""
        return await coach.respond(body)

    @api.post("/search", tags=["02 코치"], summary="조건 필터와 검색 결과 직접 확인")
    def search(
        body: SearchRequest = Body(
            # 자동 예시의 도구 'string'·예산 0 대신 바로 실행할 수 있는 조건을 제공한다.
            openapi_examples={
                "activities": {
                    "summary": "활동 검색: 간장 드레싱",
                    "value": {"query": "간장 드레싱", "scope": "activities", "limit": 3},
                },
                "with_time_limit": {
                    "summary": "활동 검색: 20분 이내",
                    "value": {"query": "간장 드레싱", "scope": "activities",
                              "constraints": {"max_minutes": 20}, "limit": 3},
                },
                "recipes": {
                    "summary": "레시피 검색: 감자 수프 (레시피 색인 필요)",
                    "value": {"query": "감자 수프 레시피", "scope": "recipes", "limit": 3},
                },
                "knowledge": {
                    "summary": "코칭 자료 검색: 간장 드레싱 계량",
                    "value": {"query": "간장 드레싱 계량", "scope": "knowledge", "limit": 3},
                },
            }
        )
    ):
        """검색 결과와 필터·검색 방식 진단을 대화 처리와 독립적으로 확인합니다."""
        if body.hobby_key not in registry:
            raise StoreError(422, "등록되지 않은 취미입니다.")
        return retrieval.search(
            body.query,
            body.hobby_key,
            body.constraints.model_dump(),
            body.limit,
            body.scope,
        )

    @api.get("/activities", tags=["03 활동"])
    def activities(
        q: str = "", hobby_key: str = "cooking", limit: int = Query(20, ge=1, le=100)
    ):
        """기본 활동 카탈로그의 일부를 조회합니다. total은 이번 반환 건수입니다."""
        items = retrieval.list_activities(q, hobby_key, limit)
        return {"items": items, "total": len(items)}

    @api.get("/activities/{activity_id}", tags=["03 활동"])
    def activity(activity_id: str):
        """화면 상세 조회에 필요한 정규화된 활동 한 건을 반환합니다."""
        return coach.activity(activity_id)

    @api.post("/sessions/{session_id}/select", tags=["03 활동"])
    async def select(session_id: str, body: SelectRequest):
        """카드에서 선택한 활동을 대화에 반영하고 이전 진행 맥락을 정리합니다."""
        def apply():
            session = store.get_session(session_id)
            coach.select(session, body.activity_id)
            store.save_session(session)
            return public_session(session)

        # 저장소 호출은 스레드에서 실행한다. SQLite 잠금 대기가 다른 사용자의 요청까지 멈추지 않게 한다.
        await asyncio.to_thread(store.get_session, session_id)  # 없는 세션 ID로는 잠금을 만들지 않는다.
        async with coach.locks.setdefault(session_id, asyncio.Lock()):
            return await asyncio.to_thread(apply)

    @api.post(
        "/sessions/{session_id}/executions",
        tags=["03 활동"],
        summary="선택 활동 실행 시작 (반복 활동은 새 request_id)",
    )
    async def execution_start(session_id: str, body: ExecutionCreate):
        """수행을 생성합니다. 동일 request_id 재전송은 같은 실행을 반환합니다."""
        def start():
            session = store.get_session(session_id)
            activity = coach.select(session, body.activity_id)

            def attach(execution):
                coach.attach_execution(session, execution)
                return session

            # 새 실행과 세션 연결은 한 트랜잭션으로 저장한다. 세션 저장이 거부되면 실행도 만들지 않는다.
            # 재전송은 기존 결과만 돌려준다. 세션을 저장하지 않으므로 현재 실행·단계는 그대로다.
            execution, _ = store.start_execution(session_id, activity, body.request_id, attach)
            return without_session_id(execution)

        await asyncio.to_thread(store.get_session, session_id)  # 없는 세션 ID로는 잠금을 만들지 않는다.
        async with coach.locks.setdefault(session_id, asyncio.Lock()):
            return await asyncio.to_thread(start)

    @api.get("/executions/{execution_id}", tags=["03 활동"])
    def execution_get(execution_id: str):
        """진행 화면을 다시 열었을 때 단계 체크 상태와 완료 여부를 읽습니다."""
        return without_session_id(store.get_execution(execution_id))

    @api.patch(
        "/executions/{execution_id}",
        tags=["03 활동"],
        summary="전체 단계 수만큼 true/false 저장",
    )
    def execution_patch(execution_id: str, body: ProgressUpdate):
        """전체 단계에 대응하는 체크 배열을 검증한 뒤 저장합니다."""
        return without_session_id(store.update_progress(execution_id, body.done))

    @api.post(
        "/activities/{activity_id}/records",
        tags=["04 기록"],
        summary="완료 저장: 같은 실행의 재전송은 중복 생성 안 함",
    )
    def record_create(activity_id: str, body: RecordCreate):
        """전 단계 완료 여부를 검사하고 실행별 하나의 완료 기록만 저장합니다."""
        activity = coach.activity(activity_id)
        return without_session_id(store.complete(
            activity["activity_id"], body.execution_id, body.request_id, body.note
        ))

    @api.get("/records", tags=["04 기록"])
    def records(session_id: str = Query(min_length=1, max_length=80)):
        """한 세션의 완료 기록을 조회합니다. 다른 세션의 기록과 ID가 드러나지 않도록 session_id는 필수입니다."""
        store.get_session(session_id)
        items = [without_session_id(record) for record in store.records(session_id)]
        return {"items": items, "total": len(items)}

    @api.get(
        "/diagnostics/traces",
        tags=["05 진단"],
        summary="API 키·질문 원문 없이 처리 경로와 호출 수 확인",
    )
    def traces(session_id: str = Query(min_length=1, max_length=80), limit: int = Query(100, ge=1, le=500)):
        """한 세션의 실제 LLM 호출 수와 검색 경로를 확인합니다. 검색 순위에는 다른 사용자가 찾은 활동이 드러나므로 session_id는 필수입니다."""
        store.get_session(session_id)
        return {"items": store.traces(session_id, limit)}

    @api.post("/rag/ask", tags=["06 RAG 검증"], summary="검색 순위·근거·Claude 답변을 한 번에 확인")
    async def rag_ask(
        body: AskRequest = Body(
            # 스키마 자동 예시는 도구 'string'·예산 0 같은 자리표시 조건을 채워 모든 활동을 걸러낸다.
            openapi_examples={
                "basic": {
                    "summary": "조건 없이 질문",
                    "value": {"question": "간장 드레싱 만들 때 큰술과 작은술 계량은 어떻게 해?", "top_k": 3},
                },
                "with_time_limit": {
                    "summary": "시간 조건 추가 (20분 이내 활동만 근거로 사용)",
                    "value": {"question": "달걀로 할 수 있는 요리 알려줘", "constraints": {"max_minutes": 20}, "top_k": 3},
                },
            }
        )
    ):
        """대화 분기 없이 검색 → 근거 → Claude 답변 → 검증을 실행하고 단계별 점수와 지연을 반환합니다."""
        if body.hobby_key not in registry:
            raise StoreError(422, "등록되지 않은 취미입니다.")
        return await asker.answer(body.question, body.hobby_key, body.constraints.model_dump(), body.top_k)

    api.include_router(backend_router(settings, store, coach, registry))
    return api
