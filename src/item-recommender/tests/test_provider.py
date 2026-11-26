import asyncio
import json
import logging
from dataclasses import replace
from urllib.parse import quote

import httpx
import httpcore
import pytest
from httpcore._backends.mock import AsyncMockBackend

from item_recommender.config import Settings
from item_recommender.domain import ProviderError
from item_recommender.provider import AdpickClient, MAX_RESPONSE_BYTES


@pytest.fixture
def settings(tmp_path):
    return Settings(
        adpick_api_key="offline-test-key/with?special#chars",
        adpick_filtering_approved=True,
        state_db_path=tmp_path / "state.sqlite3",
    )


def row(**changes):
    return {
        "title": "전자레인지용 그릇",
        "price": "29,900원",
        "photo": "https://cdn.adpick.co.kr/image.png",
        "cp_code": "SHOP",
        "cp_name": "테스트 몰",
        "commissionlink": "https://link.adpick.co.kr/offer",
        **changes,
    }


async def search_with(settings, response, **kwargs):
    client = AdpickClient(settings, httpx.MockTransport(lambda request: response), **kwargs)
    try:
        return await client.search("그릇")
    finally:
        await client.aclose()


def test_normalization_request_and_secret_safe_logs(settings, caplog):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": [row()]})

    async def run():
        client = AdpickClient(settings, httpx.MockTransport(handle), clock=lambda: 1000.0)
        try:
            result = await client.search("그릇", limit=20)
            again = await client.search("그릇", limit=20)
            assert result.offers[0].offer_ref == again.offers[0].offer_ref
            return result
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(run())
    offer = result.offers[0]
    assert offer.price_krw == 29900
    assert offer.display_price == "29,900원"
    assert offer.provider_product_id is None
    assert offer.fetched_at.timestamp() == 1000
    assert offer.fetched_at.utcoffset().total_seconds() == 0
    assert result.discarded_count == 0
    request = requests[0]
    assert request.url.host == "biz.adpick.co.kr"
    assert b"offline-test-key%2Fwith%3Fspecial%23chars" in request.url.raw_path
    assert request.url.params["q"] == "그릇"
    assert request.url.params["limit"] == "20"
    assert request.headers["accept-encoding"] == "identity"
    assert settings.adpick_api_key not in caplog.text
    assert "biz.adpick.co.kr/api/" not in caplog.text
    assert settings.adpick_api_key not in repr(offer)


@pytest.mark.parametrize(
    "price,expected",
    [
        ("무료배송", None),
        ("1,000~2,000원", None),
        ("-100원", None),
        ("12,34원", None),
        (None, None),
        (29900, 29900),
        (29900.0, 29900),
        (29900.5, None),
        (True, None),
        ("0원", 0),
        ("9900", 9900),
    ],
)
def test_unknown_price_is_not_invented(settings, price, expected):
    result = asyncio.run(
        search_with(
            settings, httpx.Response(200, json={"success": True, "data": [row(price=price)]})
        )
    )
    assert result.offers[0].price_krw == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://link.adpick.co.kr/offer",
        "https://link.adpick.co.kr.evil.com/offer",
        "https://link.adpick.co.kr@evil.com/offer",
        "https://127.0.0.1/offer",
        "https://link.adpick.co.kr:8443/offer",
        "https://link.adpick.co.kr./offer",
        "javascript:alert(1)",
        "https://link.adpick.co.kr/%0aheader",
        "https://link.adpick.co.kr\\@evil.com/offer",
        "https://link.adpick.co.kr/offer#fragment",
    ],
)
def test_unsafe_purchase_urls_are_discarded(settings, url):
    result = asyncio.run(
        search_with(
            settings, httpx.Response(200, json={"success": True, "data": [row(commissionlink=url)]})
        )
    )
    assert result.offers == ()
    assert result.discarded_count == 1


@pytest.mark.parametrize(
    "photo", [None, "http://cdn.adpick.co.kr/a", "https://127.1/a", "https://localhost/a"]
)
def test_invalid_image_does_not_discard_offer(settings, photo):
    result = asyncio.run(
        search_with(
            settings, httpx.Response(200, json={"success": True, "data": [row(photo=photo)]})
        )
    )
    assert len(result.offers) == 1
    assert result.offers[0].image_url is None


def test_discards_malformed_and_duplicate_rows(settings):
    result = asyncio.run(
        search_with(
            settings,
            httpx.Response(
                200, json={"success": True, "data": [None, row(title=[]), row(), row()]}
            ),
        )
    )
    assert len(result.offers) == 1
    assert result.discarded_count == 3


@pytest.mark.parametrize(
    "payload", [None, [], {"success": 1, "data": []}, {"success": True, "data": {}}]
)
def test_invalid_envelopes(settings, payload):
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(search_with(settings, httpx.Response(200, json=payload)))


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, "PROVIDER_AUTH_ERROR"),
        (403, "PROVIDER_RATE_LIMITED"),
        (429, "PROVIDER_RATE_LIMITED"),
        (500, "PROVIDER_UNAVAILABLE"),
        (302, "PROVIDER_UNAVAILABLE"),
    ],
)
def test_http_errors_are_normalized_without_redirects(settings, code, expected):
    with pytest.raises(ProviderError, match=expected):
        asyncio.run(
            search_with(
                settings,
                httpx.Response(
                    code, headers={"location": "https://evil.com"}, text=settings.adpick_api_key
                ),
            )
        )


@pytest.mark.parametrize(
    "code,expected",
    [(401, "PROVIDER_AUTH_ERROR"), (403, "PROVIDER_RATE_LIMITED"), (429, "PROVIDER_RATE_LIMITED")],
)
def test_http_200_error_envelopes(settings, code, expected):
    with pytest.raises(ProviderError, match=expected):
        asyncio.run(
            search_with(settings, httpx.Response(200, json={"success": False, "code": code}))
        )


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"adpick_api_key": None}, "PROVIDER_NOT_CONFIGURED"),
        ({"adpick_filtering_approved": False}, "PROVIDER_FILTERING_NOT_APPROVED"),
    ],
)
def test_missing_configuration_makes_no_request_or_state(settings, change, expected):
    calls = []
    client = AdpickClient(
        replace(settings, **change), httpx.MockTransport(lambda r: calls.append(r))
    )
    with pytest.raises(ProviderError, match=expected):
        asyncio.run(client.search("그릇"))
    assert calls == []
    assert not settings.state_db_path.exists()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
        httpx.Response(200, content=b"invalid json"),
        httpx.Response(200, content=b"{}", headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}),
        httpx.Response(200, content=b"{}", headers={"content-encoding": "unknown"}),
    ],
)
def test_bounded_invalid_responses(settings, response):
    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(search_with(settings, response))


def test_overall_timeout_and_exception_redaction(settings):
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"success": True, "data": []})

    async def run():
        client = AdpickClient(replace(settings, timeout_seconds=0.01), httpx.MockTransport(slow))
        try:
            await client.search("그릇")
        finally:
            await client.aclose()

    with pytest.raises(ProviderError, match="PROVIDER_TIMEOUT") as error:
        asyncio.run(run())
    assert settings.adpick_api_key not in str(error.value)


def test_network_exception_does_not_leak_request_url(settings):
    def failed(request):
        raise httpx.ConnectError(str(request.url), request=request)

    client = AdpickClient(settings, httpx.MockTransport(failed))
    with pytest.raises(ProviderError, match="PROVIDER_UNAVAILABLE") as error:
        asyncio.run(client.search("그릇"))
    assert settings.adpick_api_key not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_state_contains_only_hash_and_numeric_operating_state(settings):
    asyncio.run(search_with(settings, httpx.Response(200, json={"success": True, "data": [row()]})))
    raw = settings.state_db_path.read_bytes()
    for secret in (settings.adpick_api_key, "그릇", "전자레인지용", "link.adpick.co.kr", "29900"):
        assert secret.encode() not in raw


def test_httpcore_debug_log_does_not_contain_credentials(settings, caplog):
    body = json.dumps({"success": True, "data": []}).encode()
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    transport._pool = httpcore.AsyncConnectionPool(
        network_backend=AsyncMockBackend(
            [
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            ]
        )
    )

    async def run():
        client = AdpickClient(settings, transport)
        try:
            return await client.search("secret-free-product-query")
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert any(record.name.startswith("httpcore") for record in caplog.records)
    assert settings.adpick_api_key not in caplog.text
    assert "offline-test-key" not in caplog.text
    assert "secret-free-product-query" not in caplog.text
    assert "/api/" not in caplog.text


def test_streamed_response_stops_when_limit_exceeded(settings):
    consumed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(10):
                consumed.append(i)
                yield b"x" * (MAX_RESPONSE_BYTES // 2)

    with pytest.raises(ProviderError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(search_with(settings, httpx.Response(200, stream=Stream())))
    assert consumed == [0, 1, 2]


def test_timeout_covers_slow_stream(settings):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"success":true,'
            await asyncio.sleep(0.1)
            yield b'"data":[]}'

    with pytest.raises(ProviderError, match="PROVIDER_TIMEOUT"):
        asyncio.run(
            search_with(
                replace(settings, timeout_seconds=0.01), httpx.Response(200, stream=Stream())
            )
        )


def test_discard_diagnostics_are_fixed_codes_without_provider_data(settings):
    secret = "diagnostic-secret-that-must-not-be-returned"
    data = [
        secret,
        row(title=None),
        row(commissionlink=None),
        row(commissionlink=f"javascript:{secret}"),
        row(commissionlink=f"https://untrusted.example.com/{secret}"),
        row(commissionlink=f"https://www.adpick.co.kr/{secret}"),
        row(commissionlink=f"http://link.adpick.co.kr/{secret}"),
        row(commissionlink=f"http://www.adpick.co.kr/{secret}"),
        row(),
        row(),
    ]
    result = asyncio.run(
        search_with(settings, httpx.Response(200, json={"success": True, "data": data}))
    )
    assert len(result.offers) == 1
    assert result.discarded_count == 9
    assert dict(result.discard_reasons) == {
        "INVALID_ROW": 1,
        "INVALID_TITLE": 1,
        "MISSING_PURCHASE_URL": 1,
        "UNSAFE_PURCHASE_URL": 1,
        "PURCHASE_HOST_NOT_ALLOWED": 1,
        "PURCHASE_HOST_WWW_ADPICK_NOT_ALLOWED": 1,
        "HTTP_PURCHASE_URL": 1,
        "HTTP_WWW_ADPICK_PURCHASE_URL": 1,
        "DUPLICATE": 1,
    }
    diagnostics = repr(result.discard_reasons)
    for sensitive in (secret, settings.adpick_api_key, "untrusted.example.com", "전자레인지용"):
        assert sensitive not in diagnostics


def test_discard_diagnostics_count_one_reason_per_row_and_truncation(settings):
    client = AdpickClient(settings, httpx.MockTransport(lambda r: None))
    result = client._normalize(
        {
            "success": True,
            "data": [
                row(title=None, commissionlink="javascript:unsafe"),
                row(),
                row(),
                row(),
            ],
        },
        limit=2,
    )
    assert len(result.offers) == 1
    assert result.discarded_count == 3
    assert dict(result.discard_reasons) == {"INVALID_TITLE": 1, "LIMIT_TRUNCATED": 2}


def test_discard_diagnostics_do_not_change_url_allowlist(settings):
    client = AdpickClient(settings, httpx.MockTransport(lambda r: None))
    result = client._normalize(
        {
            "success": True,
            "data": [
                row(commissionlink="https://www.adpick.co.kr/offer"),
                row(commissionlink="http://www.adpick.co.kr/offer"),
            ],
        },
        limit=20,
    )
    assert result.offers == ()
    assert result.discarded_count == 2
    assert dict(result.discard_reasons) == {
        "PURCHASE_HOST_WWW_ADPICK_NOT_ALLOWED": 1,
        "HTTP_WWW_ADPICK_PURCHASE_URL": 1,
    }


def test_biz_purchase_host_defaults_and_override(monkeypatch):
    monkeypatch.delenv("ADPICK_ALLOWED_PURCHASE_HOSTS", raising=False)
    expected = ("link.adpick.co.kr", "deg.kr", "adpick.co.kr", "biz.adpick.co.kr")
    assert (
        Settings().allowed_purchase_hosts == Settings.from_env().allowed_purchase_hosts == expected
    )
    monkeypatch.setenv("ADPICK_ALLOWED_PURCHASE_HOSTS", "deg.kr")
    assert Settings.from_env().allowed_purchase_hosts == ("deg.kr",)


def test_biz_purchase_host_normalization(settings):
    good = "https://biz.adpick.co.kr/sample?ref=spoon"
    raw_urls = [
        good,
        "http://biz.adpick.co.kr/sample",
        "https://biz.adpick.co.kr.example.com/sample",
        "https://biz.adpick.co.kr/api/old-fixture-key/directlink?url=sample",
        "https://biz.adpick.co.kr/sample?api_key=old-fixture-key",
        "https://biz.adpick.co.kr/sample?ref=" + quote(settings.adpick_api_key, safe=""),
    ]
    response = httpx.Response(
        200, json={"success": True, "data": [row(commissionlink=url) for url in raw_urls]}
    )
    result = asyncio.run(search_with(settings, response))
    assert [(o.price_krw, o.purchase_url) for o in result.offers] == [(29900, good)]
    assert dict(result.discard_reasons) == {
        "HTTP_PURCHASE_URL": 1,
        "PURCHASE_HOST_NOT_ALLOWED": 1,
        "UNSAFE_PURCHASE_URL": 3,
    }
