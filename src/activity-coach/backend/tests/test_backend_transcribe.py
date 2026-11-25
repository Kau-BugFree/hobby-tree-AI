"""Spring 백엔드가 호출하는 POST /backend/coach/transcribe 계약을 검증합니다.

변환기는 가짜를 주입하므로 구글을 부르지 않습니다. 실제 DB와 .env는 읽지 않습니다.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.backend_bridge import read_audio
from app.config import Settings
from app.main import create_app
from app.transcribe import MAX_AUDIO_BYTES

TOKEN = "test-service-token"
URL = "/backend/coach/transcribe"


class FakeTranscriber:
    """받은 음성을 기록하고 정해 둔 글자나 오류를 돌려줍니다."""

    def __init__(self, text="오늘 저녁 뭐 먹지", error=None):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        if self.error:
            raise self.error
        return self.text


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """임시 DB와 실제 카탈로그를 쓰는 설정입니다. 구글 키 파일은 넣지 않습니다."""
    settings = Settings(
        data_dir=Path(__file__).resolve().parents[1] / "data",
        index_dir=tmp_path / "index",
        database_path=tmp_path / "coach.sqlite",
        embedding_enabled=False,
        reranker_enabled=False,
        anthropic_api_key="",
        allowed_hosts=("testserver",),  # TestClient가 보내는 Host
        backend_token=TOKEN,
    )
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    return settings


@pytest.fixture
def fake():
    return FakeTranscriber()


@pytest.fixture
def client(settings, fake):
    with TestClient(create_app(settings, transcriber=fake)) as test_client:
        yield test_client


def post(client, body=b"m4a-bytes", token=TOKEN, content_type="audio/mp4"):
    """백엔드가 보내는 형식 그대로 요청합니다. token·content_type이 None이면 그 헤더를 보내지 않습니다."""
    headers = {} if token is None else {"X-Coach-Service-Token": token}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return client.post(URL, headers=headers, content=body)


def test_returns_the_text_and_passes_the_bytes_through(client, fake):
    response = post(client)

    assert response.status_code == 200, response.text
    assert response.json() == {"text": "오늘 저녁 뭐 먹지"}
    assert fake.calls == [b"m4a-bytes"]


@pytest.mark.parametrize("content_type", ["Audio/MP4; codecs=mp4a.40.2", "audio/webm"])
def test_content_type_may_carry_case_and_parameters(client, content_type):
    assert post(client, content_type=content_type).status_code == 200


def test_requires_the_service_token(settings, client, fake):
    assert post(client, token=None).status_code == 401
    assert post(client, token="wrong-token").status_code == 401
    with TestClient(create_app(replace(settings, backend_token=""), transcriber=fake)) as closed:
        assert post(closed).status_code == 503
    assert fake.calls == []


@pytest.mark.parametrize("content_type", [None, "application/json", "multipart/form-data; boundary=x"])
def test_rejects_a_body_that_is_not_audio(client, fake, content_type):
    response = post(client, content_type=content_type)

    assert response.status_code == 415
    assert response.json() == {"detail": "Content-Type은 audio/로 시작해야 합니다."}
    assert fake.calls == []


def test_rejects_an_empty_body(client, fake):
    response = post(client, body=b"")

    assert response.status_code == 400
    assert response.json() == {"detail": "음성이 비어 있습니다."}
    assert fake.calls == []


def test_accepts_exactly_the_limit_and_rejects_one_byte_more(client, fake):
    assert MAX_AUDIO_BYTES == 7 * 1024 * 1024
    assert post(client, body=bytes(MAX_AUDIO_BYTES)).status_code == 200

    response = post(client, body=bytes(MAX_AUDIO_BYTES + 1))

    assert response.status_code == 413
    assert response.json() == {"detail": "음성 파일은 7MB 이하여야 합니다."}
    assert len(fake.calls) == 1


def test_rejects_an_oversized_chunked_body_without_content_length(client, fake):
    """Content-Length 없이 조각으로 오는 본문도 한도를 넘으면 끝까지 받지 않고 거절합니다."""
    megabyte = b"x" * 1024 * 1024

    response = post(client, body=(megabyte for _ in range(11)))

    assert response.status_code == 413
    assert fake.calls == []


def test_stops_reading_once_the_body_exceeds_the_limit():
    """TestClient는 조각 본문을 한 덩어리로 합쳐 보냅니다. 읽기를 멈추는지는 조각을 세는 가짜 요청으로 봅니다."""
    class ChunkedRequest:
        headers = {}  # Content-Length 없음

        def __init__(self):
            self.chunks_read = 0

        async def stream(self):
            for _ in range(20):
                self.chunks_read += 1
                yield b"x" * 1024 * 1024

    request = ChunkedRequest()

    with pytest.raises(HTTPException) as error:
        asyncio.run(read_audio(request))

    assert error.value.status_code == 413
    assert request.chunks_read == 8  # 7MB를 넘긴 조각에서 멈추고 나머지 12개는 받지 않는다


def test_transcriber_errors_reach_the_caller_unchanged(client, fake):
    fake.error = HTTPException(422, "말소리를 알아듣지 못했습니다.")

    response = post(client)

    assert response.status_code == 422
    assert response.json() == {"detail": "말소리를 알아듣지 못했습니다."}


def test_without_a_key_file_the_route_answers_503(settings):
    """변환기를 주입하지 않으면 실제 GoogleTranscriber가 쓰입니다. 키 파일이 없으면 구글을 부르지 않고 503입니다."""
    with TestClient(create_app(settings)) as unconfigured:
        response = post(unconfigured)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "구글 음성 인식 인증 정보(GOOGLE_APPLICATION_CREDENTIALS)가 설정되지 않았습니다."
    }


def test_openapi_describes_the_binary_body(client):
    operation = client.get("/openapi.json").json()["paths"][URL]["post"]

    assert "audio/*" in operation["requestBody"]["content"]
