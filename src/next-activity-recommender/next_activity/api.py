"""Run from the module directory with uvicorn next_activity.api:create_app --factory."""

import os
from secrets import compare_digest

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .absa import analyze_aspects
from .absa_models import (
    AnalysisResult,
    GroupedHistoryRequest,
    GroupedProfileResult,
    HistoryRequest,
    ProfileResult,
)
from .home_models import HomeRankRequest, HomeRankResult
from .feedback import analyze_feedback
from .models import (
    AuthenticationError,
    FeedbackRequest,
    FeedbackResult,
    InvalidRequest,
    RankRequest,
    RankResult,
)
from .ranking import recommend, rank_home
from .profile import build_profile, build_group_profile


def create_app(*, service_token: str | None = None) -> FastAPI:
    token = (
        os.environ.get("HOBBYTREE_NEXT_ACTIVITY_TOKEN")
        if service_token is None
        else service_token
    )
    if (
        not token
        or len(token) < 32
        or not token.isascii()
        or not token.isprintable()
        or any(c.isspace() for c in token)
    ):
        raise RuntimeError(
            "Set HOBBYTREE_NEXT_ACTIVITY_TOKEN to at least 32 printable ASCII characters without spaces"
        )

    app = FastAPI(title="HobbyTree Next Activity", version="2.1.0")
    bearer = HTTPBearer(auto_error=False, scheme_name="NextActivityServiceToken")

    def authenticate(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if credentials is None or not compare_digest(
            credentials.credentials.encode(), token.encode()
        ):
            raise HTTPException(
                401,
                detail="Invalid service credential",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic errors may contain the complete diary text in the input or a user-supplied key.
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid request",
                "types": [error["type"] for error in exc.errors()],
            },
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    error_responses = {
        401: {
            "model": AuthenticationError,
            "description": "Invalid service credential",
        },
        422: {"model": InvalidRequest, "description": "Invalid request"},
    }

    @app.post(
        "/internal/v1/activity-feedback/analyze",
        response_model=FeedbackResult,
        dependencies=[Depends(authenticate)],
        responses=error_responses,
    )
    def feedback(body: FeedbackRequest) -> dict:
        analysis = analyze_feedback(
            body.memo or "", body.notDoneReason or "", body.perceivedDifficulty
        )
        return {"recordId": body.recordId, **analysis}

    @app.post(
        "/internal/v2/activity-feedback/analyze",
        response_model=AnalysisResult,
        dependencies=[Depends(authenticate)],
        responses=error_responses,
        summary="Analyze each aspect of a completed activity record",
    )
    def analyze_record(body: FeedbackRequest) -> dict:
        return {
            "recordId": body.recordId,
            "reportedDifficulty": body.perceivedDifficulty,
            "aspects": analyze_aspects(body.memo or "", body.notDoneReason or ""),
        }

    @app.post(
        "/internal/v2/activity-feedback/profile",
        response_model=ProfileResult,
        dependencies=[Depends(authenticate)],
        responses=error_responses,
        summary="Aggregate current record analyses for one hobby tree",
    )
    def analyze_history(body: HistoryRequest) -> ProfileResult:
        return build_profile(body)

    @app.post(
        "/internal/v3/activity-feedback/profile",
        response_model=GroupedProfileResult,
        dependencies=[Depends(authenticate)],
        responses=error_responses,
        summary="Aggregate recorded preferences with optional catalog group evidence",
    )
    def group_profile(body: GroupedHistoryRequest) -> GroupedProfileResult:
        return build_group_profile(body)

    @app.post(
        "/internal/v2/next-activities/rank",
        response_model=HomeRankResult,
        dependencies=[Depends(authenticate)],
        responses=error_responses,
        summary="Score the full eligible pool and return an actionable home result",
    )
    def home_rank(body: HomeRankRequest) -> dict:
        return rank_home(body.model_dump())

    @app.post(
        "/internal/v1/next-activities/rank",
        response_model=RankResult,
        dependencies=[Depends(authenticate)],
        responses=error_responses,
    )
    def rank(body: RankRequest) -> dict:
        return recommend(body.model_dump())

    return app
