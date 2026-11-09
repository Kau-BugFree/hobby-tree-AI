"""환경 설정·데이터 로딩·CORS와 API 경로를 조립하는 FastAPI 진입점."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import SERVICE_UNAVAILABLE_RESPONSE, get_recommender, router
from app.config import DataBundle
from app.service import RecommendationService


def create_app(data_directory: Path | None = None) -> FastAPI:
    """테스트용 데이터 경로 또는 환경 변수로 독립적인 앱 인스턴스를 만든다."""
    directory = data_directory or Path(os.environ.get("RECOMMENDER_DATA_DIR", str(Path(__file__).parent / "data")))

    @asynccontextmanager
    async def lifespan(application):
        """요청 수락 전에 데이터를 검증하고 종료 시 엔진 참조를 해제한다."""
        application.state.recommender = RecommendationService(DataBundle(directory))
        try:
            yield
        finally:
            application.state.recommender = None

    application = FastAPI(
        title="HobbyTree Recommendation API", version="1.0.0",
        description="온보딩 답변에 따른 설명 가능한 규칙 기반 추천. 점수는 확률이 아닙니다.",
        lifespan=lifespan,
    )
    # 웹 앱의 실제 출처만 허용한다. CORS 설정은 로그인 토큰 검증을 대신하지 않는다.
    origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:8081,http://localhost:19006").split(",") if o.strip()]
    application.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                               allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"])
    application.include_router(router)

    @application.get("/health", tags=["health"], responses=SERVICE_UNAVAILABLE_RESPONSE)
    def health(request: Request):
        """엔진 준비 상태와 로드된 규칙·카탈로그 버전을 반환한다."""
        data = get_recommender(request).data
        return {"status": "ok", "rules_version": data.rules.version, "catalog_version": data.catalog.version}

    return application


# uvicorn app.main:app에서 찾는 ASGI 애플리케이션이다.
app = create_app()
