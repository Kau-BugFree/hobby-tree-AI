# Hobbytree 규칙 기반 취미 추천 API

`onboarding.ts`의 실제 질문·선택지를 받아 **요리·악기·독서**를 추천하는 FastAPI 서비스입니다. 활동의 예산·시간·필수 제외 조건을 먼저 검사한 다음, 남은 취미의 가중 점수를 계산합니다. 질문 정의·점수 규칙·활동 카탈로그가 분리되어 취미와 정책을 확장하기 쉽습니다.

- [입력·출력·선택지 전체 가중치·구동 설명 보고서](docs/report.html) — 브라우저에서 파일을 열면 됩니다. 외부 자원 없이 작동하고 인쇄/PDF 저장을 지원합니다.
- [실제 요청 예제](examples/request.json), [실제 계산 응답](examples/response.json), [앱 통신 예제](examples/client.ts), [OpenAPI 스냅샷](docs/openapi.json)
- 원본 `onboarding.ts`와 동기화된 프로젝트 참조 파일은 수정하지 않았습니다.

## 빠르게 실행

Python 3.11 이상을 사용합니다. 아래 명령은 이 README가 있는 `hobby-recommender` 폴더에서 실행합니다.

```bash
# 프로젝트 안에 Python 가상 환경을 만들고 테스트 의존성까지 설치합니다.
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
# 추천 엔진을 Spring 백엔드와 별도 포트에서 실행합니다.
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Windows의 가상 환경 활성화 명령은 `.venv\Scripts\activate`입니다. 실행 후 [API 문서](http://localhost:8000/docs)에서 요청을 직접 보내볼 수 있습니다.

```bash
# 예제 설문으로 추천 엔진의 요청·응답을 확인합니다.
curl -X POST http://localhost:8000/api/v1/recommendations \
  -H 'Content-Type: application/json' \
  --data-binary @examples/request.json
```

예제는 **독서 100점 → 요리 95.75점 → 악기 70.5점**을 반환합니다. 독서의 보유 책 활동은 `eligible`, 요리·악기는 재료·작업·연주 소리 조건을 더 확인해야 하므로 `needs_confirmation`입니다. 점수는 만족 확률이나 검증된 정확도가 아닙니다.

```bash
# 회귀 테스트를 실행하고 실제 응답을 기준으로 문서 스냅샷을 갱신합니다.
pytest -q
python -m scripts.export_examples
```

첫 명령은 테스트를 실행하고, 두 번째는 현재 구현으로 `examples/response.json`과 `docs/openapi.json`을 다시 생성합니다. HTML 보고서의 표·계산 예제는 함께 제공된 버전의 정적 스냅샷이므로 데이터나 구현 변경 시 함께 갱신해야 합니다.

## API

| 메서드 | 경로 | 기능 |
| --- | --- | --- |
| GET | `/health` | 실행 상태와 데이터 버전 |
| GET | `/api/v1/questionnaire` | 질문·선택지·배타 선택·최대 선택 수 |
| GET | `/api/v1/catalog` | 지원 취미·활동·자원 ID |
| POST | `/api/v1/recommendations` | 답변 검증 및 추천 계산 |
| GET | `/docs` | 자동 생성 API 문서 |

### 입력

```json
{
  "schema_version": "onboarding-2026-10-13",
  "answers": {
    "q02": ["독서"],
    "q03": ["휴식·기분 전환"],
    "q04": "0원",
    "q05": "1만 원까지",
    "q06": "15분",
    "q08": ["읽을 책·전자책"],
    "q09Place": ["집"],
    "q09Exclude": ["없음"],
    "q10": "혼자"
  },
  "custom_values": {},
  "resources": {"owned_book": true},
  "monthly_committed_krw": 0,
  "top_k": 3
}
```

| 필드 | 계약 |
| --- | --- |
| `schema_version` | 기본값 `onboarding-2026-10-13`. 다른 버전은 422 |
| `answers` | 모든 키 선택 입력. 단일 선택은 문자열, 복수 선택은 문자열 배열. 미응답은 생략 또는 `null`, 복수 선택은 빈 배열도 가능 |
| `custom_values` | 직접 입력 선택에 해당하는 값. 선택 없이 값을 보내거나 선택 후 값을 빠뜨리면 422 |
| `context_text` | Q14 추가 고려사항, 1~200자 문자열. 미입력은 생략. 내용이 있으면 자동 해석하지 않고 `needs_review` 반환 |
| `resources` | 카탈로그 자원 ID에 대해 `true`: 실제 사용 가능 확인, `false`: 사용 불가 확인, 미전송: 미확인 |
| `monthly_committed_krw` | 이번 달 **모든 취미**에 이미 지출·예약한 금액. 0~1,000,000,000 정수. 미전송 가능 |
| `top_k` | 1~10 정수. 생략 시 Q11 한 개 제안이면 1, 나머지 3. 지원 취미는 현재 최대 3개 |

직접 입력 필드는 다음과 같습니다.

| 선택지 | 값 필드 | 범위 |
| --- | --- | --- |
| Q04 ‘직접 금액 입력’ | `custom_values.q04_amount_krw` | 0~1,000,000,000원 정수 |
| Q05 ‘직접 금액 입력’ | `custom_values.q05_amount_krw` | 0~1,000,000,000원 정수 |
| Q06 ‘직접 시간 입력’ | `custom_values.q06_minutes` | 1~1,440분 정수 |
| Q02 ‘다른 취미 직접 입력’ | `custom_values.q02_other` | 공백뿐이지 않은 1~200자 |
| Q09 제외 ‘직접 입력’ | `custom_values.q09_exclude_text` | 공백뿐이지 않은 1~500자 |

문자열 숫자, 불리언 금액, 잘못된 선택지·키, 중복 선택, 배타 선택 위반은 422입니다. Q03은 최대 2개입니다. Q02의 ‘3개까지 추천’은 원본에서 안내 문구이며 `max` 정의가 없어 입력을 3개로 제한하지 않습니다. 원본 선택지의 정확한 문구는 `/api/v1/questionnaire`를 기준으로 사용하세요.

`onboarding.ts`의 `initial`은 Figma 프로토타입의 초기 상태입니다. 실제로 사용자가 고른 값처럼 자동 제출하지 마세요. Q08에서 ‘주방 도구’를 골랐다고 구체적인 자원의 보유를 확정하지 않습니다.

### 출력

주요 최상위 필드:

- `status`: `recommended`, `browse`, `needs_review`, `no_match`
- `schema_version`, `rules_version`, `catalog_version`: 재현·분석을 위한 버전
- `recommendations`: 취미 ID·이름·화면 키·순위·점수·질문별 `breakdown`·이유·활동 후보
- `excluded`: 모든 활동이 제외된 취미와 이유. 단순 `top_k` 미선정은 제외 사유로 반환하지 않음
- `unsupported_interests`: 현재 지원하지 않는 관심사. 원문 반환만 하며 서버에는 저장하지 않음
- `notices`, `follow_up_questions`: 안내와 각 추천 취미의 첫 활동에 대한 추가 확인 질문
- `preferences`: 선택 방식·기록 방식·부담 요소·주간 빈도
- `coverage`: 점수 응답 충실도 및 전체 응답 그룹 수

각 활동의 `confirmation_required`에는 그 활동에 필요한 확인 사항 전체가 있습니다. 최상위 `follow_up_questions`는 추천 취미별 **첫 활동**의 확인 항목만 모은 값이므로 다른 활동으로 바꾸면 해당 활동의 목록을 읽어야 합니다.

`weighted_answer_coverage`는 점수표에 있는 선택지가 최소 하나 있는 질문들의 가중치 합(0~100)입니다. 예를 들어 Q02에서 커피만 선택하면 Q02의 가중치 40은 포함되지 않습니다. `answered_groups`는 비어 있지 않은 응답 그룹 수로 ‘아직 모르겠어요’도 포함합니다. Q09가 두 그룹이어서 `total_groups`는 14입니다. 충실도는 확률이나 정확도가 아닙니다.

| 상태 | 앱에서의 처리 |
| --- | --- |
| `recommended` | 후보 표시. 활동의 `eligible` / `needs_confirmation`을 함께 확인 |
| `browse` | Q01 시작 의사 없음 또는 Q11 직접 찾기. 순위 추천 없이 탐색 안내 |
| `needs_review` | Q09 직접 입력 제외 조건 또는 Q14 추가 고려사항을 확인해야 함. 추천 목록을 비워 반환 |
| `no_match` | 조건을 충족하는 등록 활동이 없음. 제외 이유를 설명하고 사용자에게 수정 기회 제공 |

`needs_confirmation`은 오류가 아닙니다. 카탈로그의 활동별 `checks`는 항상 확인 항목으로 반환하며, 이 API는 확인 완료를 저장하는 기능이 없습니다. 따라서 물품을 모두 `true`로 보내도 요리·악기는 재료·연주 소리 조건 등의 확인을 앱에서 별도로 진행해야 합니다. 원본 B01~B06의 전체 후속 절차를 구현한 API는 아닙니다.

### MVP 지원 범위 확정 반영

추천 대상은 **요리(`cooking`)·악기(`instrument`)·독서(`reading`)**입니다. 악기는 헤드폰 전자건반, 어쿠스틱 기타, 연습실 피아노의 첫 활동 3개를 포함합니다. 큰 소음 제외 시 기타·연습실 활동을 제외하고, 헤드폰 건반도 타건 소음 등의 확인 사항을 남깁니다. `피아노·건반`·`기타` 선택은 선호 점수에 반영하되 실제 악기나 헤드폰의 사용 가능 여부를 확정하지 않습니다.

기존 프론트엔드 입력 계약을 보존하기 위해 설문의 `러닝·걷기`·`커피` 선택지는 유지합니다. 이 선택들은 `unsupported_interests`로 안내하고 추천 후보에는 포함하지 않습니다. 운동화·운동복도 MVP 점수 근거로 사용하지 않습니다.

러닝 활동·자원 코드를 제거하고 악기 코드를 추가했으므로 규칙·카탈로그 버전을 `rules-2.0.0`, `catalog-2.0.0`으로 올렸습니다. 이전 응답의 러닝 코드나 자원 ID를 새 요청에 재사용하면 안 됩니다. **악기의 적합도·활동 비용·시간은 초기 설계값**이며, 최종 추천 기준과 실제 활동 데이터는 회의에서 검토해야 합니다. 요리·독서의 기존 적합도와 질문 가중치 합계 100은 유지했습니다.

## 추천 계산 정책

```text
점수(hobby) = Σ [질문 가중치 × 질문별 적합도]
```

| 질문 | 가중치 | 복수 선택 합성 |
| --- | ---: | --- |
| Q02 관심 취미 | 40 | 최댓값. 직접 선택한 지원 취미 1.0, 나머지 0.5 |
| Q03 목적 | 30 | 선택한 목적 적합도의 평균 |
| Q08 물품 | 10 | 최댓값. 소유 확정에는 사용하지 않음 |
| Q09 선호 장소 | 15 | 최댓값. 필수 장소 제한에는 사용하지 않음 |
| Q10 동행 선호 | 5 | 단일 선택의 적합도 |

전체 선택지의 적합도는 [rules.json](app/data/rules.json) 및 [HTML 보고서](docs/report.html)에 있습니다. 점수표에 없는 선택은 방향성 신호로 사용하지 않으며, 해당 질문에 유효한 점수 신호가 하나도 없으면 중립값 0.5입니다. 가중치를 다시 정규화하지 않아 미응답만 있는 경우 세 취미 모두 50점입니다.

점수는 내림차순, 동점은 `hobby_id` 오름차순입니다. 활동은 확인 완료 가능 상태(`eligible`) 우선 → 첫 비용 → 전체 시간 → 활동 ID 순으로 정렬합니다. Q11은 기본 후보 수와 탐색 흐름, Q12·Q13은 표시 선호에만 반영합니다. Q07은 계획 참고값으로 전달하며 월 4주 환산이나 의무 목표에 사용하지 않습니다.

### 예산·시간·제외 조건

- **Q04**: 첫 활동의 재료·물품·대여·시설·이동을 포함한 신규 지출 상한. ‘확인한 뒤 결정’과 ‘아직 모르겠어요’는 미정이며 0원이 아닙니다.
- **Q05**: 모든 취미에 걸친 이번 달 한도. `monthly_committed_krw`가 있으면 `max(0, 월 한도 - 사용·예약 금액)`과 첫 비용을 비교합니다. 없으면 월 전체 한도만 상한으로 검사하고 유료 활동에는 남은 금액 확인을 요청합니다.
- **Q06**: 준비·정리·이동·대기를 포함한 전체 시간으로 필터링합니다. 직접 활동 시간은 별도 표시합니다.
- **Q09 필수 제외**: 태그가 일치하는 활동을 제외합니다. 미응답은 ‘없음’으로 가정하지 않습니다. 자유 텍스트는 자동 해석하지 않고 `needs_review`로 반환합니다.
- **자원**: 필수 자원이 `false`면 활동을 제외하고, 미확인이면 확인을 요청합니다. 소유·대여·시설 가능 여부를 자동 추측하지 않습니다.

모든 활동이 제외된 취미만 추천 대상에서 빠집니다. 강한 관심이나 높은 목적 점수가 필수 제외를 덮어쓰지 않습니다. 예산 미정은 유료 활동에서 추가 확인을 요청하고, 시간 미정은 모든 활동에서 확인을 요청합니다.

**월간 계획 전체의 예산 충족을 보장하지 않습니다.** 각 후보를 처음 실행할 때의 필요조건만 검사합니다. 여러 취미를 함께 선택했을 때의 합산 비용·반복 횟수는 이후 계획 기능에서 계산해야 합니다. 반복 비용은 표시용으로 반환합니다.

## 앱에서 연결

기존 TypeScript 앱은 [examples/client.ts](examples/client.ts)의 `fetchRecommendations`를 연결하면 됩니다. 시간 초과·호출 취소·422·500·503 오류 처리와 결과 상태 타입, 기존 카드 변환 함수가 포함되어 있습니다. 앱의 초기 표시값과 실제 제출 답변을 구분하고, 반환된 `ui_key`로 기존 카드에 연결하세요. MVP 화면 키는 `요리`, `악기`, `독서`입니다.

- 같은 컴퓨터: `http://localhost:8000`
- 실제 휴대폰: 개발 컴퓨터의 접속 가능한 LAN IP 또는 HTTPS 배포 URL. 휴대폰의 localhost는 휴대폰 자신입니다.
- Android 에뮬레이터: 환경에 맞는 호스트 연결 주소를 별도로 설정합니다.

| HTTP 상태 | 발생 조건 | 앱에서의 처리 |
| --- | --- | --- |
| 422 | 잘못된 입력·선택지 | 응답 본문의 `detail` 배열에서 `loc`·`msg`를 해당 질문 옆에 표시 |
| 500 | 추천 계산 중 예상하지 못한 서버 예외 | 서버 오류 안내와 재시도 버튼 표시 |
| 503 | 추천 서비스가 준비되지 않았거나 사용할 수 없는 상태 | 일시적 이용 불가 안내 후 잠시 뒤 재시도 |

서버의 500·503 응답은 `{"detail": "안내 메시지"}` 형식입니다. 추천 계산 예외의 상세 내용은 서버 로그에만 남깁니다. `/health`, `/api/v1/catalog`, `/api/v1/questionnaire`도 추천 서비스가 없으면 503을 반환합니다. 잘못된 설정 파일은 기존대로 서버 시작을 실패시키며, 자동 복구나 재시작은 수행하지 않습니다.

클라이언트의 `RecommendationApiError.status`로 422·500·503을 구분하고, 500·503에서는 `message`를 안내에 사용하세요. `retryable`은 HTTP 5xx일 때 `true`이며 자동 재시도는 하지 않습니다. `detail`은 응답 본문 전체이므로 422의 필드 오류는 그 안의 `detail` 배열에 있습니다. 프록시에서 HTML·일반 텍스트·빈 오류 응답이 와도 HTTP 상태가 유지됩니다. 네트워크 실패와 호출 취소는 기존 예외로 전달됩니다.

결과 점수에 `%`를 붙이지 않고, 조건 미확인 상태를 활동 가능 확정처럼 표시하지 마세요.

클라이언트 오류 분기 테스트는 TypeScript를 직접 실행할 수 있는 Node.js 환경에서 실행합니다(로컬 검증: Node.js 26).

```bash
# Node.js의 기본 테스트 실행기로 클라이언트 계약을 확인합니다.
node --test tests/client.test.mjs
```

## Kau-BugFree 저장소 연동 점검 (2026-11-08)

아래 공개 기본 브랜치의 코드를 읽기 전용으로 확인했습니다. 이번 변경은 이 `hobby-recommender` 폴더에만 있으며 프론트엔드·Spring 백엔드·AI 원격 저장소는 수정하지 않았습니다.

| 대상 | 확인한 커밋 | 현재 상태 |
| --- | --- | --- |
| [프론트엔드](https://github.com/Kau-BugFree/hobby_tree_frontend/tree/92247153181f311556503bd5033c7e95deeecb25) `main` | `9224715` | Expo 메모리 상태와 고정 `RECOMMENDED` 사용. `summary.tsx`의 실제 추천 요청은 TODO |
| [백엔드](https://github.com/Kau-BugFree/hobby-tree-Backend/tree/a25118548abca637597a2e247109eb7106e2fce1) `develop` | `a251185` | 인증·선호 저장·추천 저장 구현. `RecommendationService`의 `RULE_BASED_V1` 임시 계산 사용, 외부 AI 호출은 미구현 |
| [AI](https://github.com/Kau-BugFree/hobby-tree-AI/tree/199bafdecb13d40b91a61b871c51cc7455915380) `main` | `199bafd` | README만 존재. 이 로컬 프로젝트가 자동으로 업로드되지는 않음 |

### 이번 프로젝트에서 고친 연동 문제

1. `examples/client.ts`에 `toRecommendationRequest`를 추가했습니다. `AppState.answers`의 `parseConfirm`·`noticeFrequency` 같은 UI 전용 키를 보내지 않고, `confirmedQuestionKeys`에 명시한 실제 입력·확인 답변만 전달합니다. 기존 앱은 프로토타입 초기값과 실제 답변을 구분해 저장하지 않으므로 호출자가 확인 목록을 관리해야 합니다. 모든 질문 키를 무조건 넣으면 초기값까지 제출되므로 피해야 합니다.
2. `customAmount.q04/q05`, `customMinutes`를 `custom_values`로 변환합니다. 직접 입력을 선택한 질문의 값만 보내며 0원도 유지합니다. Q02·Q09 직접 입력 문구는 별도 `customText` 옵션으로 받습니다. 해당 UI 입력란은 공개 프론트엔드에 아직 없어, 선택만 제출하면 서버가 422를 반환합니다.
3. 실제로 작성한 Q14를 `includeFreeText: true`로 제출하면 `context_text`로 보존합니다. 서버는 `needs_review`와 확인 질문을 반환합니다. 기존 화면의 `MOCK_PARSED`나 `parseConfirm: '맞아요'`만으로 조건이 구조화되었다고 인정하지 않습니다. Q14는 Q09 제외 조건으로 임의 치환하지 않습니다.
4. HTTP 성공 응답도 구조를 검증합니다. Spring의 `{isSuccess, code, message, result}` 응답, 잘못된 JSON/HTML, 활동 목록 없는 추천은 `RecommendationProtocolError`로 구분합니다. 기존 422·500·503 및 네트워크·취소 분기는 유지합니다.
5. 카드 변환에 `engineHobbyId`와 `scoreLabel`을 추가하고 확인 조건을 보존했습니다. `match`는 기존 카드 인터페이스 호환용 점수입니다. 공개 화면이 현재 붙이는 `%`는 나중에 `scoreLabel`로 교체해야 합니다. `activityId`는 엔진 활동 코드이며 프론트엔드의 `findCard`나 Spring DB의 활동 ID에 바로 사용할 수 없습니다.
6. 앱 코드·테스트·실행 스크립트의 함수/메소드에 한국어 설명 주석을 추가하고, 설정·주요 처리 블록도 주석으로 설명했습니다.

### Spring과 이 추천 엔진은 서로 다른 API입니다

두 서버 모두 `POST /api/v1/recommendations`를 사용하지만 주소·요청·응답 계약은 다릅니다. `fetchRecommendations`의 `baseUrl`에는 **추천 엔진 주소**를 넣으세요. 같은 도메인으로 배포한다면 프록시에서 별도 접두 경로를 엔진으로 보내도록 구성해야 합니다. 예: `/ai`를 제거해 엔진으로 프록시한다면 baseUrl은 `https://example.com/ai`입니다.

| 항목 | Spring 백엔드 API | 이 추천 엔진 API |
| --- | --- | --- |
| 요청 | `preferencesVersion`, `count`(1~5), `excludeHobbyIds`(DB 숫자 ID) | `answers`, `custom_values`, `context_text`, `resources`, `top_k`(1~10) |
| 인증·중복 처리 | Bearer 인증, `Idempotency-Key` 필수 | 인증/영속 저장/멱등 키 처리 없음. 선택적 token은 앞단 게이트웨이용 |
| 정상 응답 | HTTP 201, `{isSuccess, code, message, result: {recommendationId, items, ...}}` | HTTP 200, `{status, recommendations, ...}` |
| 취미·활동 ID | DB의 숫자 `hobbyId`, `activityId` | `reading`, `cooking`, `instrument` 및 엔진 활동 문자열 코드 |
| 선호 | `optionsVersion`, `budgetLevel`, `availableTimeMinutes`, `indoorOutdoor`, `difficultyPreference`, `preferredCategoryIds` | Q01~Q13 원문 선택지와 Q14 자유 문구 |
| 후보 없음 | `RECOMMENDATION422_1` | HTTP 200 + `status: no_match` |

MVP는 **요리·악기·독서**로 확정했으며, 공개 백엔드 분야 `COOKING / INSTRUMENT / READING`과 엔진의 `cooking / instrument / reading`을 맞췄습니다. **분야 ID 1·2·3은 취미 ID가 아닙니다.** `instrument`를 분야 ID 2라는 이유만으로 DB 취미 ID 2에 연결하면 안 됩니다. 이름이 같더라도 실제 DB 후보와 활동을 검증한 명시적 코드 매핑이 필요합니다. 카테고리와 개별 취미는 일대일이라는 보장도 없습니다.

`LOW/MEDIUM/HIGH` 예산 구간도 Q04 첫 지출액·Q05 정확한 월 한도와 같지 않습니다. 저장된 `preferencesVersion` 숫자만으로 설문을 복원할 수 없고, 현재 Spring 선호에는 Q03 목적·Q08 자원·Q09 필수 제외 조건 등이 없습니다. 이 엔진은 아직 Spring DB 후보·난이도·소유/제외 목록으로 추천을 제한하는 기능이 없으므로, 현재 출력을 그대로 DB에 저장하는 연결은 지원하지 않습니다.

다른 저장소를 변경하지 않는 범위에서 여기까지 검증했습니다. 전체 제품 연결에는 이후 프론트엔드의 확인한 답변 수집·API 호출·결과 상태 저장·고정 카드 교체, 그리고 Spring의 실제 AI 호출·선호 계약 합의·후보/ID 재검증·추천 저장 연결이 필요합니다. 특히 `browse`·`needs_review`·`no_match`를 성공 추천 화면으로 강제 이동시키지 않아야 합니다. 이번 작업이 이 화면·저장 경로를 이미 연결한 것은 아닙니다.

### 재현 가능한 연동 검증

`tests/fixtures/frontend_onboarding.json`은 위 프론트엔드 커밋의 실제 질문 14개 그룹에서 추출한 계약입니다. 서버 테스트가 키·선택 문구·단일/복수·배타 선택·최대 개수를 비교합니다. 실제 HTTP 검증은 추천 엔진을 실행한 뒤 아래 명령으로 재현할 수 있습니다.

```bash
# 실제 HTTP로 상태 변환→추천→카드, Q14 검토, 422 분기를 확인합니다.
node scripts/check_integration.mjs http://127.0.0.1:8000
# 엔진 통신 파일의 TypeScript 타입을 엄격하게 검사합니다.
npm exec --yes --package=typescript -- tsc --noEmit --strict --target ES2022 --module ESNext --lib ES2022,DOM examples/client.ts
```

## 환경 변수와 배포

`.env.example`은 설정 예제이며 로컬 실행에서 자동 로드되지 않습니다. 환경 변수로 직접 전달하거나 Docker의 `--env-file`을 사용합니다.

```bash
# 실제 웹 클라이언트 출처를 지정하고 추천 엔진을 시작합니다.
export CORS_ORIGINS='https://your-app.example.com'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

기본 허용 출처는 `http://localhost:8081,http://localhost:19006`입니다. 실제 웹 앱 출처를 쉼표로 구분해 설정하세요. CORS는 브라우저의 교차 출처 정책이며 인증·권한을 대신하지 않습니다. 자격 증명 허용은 꺼져 있습니다.

```bash
# 로컬 이미지로 실행합니다. .env.example의 값은 컨테이너 환경으로 전달됩니다.
docker build -t hobby-recommender .
docker run --rm -p 8000:8000 --env-file .env.example hobby-recommender
```

현재 서비스는 상태를 저장하지 않으므로 여러 인스턴스에서 동일한 설정으로 확장할 수 있습니다. 실행 중 규칙을 다시 읽지는 않으므로 JSON 변경은 배포 또는 재시작으로 반영합니다. 실제 공개 배포에는 HTTPS, 인증·호출 제한, 버전별 로그·관측을 앞단에 추가하세요. 이 프로젝트에는 실제 배포·사용자 DB·피드백 학습·비용 수집이 포함되지 않습니다.

## 파일 구조와 확장

```text
app/
  main.py                 FastAPI 생성, 시작 시 데이터 로드, CORS
  api.py                  HTTP 경로 및 오류 응답
  models.py               요청·응답 모델
  config.py               JSON 설정 모델과 시작 시 무결성 검사
  service.py              응답 검증, 활동 필터, 점수·결과 구성
  data/
    questionnaire.json    질문·선택지·검증 규칙
    rules.json            가중치·선택지별 적합도
    catalog.json          취미·활동·필수 자원·제외 태그
examples/                 실행 가능한 입력·출력·통신 예제
tests/                    정책·입력·API·설정 검증
scripts/export_examples.py
README.md
docs/report.html
docs/openapi.json
```

취미 추가 시:

1. `catalog.json`에 취미 ID, 화면 키, 관심 선택지와 활동을 추가합니다. 활동 시간·비용·자원·제외 태그를 검토합니다.
2. `rules.json`의 모든 점수표 항목에 새 취미 열을 추가합니다. 적합도 0~1과 가중치 합 100을 유지합니다.
3. 필요한 설문 선택지를 추가하고 설문 버전·앱 매핑을 함께 업데이트합니다. 현재 `schema_version` 요청 모델도 새 버전에 맞게 변경해야 합니다.
4. 새 취미를 표시할 앱의 그래픽·화면 키와 `examples/client.ts`의 지원 키 목록을 추가합니다.
5. 실제 사례와 경계조건을 테스트하고 규칙·카탈로그 버전을 올립니다. 예제·OpenAPI·HTML 보고서를 갱신합니다.

장기적으로는 선택지 문자열을 안정적인 `option_id`로 버전 전환하고, 지출·예약 원장과 계획 서비스를 분리하며, 버전별 노출·선택·완료·만족도 데이터를 수집해 가중치를 평가할 수 있습니다. 이 서비스는 자유 텍스트의 의미를 임의로 분류하지 않습니다.

## 검증 결과

이 제공본은 서버 테스트 84개와 클라이언트 테스트 22개 통과, TypeScript 엄격 타입 검사, 실제 HTTP 요청·응답 및 클라이언트 변환을 확인했습니다. 실제 Expo 화면 조작 및 Spring DB 저장까지의 통합 실행은 검증하지 않았습니다. Docker 컨테이너 이미지 빌드·실행도 이번 검증 범위에 포함하지 않았습니다.

## 한계와 참고

가중치는 초기 설계값으로 실제 사용자 만족도에 대한 학습·검증 결과가 아닙니다. 활동 비용과 시간은 설계·검증용 예시이므로 운영 가격·이동·재료 조건으로 교체해야 합니다. 0원 활동은 실제 자원 사용 가능·추가 지출 없음 확인이 전제입니다. 베이킹 관심은 요리로 연결되지만 현재 카탈로그에 베이킹 활동은 없습니다.

- [FastAPI: Bigger Applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [FastAPI: CORS](https://fastapi.tiangolo.com/tutorial/cors/)
- [Pydantic: Models](https://docs.pydantic.dev/latest/concepts/models/)
