"""HTTP 요청을 추천 엔진에 연결하고 422·500·503 응답을 구분한다."""
import logging

from fastapi import APIRouter, HTTPException, Request

from app.models import ErrorResponse, RecommendationRequest, RecommendationResponse
from app.service import AnswerValidationError, RecommendationService

logger = logging.getLogger(__name__)

# 추천·카탈로그·설문·상태 확인에서 공유하는 OpenAPI 오류 명세다.
SERVICE_UNAVAILABLE_RESPONSE = {
    503: {"model": ErrorResponse, "description": "추천 서비스가 준비되지 않아 일시적으로 이용할 수 없습니다."},
}

router = APIRouter(prefix="/api/v1", tags=["recommendations"], responses=SERVICE_UNAVAILABLE_RESPONSE)


def get_recommender(request: Request) -> RecommendationService:
    """앱 수명주기에서 준비된 엔진을 가져오며, 없으면 503을 반환한다."""
    service = getattr(request.app.state, "recommender", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="추천 서비스를 일시적으로 이용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        )
    return service


@router.post(
    "/recommendations",
    response_model=RecommendationResponse,
    responses={500: {"model": ErrorResponse, "description": "추천 처리 중 서버 내부 오류가 발생했습니다."}},
)
def recommend(body: RecommendationRequest, request: Request):
    """설문을 계산하고 입력 오류·명시적 HTTP 오류·예상하지 못한 예외를 구분한다."""
    service = get_recommender(request)
    try:
        return service.recommend(body)
    except AnswerValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # 내부 진단 정보는 로그에 기록하고 응답에는 사용자용 메시지만 포함한다.
        logger.exception("Recommendation request failed")
        raise HTTPException(
            status_code=500,
            detail="추천 처리 중 서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        ) from exc


@router.get("/catalog")
def catalog(request: Request):
    """현재 엔진의 취미·활동 코드와 자원 목록을 반환한다(DB ID 목록이 아님)."""
    return get_recommender(request).data.catalog.model_dump()


@router.get("/questionnaire")
def questionnaire(request: Request):
    """클라이언트가 제출할 수 있는 정확한 질문 키와 선택지 규칙을 반환한다."""
    return get_recommender(request).data.questionnaire.model_dump()
