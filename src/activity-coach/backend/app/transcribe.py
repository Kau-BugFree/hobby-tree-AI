"""음성을 글자로 바꾸는 Google Speech-to-Text v2 클라이언트.

서비스 계정이 직접 서명한 JWT를 Bearer 토큰으로 씁니다(토큰 교환 요청 없음). v2는 API 키를 받지 않습니다.
음성과 키는 저장하지 않고 로그에도 남기지 않습니다. 실패는 호출자가 상태 코드로 구분할 수 있게
HTTPException으로 알립니다. 테스트는 httpx.MockTransport를 주입해 구글을 부르지 않습니다.
"""

import asyncio
import base64
import json
import logging

import httpx
from fastapi import HTTPException
from google.auth import jwt

from .config import Settings
from .mp4_faststart import faststart

# 구글 동기 인식의 한도는 60초 또는 요청 10MB다. 10MB는 음성이 아니라 base64로 부푼 요청 전체에 걸린다
# (실측 2026-11-23: 8.2MB 음성이 "Request payload size exceeds the limit: 10485760 bytes"). 7MB 음성은 약 9.8MB 요청이 된다.
# 길이는 구글이 판정한다.
MAX_AUDIO_BYTES = 7 * 1024 * 1024
LANGUAGE = "ko-KR"
NO_CREDENTIALS = "구글 음성 인식 인증 정보(GOOGLE_APPLICATION_CREDENTIALS)가 설정되지 않았습니다."
UNAVAILABLE = "음성 인식 서비스를 지금 사용할 수 없습니다."
REJECTED = "음성을 읽을 수 없습니다. 60초 이하의 음성 파일인지 확인해 주세요."
NO_SPEECH = "말소리를 알아듣지 못했습니다."
TIMED_OUT = "음성 인식이 제한 시간 안에 끝나지 않았습니다."

logger = logging.getLogger(__name__)


def transcript_of(payload: dict) -> str:
    """조각마다 첫 번째 후보의 글자를 순서대로 잇습니다. 긴 말은 여러 조각으로 오고, 글자가 없는 조각도 섞여 옵니다."""
    parts = []
    for result in payload.get("results", []):
        alternatives = result.get("alternatives") or [{}]
        parts.append(alternatives[0].get("transcript", "").strip())
    return " ".join(part for part in parts if part)


def google_error(response: httpx.Response) -> tuple[str, str]:
    """구글 오류의 분류(PERMISSION_DENIED 등)와 설명입니다. 음성이나 키는 들어 있지 않습니다."""
    try:
        error = response.json()["error"]
        return str(error.get("status", "")), str(error.get("message", ""))
    except (ValueError, KeyError, TypeError, AttributeError):
        return "unknown", ""


class GoogleTranscriber:
    """음성 바이트를 구글에 보내고 글자를 돌려줍니다."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        location = settings.stt_location
        self.host = "speech.googleapis.com" if location == "global" else f"{location}-speech.googleapis.com"
        self._account: tuple[jwt.Credentials, str] | None = None

    def _load(self) -> tuple[jwt.Credentials, str]:
        """키 파일은 처음 쓸 때 읽습니다. 서명 도구와 호출 주소를 돌려줍니다."""
        if self._account is None:
            path = self.settings.google_credentials_path
            if path is None:
                raise HTTPException(503, NO_CREDENTIALS)
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                credentials = jwt.Credentials.from_service_account_info(info, audience=f"https://{self.host}/")
                url = (
                    f"https://{self.host}/v2/projects/{info['project_id']}"
                    f"/locations/{self.settings.stt_location}/recognizers/_:recognize"
                )
            except (OSError, ValueError, KeyError, AttributeError, TypeError) as error:  # 뒤의 둘: JSON이지만 객체가 아닌 파일
                # 키 내용이 로그에 실리지 않게 예외 종류만 남긴다.
                logger.warning("구글 서비스 계정 키를 읽지 못했습니다: %s", type(error).__name__)
                raise HTTPException(503, NO_CREDENTIALS) from None
            self._account = credentials, url
        return self._account

    async def transcribe(self, audio: bytes) -> str:
        """글자를 돌려줍니다. 말소리가 없으면 422, 구글이 음성을 거절하면 400, 구글을 쓸 수 없으면 503·504입니다."""
        credentials, url = self._load()
        headers: dict[str, str] = {}
        # 만료된 토큰은 여기서 다시 서명한다. 네트워크를 쓰지 않는다.
        credentials.before_request(None, "POST", url, headers)
        body = {
            "config": {"autoDecodingConfig": {}, "languageCodes": [LANGUAGE], "model": self.settings.stt_model},
            # 구글은 색인이 파일 끝에 있는 m4a를 읽지 못한다. 휴대폰 녹음이 그렇게 온다.
            "content": base64.b64encode(faststart(audio)).decode("ascii"),
        }
        timeout = self.settings.stt_timeout_seconds
        try:
            # httpx의 제한 시간은 연결·읽기 단계마다 따로 센다. 전체는 asyncio.timeout으로 묶는다. 넘기면 Spring이 먼저 끊는다.
            # ponytail: 요청마다 새 연결을 연다(TLS 약 0.1~0.2초). 호출이 잦아지면 앱 수명 동안 쓰는 클라이언트로 바꾼다.
            async with asyncio.timeout(timeout), httpx.AsyncClient(transport=self.transport, timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except (httpx.TimeoutException, TimeoutError):
            raise HTTPException(504, TIMED_OUT) from None
        except httpx.HTTPError as error:
            logger.warning("구글 음성 인식에 연결하지 못했습니다: %s", type(error).__name__)
            raise HTTPException(503, UNAVAILABLE) from None
        if response.status_code == 400:
            # 60초 초과·알 수 없는 형식이 모두 INVALID_ARGUMENT라 구글의 설명이 있어야 구분된다.
            logger.warning("구글이 음성을 거절했습니다: %s", google_error(response)[1][:200])
            raise HTTPException(400, REJECTED)
        if response.status_code != 200:
            # 401·403은 서비스 계정 문제다. 호출자의 토큰 오류(401)와 섞이지 않게 503으로 알린다.
            logger.warning("구글 음성 인식 오류: %s %s", response.status_code, google_error(response)[0])
            raise HTTPException(503, UNAVAILABLE)
        try:
            text = transcript_of(response.json())
        except (ValueError, AttributeError, TypeError, IndexError, KeyError):
            logger.warning("구글 음성 인식의 응답을 읽지 못했습니다.")
            raise HTTPException(503, UNAVAILABLE) from None
        if not text:
            raise HTTPException(422, NO_SPEECH)
        return text
