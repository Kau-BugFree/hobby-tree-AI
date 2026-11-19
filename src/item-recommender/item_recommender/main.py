"""Internal HTTP boundary; the BE remains owner of users, activities and Q14."""

import asyncio
import hashlib
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from .config import Settings
from .catalog_domain import CatalogStore
from .catalog_store import open_store
from .domain import Offer
from .models import (
    CuratedRegistration,
    FallbackRegistration,
    QueryRegistration,
    RecommendationRequest,
    RecommendationResponse,
)
from .provider import _https_url, _text
from .service import RecommendationService

MAX_BODY_BYTES = 64 * 1024


def error_response(status: int, code: str, message: str, details=None):
    body = {"isSuccess": False, "code": code, "message": message, "result": None}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


class BoundaryMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Buffer only bounded bodies. Avoid parsing or logging arbitrary request data.
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY_BYTES:
                return await error_response(413, "REQUEST_TOO_LARGE", "요청이 너무 큽니다.")(
                    scope, receive, send
                )
            messages.append(message)
            if not message.get("more_body", False):
                break
        index = 0

        async def replay():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        async def private_send(message):
            if message["type"] == "http.response.start":
                response_headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                response_headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ]
                )
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, replay, private_send)


def create_app(settings: Settings | None = None, store: CatalogStore | None = None, clock=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        catalog = store or open_store(settings)
        if settings.app_env == "local" or store is not None:
            await asyncio.to_thread(catalog.initialize)
        app.state.catalog = catalog
        app.state.recommendations = RecommendationService(catalog, settings, clock=clock)
        try:
            yield
        finally:
            if store is None:
                await asyncio.to_thread(catalog.close)

    app = FastAPI(
        title="HobbyTree Item Recommender",
        version="0.3.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env == "local" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env == "local" else None,
    )
    app.add_middleware(BoundaryMiddleware, settings=settings)

    async def catalog_now(request: Request):
        return await asyncio.to_thread(request.app.state.recommendations.clock)

    token_header = APIKeyHeader(name="X-Service-Token", auto_error=False)

    async def require_service_token(token: str | None = Depends(token_header)):
        if settings.service_token:
            supplied = (token or "").encode("utf-8")
            if not secrets.compare_digest(supplied, settings.service_token.encode("utf-8")):
                raise HTTPException(status_code=401)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        if error.status_code == 401:
            return error_response(401, "UNAUTHORIZED", "서비스 인증이 필요합니다.")
        if error.detail == "STORAGE_NOT_APPROVED":
            return error_response(
                403, "STORAGE_NOT_APPROVED", "상품 저장·재사용 허용 설정을 확인해주세요."
            )
        if error.status_code == 422:
            return error_response(
                422, "INVALID_CATALOG_INPUT", "상품·검색어·유효기간을 확인해주세요."
            )
        return error_response(error.status_code, "HTTP_ERROR", "요청을 처리할 수 없습니다.")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Pydantic's raw error includes the user's input; return field/type only.
        details = [
            {"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in error.errors()
        ]
        return error_response(422, "INVALID_REQUEST", "요청 형식을 확인해주세요.", details)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "item-recommender",
            "provider": "ADPICK",
            "servingMode": "CATALOG_ONLY",
            "catalogBackend": "postgresql" if settings.catalog_database_url else "sqlite",
            "storageApproved": settings.storage_approved,
            "upstreamCallsOnRequest": False,
            "batchWindow": "03:00-05:00 Asia/Seoul",
            "providerConnectivityChecked": False,
        }

    @app.post(
        "/internal/v1/item-recommendations",
        response_model=RecommendationResponse,
        dependencies=[Depends(require_service_token)],
    )
    async def recommend(payload: RecommendationRequest, request: Request):
        try:
            return await request.app.state.recommendations.recommend(payload)
        except Exception:
            return error_response(
                503,
                "CATALOG_UNAVAILABLE",
                "상품 정보를 확인 중이에요. 준비물 안내를 먼저 확인해주세요.",
            )

    @app.post("/internal/v1/catalog/queries", dependencies=[Depends(require_service_token)])
    async def register_queries(payload: QueryRegistration, request: Request):
        try:
            count = await asyncio.to_thread(
                request.app.state.catalog.register_queries,
                payload.queries,
                await catalog_now(request),
            )
            return {
                "isSuccess": True,
                "code": "QUERIES_REGISTERED",
                "result": {"registeredCount": count},
            }
        except ValueError:
            raise HTTPException(422) from None
        except Exception:
            return error_response(503, "CATALOG_UNAVAILABLE", "검색어를 등록할 수 없습니다.")

    @app.put("/internal/v1/catalog/fallbacks", dependencies=[Depends(require_service_token)])
    async def register_fallbacks(payload: FallbackRegistration, request: Request):
        try:
            await asyncio.to_thread(
                request.app.state.catalog.set_fallbacks,
                payload.primary_query,
                payload.fallback_queries,
                await catalog_now(request),
            )
            return {"isSuccess": True, "code": "FALLBACKS_REGISTERED", "result": None}
        except ValueError:
            raise HTTPException(422) from None
        except Exception:
            return error_response(503, "CATALOG_UNAVAILABLE", "대체 검색어를 등록할 수 없습니다.")

    @app.put("/internal/v1/catalog/curated", dependencies=[Depends(require_service_token)])
    async def register_curated(payload: CuratedRegistration, request: Request):
        if not settings.storage_approved:
            raise HTTPException(403, detail="STORAGE_NOT_APPROVED")
        try:
            fetched_at = await catalog_now(request)
        except Exception:
            return error_response(503, "CATALOG_UNAVAILABLE", "상품 정보를 확인 중이에요.")
        if (
            payload.expires_at.tzinfo is None
            or not fetched_at
            < payload.expires_at
            <= fetched_at + timedelta(seconds=settings.retention_seconds)
        ):
            raise HTTPException(422)
        offers = []
        seen = set()
        for product in payload.products:
            title = _text(product.title, 500)
            purchase_url = _https_url(product.purchase_url, settings.allowed_purchase_hosts)
            if not title or not purchase_url or purchase_url in seen:
                raise HTTPException(422)
            seen.add(purchase_url)
            offers.append(
                Offer(
                    offer_ref="curated_"
                    + hashlib.sha256(
                        (product.merchant_code + "\0" + purchase_url).encode()
                    ).hexdigest()[:24],
                    title=title,
                    display_price=f"{product.price_krw:,}원"
                    if product.price_krw is not None
                    else "가격 확인 필요",
                    price_krw=product.price_krw,
                    image_url=_https_url(product.image_url),
                    purchase_url=purchase_url,
                    merchant_code=product.merchant_code,
                    merchant_name=product.merchant_name,
                    fetched_at=fetched_at,
                )
            )
        try:
            revision = await asyncio.to_thread(
                request.app.state.catalog.put_curated,
                payload.query,
                offers,
                fetched_at,
                payload.expires_at,
            )
            return {
                "isSuccess": True,
                "code": "CURATED_PRODUCTS_REGISTERED",
                "result": {"catalogRevision": revision},
            }
        except ValueError:
            raise HTTPException(422) from None
        except Exception:
            return error_response(503, "CATALOG_UNAVAILABLE", "검수 상품을 등록할 수 없습니다.")

    return app


app = create_app()
