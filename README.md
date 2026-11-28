# HobbyTree AI

하비트리는 사용자가 요리·악기연주·독서 중에서 취미를 고르고, 첫 활동을 해 본 뒤, 그 경험에 맞는 다음 활동을 이어 가도록 돕습니다. 이 저장소에는 온보딩 추천, 준비물 상품 조회, 활동 코치, 기록 기반 다음 활동 추천을 담당하는 네 개의 FastAPI 서비스가 있습니다. 각 서비스는 독립적으로 실행할 수 있습니다.

## 서비스별 역할과 실행 안내

| 사용 시점 | 서비스 · 실행 안내 | 입력과 결과 | 로컬 포트 · 상태 확인 |
| --- | --- | --- | --- |
| 취미를 처음 고를 때 | [온보딩 취미 추천](src/hobby-recommender/README.md) | 설문에서 예산·시간·제외 조건에 맞는 첫 활동을 찾고, 관심사와 목적에 따라 취미 순위와 추천 근거를 반환합니다. | `8000` · `GET /health` |
| 활동 준비물을 확인할 때 | [준비물 상품 추천](src/item-recommender/README.md) | 백엔드가 보낸 준비물 목록을 기준으로 저장된 상품과 가격 정보를 조회합니다. 외부 상품 수집은 별도 작업자가 담당합니다. | `8001` · `GET /health` |
| 활동 중 도움이 필요할 때 | [활동 코치](src/activity-coach/README.md) | 활동 자료를 검색해 준비물·수행 단계에 관한 질문에 답합니다. 음성 질문은 텍스트로 변환해 대화에 사용할 수 있습니다. | `8003` · `GET /health` |
| 기록을 남기고 다음 활동을 고를 때 | [다음 활동 추천](src/next-activity-recommender/README.md) | 기록에서 즐거움·시간 부담 등의 선호를 추출해 같은 취미 나무의 활동 후보를 평가합니다. 홈에는 최대 두 장을 제안하고, 활성 나무에 추천할 카드가 없으면 자율 활동을 안내합니다. | `8010` · `GET /healthz` |

## 빠른 시작 · 예제 설문으로 첫 추천 받기

온보딩 추천은 저장소에 포함된 설문·규칙·활동 데이터로 동작합니다. Python 3.11 이상을 준비하고 macOS 또는 Linux에서 다음 순서로 실행합니다.

**1. 저장소 루트에서 가상환경을 만들고 서버를 실행합니다.**

```bash
cd src/hobby-recommender
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**2. 새 터미널을 저장소 루트에서 열고 예제 설문을 보냅니다.**

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/api/v1/recommendations \
  -H 'Content-Type: application/json' \
  --data-binary @src/hobby-recommender/examples/request.json
```

상태 응답의 `status`는 `ok`입니다. 예제 설문의 추천 응답에서는 `recommendations[0].name`이 `독서`이고, 같은 항목의 `breakdown`에서 질문별 점수 근거를 볼 수 있습니다. [예제 응답 전체](src/hobby-recommender/examples/response.json)

## 백엔드 연동

| 기능 | 대표 호출 | 연동 시 확인할 점 |
| --- | --- | --- |
| 온보딩 추천 | `POST /api/v1/recommendations` | 앱에서 사용하는 질문과 선택지는 `GET /api/v1/questionnaire`로 확인합니다. [API 명세](src/hobby-recommender/docs/openapi.json) |
| 준비물 상품 조회 | `POST /internal/v1/item-recommendations` | 백엔드는 `X-Service-Token`을 보냅니다. 기본 설정에서 상품 저장이 비활성화되어 있으면 `STORAGE_NOT_APPROVED`와 빈 목록을 반환합니다. [연동 계약](src/item-recommender/docs/integration.md) · [배치 설정](src/item-recommender/docs/batch.md) |
| 활동 코치 | `POST /backend/coach/chat`, `POST /backend/coach/transcribe` | 음성 전사는 Google Speech-to-Text 설정이 필요합니다. 모델 가중치와 대용량 검색 색인은 별도 준비 항목입니다. [채팅 API](src/activity-coach/docs/HobbyTree_Coach_V3_API_Spec_ko.md) · [음성 전사 API](src/activity-coach/docs/HobbyTree_Coach_Voice_Transcribe_Spec_ko.md) |
| 다음 활동 추천 | `POST /internal/v2/activity-feedback/analyze` → `POST /internal/v3/activity-feedback/profile` → `POST /internal/v2/next-activities/rank` | 백엔드는 기록을 마칠 때 한 건을 분석하고, 홈 조회 시 해당 나무의 분석 결과를 모아 후보 정렬을 요청합니다. 세 호출 모두 서비스 토큰을 `Authorization: Bearer` 헤더로 전달합니다. [API 명세](src/next-activity-recommender/docs/openapi.json) |
