"""Adpick search adapter for batch collection; persistence belongs to the catalog store."""

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit

import httpx

from .config import Settings
from .domain import Offer, ProviderError, SearchResult
from .provider_state import ProviderState

MAX_RESPONSE_BYTES = 256 * 1024
_ORIGIN = "https://biz.adpick.co.kr"
_PRICE = re.compile(r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\s*원)?")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def _https_url(value, allowed_hosts: tuple[str, ...] | None = None) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return None
    decoded = unquote(value)
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in decoded:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.fragment
            or not host.isascii()
            or host.endswith(".")
            or "." not in host
            or any(not _LABEL.fullmatch(label) for label in host.split("."))
            or not re.fullmatch(r"[a-z]{2,63}", host.rsplit(".", 1)[-1])
            or host.rsplit(".", 1)[-1] in {"localhost", "local", "internal", "invalid", "test"}
        ):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        if allowed_hosts is not None and host not in allowed_hosts:
            return None
    except ValueError:
        return None
    return value


def _price(value) -> tuple[str, int | None]:
    if type(value) is int and 0 <= value <= 1_000_000_000_000:
        return f"{value:,}원", value
    if (
        type(value) is float
        and math.isfinite(value)
        and value.is_integer()
        and 0 <= value <= 1_000_000_000_000
    ):
        return f"{int(value):,}원", int(value)
    display = _text(value, 100)
    if display is None:
        return "가격 확인 필요", None
    if not _PRICE.fullmatch(display):
        return display, None
    numeric = int(display.removesuffix("원").strip().replace(",", ""))
    return display, numeric if numeric <= 1_000_000_000_000 else None


def _purchase_url_discard_reason(value) -> str:
    """Return fixed diagnostic codes only; never include provider data or arbitrary hosts."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "MISSING_PURCHASE_URL"
    if isinstance(value, str):
        try:
            parts = urlsplit(value)
            if parts.scheme == "http":
                return (
                    "HTTP_WWW_ADPICK_PURCHASE_URL"
                    if parts.hostname == "www.adpick.co.kr"
                    else "HTTP_PURCHASE_URL"
                )
            if _https_url(value) is not None:
                return (
                    "PURCHASE_HOST_WWW_ADPICK_NOT_ALLOWED"
                    if parts.hostname == "www.adpick.co.kr"
                    else "PURCHASE_HOST_NOT_ALLOWED"
                )
        except ValueError:
            pass
    return "UNSAFE_PURCHASE_URL"


def _retry_after(value: str | None, now: float) -> int:
    if not value or len(value) > 100:
        return 60
    if value.isascii() and value.isdigit() and len(value) <= 10:
        return max(60, int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return 60
        return max(60, math.ceil(date.timestamp() - now))
    except (ValueError, TypeError, OverflowError):
        return 60


class AdpickClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.time,
    ):
        self.settings = settings
        self.clock = clock
        self._transport = transport
        self._closed = False
        self._namespace = hashlib.sha256((settings.adpick_api_key or "").encode()).hexdigest()
        self._state = ProviderState(
            settings.state_db_path,
            self._namespace,
            settings.rate_limit_per_minute,
            settings.circuit_failure_threshold,
            settings.circuit_reset_seconds,
            settings.timeout_seconds + 1,
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._transport is not None:
            try:
                await self._transport.aclose()
            except Exception:
                raise ProviderError("PROVIDER_UNAVAILABLE") from None

    async def search(self, query: str, limit: int = 20) -> SearchResult:
        if not self.settings.adpick_api_key:
            raise ProviderError("PROVIDER_NOT_CONFIGURED")
        if not self.settings.adpick_filtering_approved:
            raise ProviderError("PROVIDER_FILTERING_NOT_APPROVED")
        if self._closed:
            raise ProviderError("PROVIDER_UNAVAILABLE")
        query = _text(query, 200)
        if query is None or type(limit) is not int or not 1 <= limit <= 20:
            raise ProviderError("PROVIDER_INVALID_QUERY")
        limit = min(limit, self.settings.search_limit)
        generation = await asyncio.to_thread(self._state.reserve, self.clock())
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                payload = await self._request(query, limit)
                result = self._normalize(payload, limit)
        except (TimeoutError, httpx.TimeoutException):
            await self._failed("PROVIDER_TIMEOUT")
            raise ProviderError("PROVIDER_TIMEOUT") from None
        except ProviderError as exc:
            block = None
            if exc.code == "PROVIDER_RATE_LIMITED":
                block = max(60, exc.retry_after_seconds or 60)
            elif exc.code == "PROVIDER_AUTH_ERROR":
                block = self.settings.circuit_reset_seconds
            await self._failed(exc.code, block)
            raise ProviderError(exc.code, exc.retry_after_seconds) from None
        except Exception:
            await self._failed("PROVIDER_UNAVAILABLE")
            raise ProviderError("PROVIDER_UNAVAILABLE") from None
        await asyncio.to_thread(self._state.success, generation)
        return result

    async def _failed(self, code: str, block: int | None = None) -> None:
        # The original exception, response body and credential-bearing URL never enter logs.
        await asyncio.to_thread(self._state.failure, self.clock(), block)

    async def _request(self, query: str, limit: int):
        if self._transport is None:
            self._transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        request = httpx.Request(
            "GET",
            f"{_ORIGIN}/api/{quote(self.settings.adpick_api_key, safe='')}/search",
            params={"q": query, "limit": limit},
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            extensions={
                "timeout": {
                    name: self.settings.timeout_seconds
                    for name in ("connect", "read", "write", "pool")
                }
            },
        )
        # Using the transport directly avoids AsyncClient's INFO log containing the API key URL.
        response = await self._transport.handle_async_request(request)
        try:
            if response.status_code == 401:
                raise ProviderError("PROVIDER_AUTH_ERROR")
            if response.status_code in (403, 429):
                raise ProviderError(
                    "PROVIDER_RATE_LIMITED",
                    _retry_after(response.headers.get("retry-after"), self.clock()),
                )
            if response.status_code != 200:
                raise ProviderError("PROVIDER_UNAVAILABLE")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ProviderError("PROVIDER_INVALID_RESPONSE")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise ProviderError("PROVIDER_INVALID_RESPONSE")
            body = bytearray()
            # MockTransport commonly returns an already-buffered response.
            if response.is_stream_consumed:
                body.extend(response.content)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderError("PROVIDER_INVALID_RESPONSE")
            else:
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProviderError("PROVIDER_INVALID_RESPONSE")
                    body.extend(chunk)
            try:
                return json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise ProviderError("PROVIDER_INVALID_RESPONSE") from None
        finally:
            await response.aclose()

    def _normalize(self, payload, limit: int) -> SearchResult:
        if not isinstance(payload, dict):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        if payload.get("success") is not True:
            code = str(payload.get("code", ""))
            if code == "401":
                raise ProviderError("PROVIDER_AUTH_ERROR")
            if code in {"403", "429"}:
                raise ProviderError("PROVIDER_RATE_LIMITED", 60)
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        offers = []
        seen = set()
        reasons = Counter()
        if len(data) > limit:
            reasons["LIMIT_TRUNCATED"] = len(data) - limit
        for row in data[:limit]:
            if not isinstance(row, dict):
                reasons["INVALID_ROW"] += 1
                continue
            title = _text(row.get("title"), 500)
            if title is None:
                reasons["INVALID_TITLE"] += 1
                continue
            purchase_url = _https_url(
                row.get("commissionlink"), self.settings.allowed_purchase_hosts
            )
            if purchase_url is None:
                reasons[_purchase_url_discard_reason(row.get("commissionlink"))] += 1
                continue
            if purchase_url in seen:
                reasons["DUPLICATE"] += 1
                continue
            seen.add(purchase_url)
            display_price, price = _price(row.get("price"))
            offers.append(
                Offer(
                    offer_ref="adpick_"
                    + hashlib.sha256((self._namespace + "\0" + purchase_url).encode()).hexdigest()[
                        :24
                    ],
                    title=title,
                    display_price=display_price,
                    price_krw=price,
                    image_url=_https_url(row.get("photo")),
                    purchase_url=purchase_url,
                    merchant_code=_text(row.get("cp_code"), 100) or "",
                    merchant_name=_text(row.get("cp_name"), 200) or "",
                    fetched_at=datetime.fromtimestamp(self.clock(), timezone.utc),
                )
            )
        return SearchResult(tuple(offers), sum(reasons.values()), tuple(sorted(reasons.items())))
