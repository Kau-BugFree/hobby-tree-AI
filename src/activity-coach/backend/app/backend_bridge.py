"""Spring 백엔드(hobby-tree-Backend)의 POST /api/v1/coach/chat이 부르는 경로입니다.

백엔드는 대화를 숫자 conversationId로 저장합니다. 이 경로가 그 번호와 코치 세션을 이어 주므로
백엔드는 세션 ID를 보관하지 않습니다. 활동은 백엔드의 숫자 ID 대신 activity_code(이 서버의 activity_id)로
알리고, 숫자 ID로 바꾸는 일은 백엔드가 합니다.
"""

import asyncio
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException

from .coach import Coach
from .config import Settings
from .models import BackendChatRequest, CoachRequest
from .store import Store, StoreError

ANSWER_VERSION = "coach-v3.0.0"
# 세션의 origin_request_id에 넣는 열쇠. 공개 경로는 v4 UUID만 이 자리에 쓰므로 겹치지 않는다.
ORIGIN_PREFIX = "backend-conversation:"
SOURCE_TYPES = {"activity": "ACTIVITY", "recipe": "RECIPE"}


def backend_reply(conversation_id: int, reply: dict) -> dict:
    """코치 응답을 Spring의 CoachChatResponse로 옮길 이름으로 바꿉니다. 세션 ID와 진단은 넘기지 않습니다."""
    references = []
    for source in reply["sources"]:
        source_type = SOURCE_TYPES.get(source["kind"], "KNOWLEDGE")
        references.append({
            "sourceType": source_type,
            "sourceCode": source["evidence_id"] if source_type == "KNOWLEDGE" else source["activity_id"],
            # 레시피는 백엔드 DB의 활동이 아니다. 활동 코드로 알리면 백엔드가 없는 활동을 찾게 된다.
            "activityCode": None if source_type == "RECIPE" else source["activity_id"],
            "title": source["title"],
        })
    return {
        "conversationId": conversation_id,
        "answer": reply["text"],
        "answerVersion": ANSWER_VERSION,
        "mode": reply["mode"],
        "references": references,
        "suggestedActivities": [
            # 활동별 추천 이유는 따로 만들지 않는다. 자료의 한 줄 요약을 보내고, 없으면 null이다.
            {"activityCode": item["activity_id"], "title": item["title"], "reason": item.get("description") or None}
            for item in reply["activities"]
            if item["kind"] == "activity"
        ],
        "replayed": bool(reply["diagnostics"].get("replayed")),
    }


def backend_router(settings: Settings, store: Store, coach: Coach, registry: dict) -> APIRouter:
    """백엔드 전용 경로를 만듭니다. 토큰을 설정하지 않은 서버에서는 모든 요청을 거절합니다."""

    def authorize(x_coach_service_token: str | None = Header(None)) -> None:
        if not settings.backend_token:
            raise HTTPException(503, "백엔드 연동 토큰(COACH_BACKEND_TOKEN)이 설정되지 않았습니다.")
        # 글자 수와 무관하게 같은 시간에 비교한다. 응답 시간으로 토큰을 한 글자씩 맞히지 못하게 한다.
        if not hmac.compare_digest((x_coach_service_token or "").encode(), settings.backend_token.encode()):
            raise HTTPException(401, "X-Coach-Service-Token이 올바르지 않습니다.")

    router = APIRouter(prefix="/backend", tags=["07 백엔드 연동"], dependencies=[Depends(authorize)])

    @router.post("/coach/chat", summary="대화 한 턴 (Spring의 POST /api/v1/coach/chat이 호출)")
    async def chat(body: BackendChatRequest) -> dict:
        """conversationId가 같으면 같은 대화로 이어 갑니다. 같은 requestId의 재전송은 저장된 답을 돌려줍니다."""
        if body.hobby_code not in registry:
            raise StoreError(422, "등록되지 않은 취미입니다.")
        # 찾기와 만들기가 한 트랜잭션이라 같은 대화의 첫 요청이 동시에 와도 세션은 하나다.
        session = await asyncio.to_thread(
            store.session_for_origin, f"{ORIGIN_PREFIX}{body.conversation_id}", body.hobby_code
        )
        reply = await coach.respond(
            CoachRequest(session_id=session["session_id"], message=body.message, request_id=body.request_id)
        )
        return backend_reply(body.conversation_id, reply)

    return router
