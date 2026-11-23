"""GoogleTranscriber가 구글 Speech-to-Text v2에 보내는 요청과, 결과·오류를 해석하는 방식을 검증합니다.

구글은 부르지 않습니다. httpx.MockTransport가 응답을 대신하고, 서비스 계정 키는 테스트가 만든 임시 키입니다.
"""

import asyncio
import base64
import json
import struct

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.config import Settings
from app.transcribe import MAX_AUDIO_BYTES, GoogleTranscriber

NO_CREDENTIALS = "구글 음성 인식 인증 정보(GOOGLE_APPLICATION_CREDENTIALS)가 설정되지 않았습니다."
UNAVAILABLE = "음성 인식 서비스를 지금 사용할 수 없습니다."
REJECTED = "음성을 읽을 수 없습니다. 60초 이하의 음성 파일인지 확인해 주세요."
TIMED_OUT = "음성 인식이 제한 시간 안에 끝나지 않았습니다."


@pytest.fixture(scope="module")
def key_file(tmp_path_factory):
    """서명이 되려면 진짜 RSA 키가 있어야 합니다. 어떤 구글 계정과도 무관한 임시 키입니다."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    path = tmp_path_factory.mktemp("google") / "key.json"
    path.write_text(json.dumps({
        "type": "service_account", "project_id": "test-project", "private_key_id": "k1", "private_key": pem,
        "client_email": "stt@test-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }), encoding="utf-8")
    return path


def run(credentials_path, handler, audio=b"audio-bytes", **overrides):
    """handler는 구글 대신 답하는 함수입니다. 변환된 글자를 돌려주거나 HTTPException을 던집니다."""
    settings = Settings(google_credentials_path=credentials_path, **overrides)
    return asyncio.run(GoogleTranscriber(settings, httpx.MockTransport(handler)).transcribe(audio))


def failure(credentials_path, handler, **overrides):
    with pytest.raises(HTTPException) as error:
        run(credentials_path, handler, **overrides)
    return error.value.status_code, error.value.detail


def said(*transcripts):
    return httpx.Response(200, json={"results": [{"alternatives": [{"transcript": text}]} for text in transcripts]})


def test_sends_the_audio_to_google_and_returns_the_text(key_file):
    seen = []

    def google(request):
        seen.append(request)
        return said("오늘 저녁 뭐 먹지")

    assert run(key_file, google) == "오늘 저녁 뭐 먹지"

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == (
        "https://speech.googleapis.com/v2/projects/test-project/locations/global/recognizers/_:recognize"
    )
    scheme, token = request.headers["authorization"].split(" ")
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert scheme == "Bearer"
    assert claims["aud"] == "https://speech.googleapis.com/"
    assert claims["iss"] == "stt@test-project.iam.gserviceaccount.com"
    body = json.loads(request.content)
    assert body["config"] == {"autoDecodingConfig": {}, "languageCodes": ["ko-KR"], "model": "long"}
    assert base64.b64decode(body["content"]) == b"audio-bytes"
    assert set(body) == {"config", "content"}


def test_the_largest_accepted_audio_fits_googles_request_limit(key_file):
    """구글의 10MB 한도는 음성이 아니라 base64로 부푼 요청 전체에 걸립니다(실측 2026-11-23: 8.2MB 음성이 거절됨)."""
    sizes = []

    def google(request):
        sizes.append(len(request.content))
        return said("네")

    run(key_file, google, audio=bytes(MAX_AUDIO_BYTES), stt_model="a-long-model-name-with-room-to-spare")

    assert sizes[0] <= 10 * 1024 * 1024


def test_moves_an_m4a_index_to_the_front_before_sending(key_file):
    """구글은 색인(moov)이 파일 끝에 있는 m4a를 읽지 못합니다. 휴대폰 녹음은 그렇게 옵니다."""
    def box(kind, payload):
        return struct.pack(">I4s", 8 + len(payload), kind) + payload

    recording = (
        box(b"ftyp", b"M4A \x00\x00\x00\x00") + box(b"mdat", b"aac-frames") + box(b"moov", box(b"mvhd", b"\x00" * 12))
    )
    seen = []

    def google(request):
        seen.append(request)
        return said("네")

    run(key_file, google, audio=recording)

    sent = base64.b64decode(json.loads(seen[0].content)["content"])
    assert sent.index(b"moov") < sent.index(b"mdat")
    assert len(sent) == len(recording)


def test_regional_location_uses_the_regional_host(key_file):
    seen = []

    def google(request):
        seen.append(request)
        return said("네")

    run(key_file, google, stt_location="us", stt_model="chirp_3")

    assert str(seen[0].url) == (
        "https://us-speech.googleapis.com/v2/projects/test-project/locations/us/recognizers/_:recognize"
    )
    assert json.loads(seen[0].content)["config"]["model"] == "chirp_3"


def test_joins_every_result_in_order_and_skips_empty_ones(key_file):
    """긴 말은 구글이 여러 조각으로 나눠 줍니다. 글자가 없는 조각(언어 표시만 있는 것)도 섞여 옵니다."""
    reply = httpx.Response(200, json={"results": [
        {"alternatives": [{"transcript": " 간장 두 큰술 넣고"}]},
        {"languageCode": "ko-KR"},
        {"alternatives": [{"transcript": "약불에 볶아 줘 "}]},
    ]})

    assert run(key_file, lambda request: reply) == "간장 두 큰술 넣고 약불에 볶아 줘"


@pytest.mark.parametrize("reply", [
    httpx.Response(200, json={"metadata": {"totalBilledDuration": "1s"}}),
    said(),
    said("  "),
], ids=["no_results", "empty_results", "blank_transcript"])
def test_no_speech_is_422(key_file, reply):
    assert failure(key_file, lambda request: reply) == (422, "말소리를 알아듣지 못했습니다.")


def test_google_rejecting_the_audio_is_400_and_is_not_retried(key_file):
    """60초를 넘거나 알 수 없는 형식이면 구글이 400을 줍니다. 구글의 영어 문구는 그대로 내보내지 않습니다."""
    calls = []

    def google(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {
            "code": 400, "message": "Audio can be of a maximum of 60 seconds.", "status": "INVALID_ARGUMENT"}})

    assert failure(key_file, google) == (400, REJECTED)
    assert len(calls) == 1


def test_the_whole_call_shares_one_time_limit(key_file):
    """httpx의 제한 시간은 연결·읽기 단계마다 따로 셉니다. 전체가 제한을 넘으면 Spring이 끊기 전에 504로 답합니다."""
    async def google(request):
        await asyncio.sleep(0.3)
        return said("늦은 답")

    assert failure(key_file, google, stt_timeout_seconds=0.05) == (504, TIMED_OUT)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_google_refusal_or_outage_is_503(key_file, status):
    """키가 폐기되거나 권한이 빠져도 401이 아니라 503입니다. V3의 401은 백엔드 토큰 오류에만 씁니다."""
    reply = httpx.Response(status, json={"error": {"code": status, "status": "PERMISSION_DENIED"}})

    assert failure(key_file, lambda request: reply) == (503, UNAVAILABLE)


def test_connection_failure_and_non_json_reply_are_503(key_file):
    def unreachable(request):
        raise httpx.ConnectError("no route", request=request)

    assert failure(key_file, unreachable) == (503, UNAVAILABLE)
    assert failure(key_file, lambda request: httpx.Response(200, text="<html>proxy</html>")) == (503, UNAVAILABLE)
    # 약속과 다른 모양의 200(후보 목록이 배열이 아님)도 500으로 죽지 않는다.
    odd = {"results": [{"alternatives": {"transcript": "배열이 아닌 후보"}}]}
    assert failure(key_file, lambda request: httpx.Response(200, json=odd)) == (503, UNAVAILABLE)


def test_timeout_is_504(key_file):
    def slow(request):
        raise httpx.ReadTimeout("too slow", request=request)

    assert failure(key_file, slow) == (504, TIMED_OUT)


@pytest.mark.parametrize("kind", ["unset", "missing_file", "not_a_key", "not_an_object", "not_json"])
def test_missing_or_unreadable_credentials_are_503_without_calling_google(tmp_path, kind):
    path = None if kind == "unset" else tmp_path / "key.json"
    content = {"not_a_key": "{}", "not_an_object": "[]", "not_json": "service-account"}.get(kind)
    if content is not None:
        path.write_text(content, encoding="utf-8")
    calls = []

    def google(request):
        calls.append(request)
        return said("불리면 안 됩니다")

    assert failure(path, google) == (503, NO_CREDENTIALS)
    assert calls == []
