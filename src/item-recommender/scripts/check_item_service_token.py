"""Verify the internal token on the loopback port published by the item Compose service."""

import os

import httpx


def main() -> int:
    token = os.getenv("SERVICE_TOKEN", "")
    if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
        print("SERVICE_TOKEN에 32자 이상의 공백 없는 ASCII 토큰을 설정하세요.")
        return 1
    body = {
        "activityId": 1,
        "activityContentVersion": 1,
        "reviewStatus": "PUBLISHED",
        "materials": [],
    }
    cases = (
        ("헤더 누락", {}, 401),
        ("잘못된 값", {"X-Service-Token": "invalid-probe"}, 401),
        ("일치하는 값", {"X-Service-Token": token}, 200),
    )
    try:
        with httpx.Client(
            base_url="http://127.0.0.1:8001", trust_env=False, follow_redirects=False, timeout=5
        ) as client:
            for label, headers, expected in cases:
                response = client.post(
                    "/internal/v1/item-recommendations", headers=headers, json=body
                )
                print(f"{label} HTTP {response.status_code}")
                if response.status_code != expected:
                    return 1
            result = response.json()
            if result["isSuccess"] is not True or result["result"]["status"] != "NO_MATERIALS":
                print("준비물 추천 응답 형식을 확인하세요.")
                return 1
    except httpx.HTTPError:
        print("AI 호스트에서 127.0.0.1:8001 연결 상태를 확인하세요.")
        return 1
    except (ValueError, KeyError, TypeError):
        print("준비물 추천 응답 본문을 확인하세요.")
        return 1
    print("ITEM_TOKEN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
