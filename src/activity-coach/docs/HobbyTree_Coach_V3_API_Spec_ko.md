# HobbyTree 활동코치 V3 API 명세

FE · BE · AI 팀 공유용 | 문서 버전 1.1 | 작성일 2026-11-11

**명세 기준:** `rag 챗봇연결 v3/2026-11-09/backend/app`의 2026-11-10 23:27 시점 코드(git 저장소가 아니라 커밋이 없습니다). 현재 구현된 V3 서버 계약을 설명합니다. 배포 서버 주소와 Hobby Recommender와의 연결 방식은 아직 확정되지 않았습니다.

**1.1 변경(2026-11-11):** Spring 백엔드 전용 경로 `POST /backend/coach/chat`을 추가하고 11장에 계약을 적었습니다. 1~9장의 기존 경로는 바뀌지 않았습니다.

**확인 방법:** 2026-11-11에 서버를 임시 DB로 띄워 요청 50건의 실제 응답을 수집하고, 발화 11건의 분류를 따로 확인했습니다(Claude 키 없음, 임베딩·리랭커 끔). Claude 생성 경로, 하이브리드 검색, 레시피 검색, 500 응답은 실행하지 않고 코드만 읽었으며 본문에 "코드 기준"으로 표시합니다.

## 1. 서비스 개요

요리 활동을 검색으로 찾아 추천하고, 선택한 활동의 준비물·단계를 설명하며, 단계 진행과 완료를 기록하는 대화형 코치입니다. 추천·선택·조회는 생성 모델 없이 처리하고, 설명·문제 코칭·불명확한 발화의 해석에 Claude를 사용합니다. Claude 키가 없으면 자료 원문을 그대로 안내합니다.

Hobby Recommender(온보딩 설문 → 취미 추천)와는 별개의 서버이며 경로·식별자·요청 형식이 모두 다릅니다. 대응 관계는 9.1절에 정리했습니다.

**권장 연동 구조(연결 구현 필요):** Frontend → Backend(Spring) → 활동코치 V3(FastAPI) → Backend → Frontend

| 항목 | 현재 구현 |
| --- | --- |
| API 버전 | 3.0.0 |
| 로컬 Base URL | http://127.0.0.1:8003 (`start_backend.bat`) |
| 배포 Base URL | 미확정 |
| 경로 접두사 | 없음 (`/api/v1` 같은 접두사를 쓰지 않음) |
| 요청·응답 | JSON. POST·PATCH 헤더는 Content-Type: application/json |
| 인증 | 공개 경로는 없음. `/backend` 경로만 서비스 토큰 검사(11장). 요청 수 제한은 없음 |
| 지원 취미 | `cooking` 하나 |
| 저장 | SQLite(`backend/var/coach.sqlite`)에 세션·대화·실행·완료 기록·진단을 저장 |
| 식별자 | 세션·메시지·실행·기록은 UUID 문자열, 활동은 `cook-0-1` 형식의 문자열 코드 |
| 시간·시각 단위 | 시간은 분, 시각은 UTC ISO 8601(밀리초, `2026-11-10T16:50:44.519+00:00`) |
| 입력 검증 | 정의되지 않은 요청 필드 거부. 문자열 앞뒤 공백 제거. 문자열→숫자 자동 변환은 허용(`"20"` → 20) |
| 허용 Host | 기본 `127.0.0.1`, `localhost`만. 그 외 Host 헤더는 400 |
| CORS | 출처 `http://localhost:8081`, `http://127.0.0.1:8081`, `http://localhost:19006`. 메서드 GET·POST·PATCH. 헤더는 Content-Type만 허용 |
| 실행 방식 | 로컬 단일 사용자 시연용, 단일 worker 권장 |

CORS 허용 헤더에 Authorization이 없습니다. 브라우저가 토큰 헤더를 붙여 V3를 직접 호출하는 구성은 현재 설정으로 허용되지 않습니다.

### 1.1 엔드포인트 목록

| Method | 경로 | 기능 | 정상 응답 |
| --- | --- | --- | --- |
| GET | /health | 서버·검색·Claude 설정 상태 | 200 |
| GET | /hobbies | 등록된 취미 목록 | 200 |
| POST | /sessions | 새 대화 세션 생성 | 200 |
| GET | /sessions/{session_id} | 세션·대화·선택 활동 복원 | 200 |
| POST | /sessions/{session_id}/select | 활동 선택 | 200 |
| POST | /coach/messages | 대화(추천·선택·설명·완료) | 200 |
| POST | /sessions/{session_id}/executions | 활동 실행 시작 | 200 |
| GET | /executions/{execution_id} | 실행 진행 상태 조회 | 200 |
| PATCH | /executions/{execution_id} | 단계 체크 저장 | 200 |
| POST | /activities/{activity_id}/records | 완료 기록 저장 | 200 |
| GET | /records | 한 세션의 완료 기록 | 200 |
| GET | /activities | 활동 목록·주제 검색 | 200 |
| GET | /activities/{activity_id} | 활동 한 건 | 200 |
| POST | /search | 조건 필터와 검색 결과 직접 확인 | 200 |
| POST | /rag/ask | 검색→근거→답변을 한 번에 확인(검증용) | 200 |
| GET | /diagnostics/traces | 한 세션의 처리 경로·호출 수 | 200 |
| POST | /backend/coach/chat | Spring 백엔드 전용 대화(11장) | 200 |
| GET | /docs | Swagger UI | 200 |
| GET | /openapi.json | OpenAPI 명세 | 200 |

생성 요청도 201이 아니라 200을 반환합니다.

### 1.2 팀에서 먼저 알아둘 점

- `session_id`가 접근 열쇠입니다. 로그인이 없으므로 세션 ID를 아는 쪽이 그 대화·기록·진단을 읽습니다.
- HTTP 200이어도 본문의 `mode`와 `diagnostics`를 확인해야 합니다. 후보 없음, 범위 밖 질문, 뜻이 불명확한 말은 모두 HTTP 200에 `mode: "clarify"`입니다.
- 활동 ID는 Hobby Recommender의 `activity_id`(`cooking-pantry-dressing` 등)와 통하지 않습니다. 그 값을 보내면 404입니다.
- Claude 키가 설정된 서버에서는 `/coach/messages`와 `/rag/ask`가 유료 호출을 일으킬 수 있습니다.
- 예산(`budget_max`)과 난이도(`max_difficulty`) 조건은 받지만, 현재 활동 자료에 비용·난이도가 없어 지정하면 후보가 나오지 않습니다(2.2절).

<!-- pagebreak -->

## 2. 대화 요청 계약

**POST /coach/messages**

추천, 후보 선택, 단계 설명, 문제 보고, 완료 선언을 모두 이 경로로 보냅니다. 서버가 `message`의 뜻을 분류해 처리합니다.

| 필드 | 타입 | 생략 기본값 | 설명·제약 |
| --- | --- | --- | --- |
| message | string | 필수 | 사용자 발화. 공백 제거 후 1~2,000자 |
| session_id | string 또는 null | null | 1~80자. 생략하면 `cooking` 세션을 새로 만듦. 없는 ID는 404 |
| request_id | string 또는 null | null | 1~120자. 재전송 식별자(2.3절) |
| activity_id | string 또는 null | null | 1~120자. 보내면 그 활동을 선택한 뒤 메시지를 처리. 없는 ID는 404 |
| current_step | integer 또는 null | null | 1~500. 선택 활동이 없거나 단계 수를 넘으면 422 |
| constraints | object 또는 null | null | 세션 조건 변경(2.2절) |

`hobby_key`는 이 요청에 없습니다. 취미는 세션을 만들 때 정해집니다.

### 2.1 처리되는 발화 유형

응답의 `diagnostics.intent`로 서버가 어떻게 분류했는지 확인합니다. 아래 예시는 키 없는 서버에서 실제로 그렇게 분류된 발화입니다.

| intent | 예시 발화 | 처리 |
| --- | --- | --- |
| recommend | "요리 활동 추천해줘" | 조건에 맞는 활동 후보를 검색(수집 실행에서 3개) |
| update_constraints | "오늘 20분 안에 할 요리 활동 추천해줘" | 조건을 바꾼 뒤 추천. 발화나 `constraints`로 조건이 바뀐 턴 |
| select | "두 번째 걸로 할게" | 직전 후보에서 선택. 새 검색 없음 |
| activity_lookup | "준비물 알려줘" | 선택 활동의 원문 조회 |
| start | "시작할게" | 선택 활동의 실행 생성(`mode: "action"`) |
| explain | "현재 단계를 쉽게 설명해줘" | 근거 기반 설명(Claude 사용 가능) |
| coaching | "2단계 계량이 잘 안돼" | 문제 상태를 묻거나 관련 자료를 안내 |
| complete | "완료했어" | 완료 기록 저장 또는 기존 기록 안내(`mode: "action"`) |
| records | "기록 보여줘" | 이 대화의 완료 수 안내 |
| greeting | "안녕" | 안내 문구 |
| out_of_scope | "오늘 날씨 어때?" | 요리 범위 밖 안내(`mode: "clarify"`) |

그 밖에 capabilities, ambiguous, self_assessment, recommend_reason, metacognition, unknown 값이 있습니다(코드 기준).

분류는 대화 상태에 따라 달라집니다. 키 없는 서버에서 "음..."은 recommend로 분류되어 후보 없음 안내(`mode: "clarify"`)가 나왔습니다.

**확인 질문 대기 중의 완료·시작 발화:** 코치가 되물은 질문(응답의 `clarification`, 세션의 `dialogue.pending_question`)이 남아 있는 동안에는 "완료했어", "다 했어", "끝냈어요", "시작할게"를 그 질문의 답으로 받습니다. 완료 기록이나 실행을 만들지 않고 `intent`는 coaching이며, 그 말은 문제의 관찰로 저장됩니다. 완료 기록은 되돌릴 수 없어서 의도적으로 둔 규칙이고 기존 테스트(`test_pending_reply_never_performs_activity_action`)가 이 동작을 고정합니다. 이 상태에서 완료·시작을 처리하려면 "활동 완료했어", "활동 시작할게"처럼 "활동"을 붙여 말하거나, 질문에 먼저 답한 뒤 다시 말해야 합니다. 응답 문장에는 완료가 기록되지 않았다는 안내가 없으므로, 클라이언트는 `record`와 `execution_id`의 유무로 결과를 판단해야 합니다.

화면 버튼으로 확정된 동작은 발화 대신 3.3절과 6장의 전용 경로를 쓰는 편이 안전합니다.

### 2.2 조건: constraints

| 필드 | 타입 | 제약 | 동작 |
| --- | --- | --- | --- |
| max_minutes | number 또는 null | 0 초과 43,200 이하 | 전체 소요 시간이 이 값 이하인 활동만. 소요 시간이 없는 활동은 제외 |
| available_tools | string[] 또는 null | 최대 30개, 각 1~100자 | 이 도구만으로 가능한 활동만. null은 제한 없음 |
| excluded_tools | string[] 또는 null | 최대 30개, 각 1~100자 | 이 도구가 필요한 활동 제외 |
| max_difficulty | integer 또는 null | 0~5 | 현재 활동 162건 모두 난이도 미지정이라 지정하면 후보 없음 |
| budget_max | integer 또는 null | 0 이상 | 현재 활동 자료에 비용 필드가 없어 지정하면 후보 없음 |

- 조건은 세션에 저장되어 이후 요청에 계속 적용됩니다.
- **필드 생략은 기존 값 유지, 명시적 null은 해제**입니다. 두 요청은 다른 요청으로 취급합니다.
- 도구 이름은 "냄비", "도마" 같은 한글 문자열입니다. 코드 값이 아닙니다.
- 발화 속 조건도 반영합니다. "20분 안에"라고 말하면 `max_minutes`가 20으로 저장됩니다.
- 예산 조건을 주면 `text`에 "검증된 비용 정보가 없어 예산 조건의 충족을 확인할 수 없어요"가 나오고 `diagnostics.missing_filter_fields`에 `budget_max`가 들어갑니다. 난이도 조건도 같은 방식으로 안내합니다.

### 2.3 재전송: request_id

| 상황 | 결과 |
| --- | --- |
| 같은 `session_id`·`request_id`·본문 | 저장된 응답을 그대로 반환. `diagnostics.replayed: true`, 호출 수는 0으로 표시 |
| 같은 `request_id`를 다른 본문에 사용 | 409 |
| `session_id` 없이, `request_id`가 v4 UUID 표준 표기 | 같은 요청의 재전송은 같은 세션을 다시 사용(코드 기준) |
| `session_id` 없이, 그 외 `request_id`(`demo-1` 등) 또는 생략 | 매번 새 세션 |

본문 비교는 `request_id`를 뺀 "보낸 필드"로 합니다. `request_id`를 생략하면 재전송 보호가 없습니다.

```json
{
  "session_id": "b716b814-c6d0-4e5d-a6c8-a1e452a91231",
  "request_id": "demo-1",
  "message": "오늘 20분 안에 할 요리 활동 추천해줘"
}
```

<!-- pagebreak -->

## 3. 세션

### 3.1 POST /sessions

| 필드 | 타입 | 생략 기본값 | 설명·제약 |
| --- | --- | --- | --- |
| hobby_key | string | cooking | 1~40자. 등록되지 않은 취미는 422 |

`{}`도 유효한 요청입니다. 응답은 Session 객체입니다.

### 3.2 GET /sessions/{session_id}

앱을 다시 열었을 때 대화, 추천 후보, 현재 단계, 실행 ID를 복원합니다. 없는 ID는 404입니다.

### 3.3 POST /sessions/{session_id}/select

| 필드 | 타입 | 설명·제약 |
| --- | --- | --- |
| activity_id | string | 필수. 1~120자. 없는 활동 404, 세션의 취미와 다른 활동 422 |

카드에서 고른 활동을 세션에 반영합니다. 응답은 갱신된 Session 객체입니다.

### 3.4 Session 객체

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| session_id | string | 세션 UUID |
| hobby_key | string | 취미 코드 |
| candidate_ids | string[] | 직전 추천 후보의 활동 ID |
| selected_activity_id | string 또는 null | 선택한 활동 |
| current_step | integer | 현재 단계(1부터) |
| execution_id | string 또는 null | 진행 중인 실행 |
| constraints | Constraints | 세션에 저장된 조건. 미지정은 null |
| messages | Message[] | 최근 100개. `message_id`, `role`(user/assistant), `text`, assistant는 `response`(그 턴의 응답 전체) |
| search_query / search_scope | string | 직전 검색 주제와 범위 |
| created_at | string | 생성 시각(UTC) |
| dialogue | object | 문제 코칭의 내부 상태. 클라이언트가 해석할 필요 없음 |

대화가 진행되면 `shown_ids`, `candidate_selection_pending`, `last_recommendation`, `context_after_message_id`가 추가됩니다. 서버 내부 값인 `origin_request_id`와 `revision`은 응답에 포함하지 않습니다.

`messages`에는 각 턴의 응답 전체(Activity 포함)가 중첩되므로 대화가 길어질수록 세션 응답이 커집니다.

<!-- pagebreak -->

## 4. 코치 응답: 상태와 최상위 구조

### 4.1 응답 유형: mode

`/coach/messages`의 정상 응답은 항상 HTTP 200입니다. 클라이언트는 `mode`를 확인합니다.

| mode | 의미 | FE·BE 처리 제안 |
| --- | --- | --- |
| direct | 추천 목록, 선택 확인, 원문 조회, 인사 등 규칙 응답 | `text`와 `activities` 카드 표시 |
| clarify | 후보 없음, 범위 밖, 뜻 불명확, 문제 상태 되묻기 | 조건 수정·재질문 흐름 제공 |
| action | 실행 시작, 완료 기록 등 상태 변경 | `execution_id`, `actions`, `record` 반영 |
| reference | 자료 원문을 그대로 안내(키 없음 또는 생성 불가) | 원문 안내임을 표시 |
| generated | Claude 생성 답변이 검증을 통과(코드 기준) | 답변과 인용 표시 |
| insufficient_evidence | 처리 중단. `diagnostics.stop_reason` 참고(코드 기준) | 중단 안내, 재시도 유도 |

검증에서 떨어져 보류된 답은 `mode`가 `reference`로 남고 `diagnostics.llm_status`가 `withheld`입니다(코드 기준). 답변 채택 여부는 `llm_status`로 판단합니다.

### 4.2 최상위 응답 필드

아래 필드는 모든 정상 응답에 포함됩니다.

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| message_id | string | 이 응답의 UUID |
| session_id | string | 세션 UUID. 세션 없이 보낸 요청은 여기서 새 ID를 받음 |
| mode | string | 4.1절의 응답 유형 |
| text | string | 사용자에게 보일 문장 |
| activities | Activity[] | 추천·선택된 활동. 없으면 빈 배열 |
| sources | Source[] | 근거. 없으면 빈 배열 |
| actions | Action[] | 화면 동작 제안. 없으면 빈 배열 |
| selected_activity_id | string 또는 null | 처리 후 선택 활동 |
| current_step | integer | 처리 후 현재 단계 |
| execution_id | string 또는 null | 처리 후 실행 ID |
| diagnostics | Diagnostics | 처리 경로·호출 수 |
| issue_id | string 또는 null | 문제 코칭 중인 문제의 ID |
| issue_phase | string | 문제 코칭 단계. 평소 `idle` |

아래 필드는 해당 경로에서만 포함됩니다.

| 필드 | 포함되는 경우 | 설명 |
| --- | --- | --- |
| cited_evidence_ids / cited_activity_ids | 설명·문제 코칭 | 답변이 인용한 근거·활동 ID |
| validation_errors | 설명·문제 코칭 | 검증 실패·생성 불가 사유 |
| validation | 설명·문제 코칭 | `format`, `citations`, `quantities`, `semantic_support` 각각의 검증 결과 |
| metacognition | 설명·문제 코칭 | `supported`, `unresolved`, `source_review`, `stop_reason`, `next_action` |
| record | 완료 선언 | 완료 기록. 이 객체에는 `session_id`가 들어 있음 |

`activities`가 있고 `actions`가 비어 있으면 서버가 활동마다 `open_activity` 동작을 채웁니다.

### 4.3 Diagnostics

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| intent | string | 2.1절의 분류 결과 |
| search_performed | boolean | 이번 턴에 검색했는지 |
| search_mode | string | `none`, `bm25`, `catalog_browse`, `hybrid_bm25_dense_rrf` |
| llm_calls | integer | 이번 턴의 실제 Claude 호출 수 |
| llm_status | string | `not_needed`, `reference`, `no_evidence`, `generated`, `generated_repaired`, `generation_unavailable`, `withheld` |
| decision_calls / retrieval_calls / generation_calls / verification_calls | integer | 종류별 작업 횟수 |
| usage | object | 토큰 사용량 합계 |
| latency_ms | number | 처리 시간 |
| trace_id | string | `/diagnostics/traces`의 항목과 대응 |
| replayed | boolean | 재전송 응답일 때만 `true` |
| stop_reason / stop_detail | string | 중단했을 때만. `budget_exhausted`, `context_limit`, `ambiguous_intent`, `execution_failure`, `missing_observation`, `missing_knowledge` |

검색한 턴에는 검색 진단이 함께 들어갑니다: `scope`, `candidate_ids`, `ranking`, `total_documents`, `eligible_documents`, `missing_filter_fields`, `empty_reason`, `reranker`, `index_version` 등. `empty_reason`은 `no_eligible_documents`, `outside_hobby_scope`, `missing_search_topic`, `below_rerank_threshold` 중 하나입니다.

`llm_status`는 설명 경로에서만 갱신됩니다(코드 기준). 실제 호출 여부는 `llm_calls`로 확인합니다.

한 턴의 한도는 60초, 의도 해석 6회, 검색 3회, 생성 2회, 검증 2회입니다. 넘으면 `stop_reason: "budget_exhausted"`로 중단합니다(코드 기준).

<!-- pagebreak -->

## 5. 코치 응답: 하위 객체

### 5.1 Activity

`/coach/messages`의 `activities`, `/search`·`/activities`의 `items`, `/activities/{id}`가 같은 형태입니다.

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| activity_id | string | `cook-0-1` 형식. 6단계 활동은 `coo-6-1` 형식 |
| hobby_key / kind / language | string | `cooking` / `activity` / `ko` |
| title / description | string | 활동 이름 / 한 줄 요약 |
| categories | string[] | 분류(예: 한식, 공통) |
| catalog_level | integer | 0~6 단계. 난이도와 다른 값 |
| difficulty | integer 또는 null | 현재 전부 null |
| duration | object | `total_minutes`, `active_minutes`, `wait_minutes`, `prep_minutes`, `cook_minutes`, `is_estimate` |
| tools / materials | string[] | 도구 / 재료(한글 문자열) |
| steps | string[] | 단계. 실행의 `done` 배열 길이와 같음 |
| checks / safety_notes | string[] | 확인 항목 / 안전 주의 |
| lighter_alternative | string | 더 가벼운 대안 |
| cooking | object | `ingredients`(`text`, `name`, `quantity_text`, `unit`), `servings` |
| quality | object | `status`, `flags`, `human_reviewed`, `capabilities` |
| schema_version | string | 활동 자료 버전(`1.0`) |

활동 162건 중 161건에 `duration.total_minutes`가 있습니다. 소요 시간은 추정치(`is_estimate: true`)입니다.

### 5.2 Source

| 경우 | 필드 |
| --- | --- |
| 추천 | `activity_id`, `title`, `kind`, `excerpt` |
| 설명·문제 코칭 | 위 필드에 `evidence_id`, `review_status`, `source_refs`, `applies_when` 추가 |

### 5.3 Action

| type | 필드 | 의미 |
| --- | --- | --- |
| open_activity | `activity_id`, `label` | 활동 상세 열기 |
| view_records | `label` | 완료 기록 화면 열기 |

### 5.4 Execution

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| execution_id | string | 실행 UUID |
| activity_id / activity_title | string | 실행한 활동 |
| done | boolean[] | 단계별 완료 체크. 길이는 활동 단계 수 |
| status | string | `in_progress` 또는 `completed` |
| record_id | string 또는 null | 완료 기록 ID |
| created_at | string | 시작 시각(UTC) |

### 5.5 Record

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| record_id / execution_id | string | 기록 UUID / 대상 실행 |
| activity_id / activity_title | string | 완료한 활동 |
| completed_at | string | 완료 시각(UTC) |
| note | string | 메모 |
| created | boolean | 저장 요청의 응답에만 포함. 새로 만들었으면 true, 기존 기록이면 false |

실행·기록 응답에는 `session_id`가 없습니다. 실행 ID만 아는 쪽이 세션 열쇠를 얻지 못하게 하기 위해서입니다. 단, 코치 응답의 `record`에는 `session_id`가 들어 있습니다.

<!-- pagebreak -->

## 6. 활동 진행·기록 API

### 6.1 POST /sessions/{session_id}/executions

| 필드 | 타입 | 설명·제약 |
| --- | --- | --- |
| activity_id | string | 필수. 1~120자 |
| request_id | string | 필수. 1~120자 |

활동을 선택하고 실행을 만듭니다. 같은 `request_id`의 재전송은 같은 실행을 반환합니다. 같은 활동을 다시 수행하려면 새 `request_id`를 씁니다. 같은 `request_id`를 다른 활동에 쓰면 409입니다.

### 6.2 GET /executions/{execution_id}

요청 본문 없음. Execution을 반환합니다. 없는 ID는 404입니다.

### 6.3 PATCH /executions/{execution_id}

| 필드 | 타입 | 설명·제약 |
| --- | --- | --- |
| done | boolean[] | 필수. 1~500개. 활동의 전체 단계 수와 같아야 함 |

일부만 보내는 방식이 아니라 전체 배열을 매번 보냅니다. 길이가 다르면 422, 완료된 실행의 값을 바꾸려 하면 409입니다. 모든 값이 true가 되어도 `status`는 `in_progress`로 남고, 완료 기록을 저장해야 `completed`가 됩니다.

### 6.4 POST /activities/{activity_id}/records

| 필드 | 타입 | 생략 기본값 | 설명·제약 |
| --- | --- | --- | --- |
| execution_id | string | 필수 | 1~80자 |
| request_id | string | 필수 | 1~120자 |
| note | string | "" | 최대 2,000자 |

실행 하나에 기록은 하나입니다. 중복 판단 기준은 `execution_id`이며, `request_id`가 달라도 같은 실행이면 기존 기록을 `created: false`로 반환합니다. 모든 단계가 true가 아니면 409, 경로의 활동이 실행의 활동과 다르면 409입니다.

### 6.5 GET /records

| 쿼리 | 타입 | 설명·제약 |
| --- | --- | --- |
| session_id | string | 필수. 1~80자. 생략 422, 없는 세션 404 |

응답은 `items: Record[]`(최근 저장 순)와 `total: integer`입니다. 세션 범위 없는 전체 조회는 없습니다.

### 6.6 진행 순서

1. `POST /sessions`로 세션을 만든다.
2. `POST /coach/messages`로 추천을 받는다.
3. 말("두 번째 걸로 할게") 또는 `POST /sessions/{id}/select`로 활동을 고른다.
4. 말("시작할게") 또는 `POST /sessions/{id}/executions`로 실행을 시작한다.
5. `PATCH /executions/{id}`로 단계 체크를 저장한다.
6. `POST /activities/{activity_id}/records`로 완료를 기록한다.

<!-- pagebreak -->

## 7. 조회·검색 API

### 7.1 GET /health

요청 본문·쿼리 파라미터 없음. 키 없이 임베딩·리랭커를 끈 상태의 응답 예시:

```json
{
  "status": "ok",
  "version": "3.0.0",
  "mode": "local_single_user_demo",
  "llm_configured": false,
  "llm_model": "claude-opus-5-5",
  "retrieval": {
    "documents": {"activities": 162, "knowledge": 486},
    "embedding_enabled": false,
    "embedding_model": null,
    "dense_index_scopes": [],
    "embedding_error": null,
    "index_errors": {},
    "reranker_enabled": false,
    "reranker_model": null,
    "reranker_error": null,
    "rerank_candidates": 20,
    "index_version": "hobbytree-fts-v2.0"
  },
  "supported_hobbies": ["cooking"]
}
```

키 원문은 반환하지 않고 설정 여부(`llm_configured`)만 알려 줍니다. 전달 패키지의 실제 색인에서는 `documents`에 `recipes`가 추가됩니다(패키지 README 기준 544,888건). 503을 반환하는 분기는 없으며, 검색 모델·색인의 문제는 `embedding_error`·`reranker_error`·`index_errors`에 남습니다(코드 기준).

### 7.2 GET /hobbies

응답은 `items: [{hobby_key, label, scope, description}]`입니다. 현재 `cooking` 한 건입니다.

### 7.3 GET /activities

| 쿼리 | 타입 | 기본값 | 설명·제약 |
| --- | --- | --- | --- |
| q | string | "" | 비우면 카탈로그 순서, 넣으면 주제 검색 |
| hobby_key | string | cooking | 취미 코드 |
| limit | integer | 20 | 1~100 |

응답은 `items: Activity[]`와 `total`입니다. `total`은 전체 개수가 아니라 이번에 반환한 건수입니다. 페이지 넘김은 없습니다.

### 7.4 GET /activities/{activity_id}

Activity 한 건을 반환합니다. 없는 ID는 404입니다.

### 7.5 POST /search

| 필드 | 타입 | 생략 기본값 | 설명·제약 |
| --- | --- | --- | --- |
| query | string | "" | 최대 1,000자 |
| hobby_key | string | cooking | 등록되지 않은 취미는 422 |
| scope | string | activities | `activities`, `recipes`, `knowledge` 중 하나 |
| constraints | Constraints | {} | 2.2절과 같은 형식 |
| limit | integer | 3 | 1~20 |

응답은 `items`와 `diagnostics`입니다. 세션과 무관하며 조건을 저장하지 않습니다. `knowledge` 범위의 항목은 Activity가 아니라 코칭 자료 형태(`knowledge_id`, `activity_ids`, `content`, `review_status` 등)입니다. `diagnostics.ranking`에는 후보별 `bm25_rank`, `dense_score`, `rrf_score`, `rerank_score`, `passed`가 들어 있습니다.

### 7.6 POST /rag/ask

| 필드 | 타입 | 생략 기본값 | 설명·제약 |
| --- | --- | --- | --- |
| question | string | 필수 | 1~2,000자 |
| hobby_key | string | cooking | 등록되지 않은 취미는 422 |
| constraints | Constraints | {} | 2.2절과 같은 형식 |
| top_k | integer | 3 | 1~5. Claude에 근거로 넘길 상위 활동 수 |

세션과 대화 분기를 거치지 않는 검증용 경로이며 아무것도 저장하지 않습니다. 응답 필드는 `question`, `answer`, `status`, `cited_activity_ids`, `validation_errors`, `validation`, `metacognition`, `retrieval`(`items`, `diagnostics`), `evidence`, `llm`(`model`, `calls`, `usage`), `latency_ms`(`retrieval`, `llm`, `total`)입니다. `status`는 4.3절의 `llm_status`와 같은 값을 쓰며, 검색이 시간 안에 끝나지 않으면 `budget_exhausted`입니다(코드 기준).

### 7.7 GET /diagnostics/traces

| 쿼리 | 타입 | 기본값 | 설명·제약 |
| --- | --- | --- | --- |
| session_id | string | 필수 | 1~80자. 없는 세션 404 |
| limit | integer | 100 | 1~500 |

응답은 `items: Diagnostics[]`(최근 순)입니다. 질문 원문과 API 키는 들어 있지 않습니다.

<!-- pagebreak -->

## 8. 오류 응답

### 8.1 오류 처리

| HTTP 상태 | 상황 | 클라이언트 처리 |
| --- | --- | --- |
| 400 | 허용하지 않은 Host 헤더 | 서버의 `COACH_ALLOWED_HOSTS` 설정 확인 |
| 404 | 없는 세션·활동·실행 | 새 세션 생성 또는 ID 확인 |
| 409 | 요청 ID 재사용, 완료 조건 미충족, 동시 수정 | 메시지에 따라 수정하거나 같은 요청 재전송 |
| 422 | 타입·범위·필드 오류, 업무 규칙 위반 | 해당 입력 수정 후 재요청 |
| 500 | 처리 중 내부 오류 | 일반 오류 안내, 서버 로그 확인 |

`detail`의 형태는 두 가지입니다. 클라이언트는 두 형태를 모두 처리해야 합니다.

요청 형식 검증 실패(FastAPI 기본) - 배열:

```json
{
  "detail": [
    {
      "type": "extra_forbidden",
      "loc": ["body", "x"],
      "msg": "Extra inputs are not permitted",
      "input": 1
    }
  ]
}
```

업무 규칙 위반(404·409·일부 422) - 문자열:

```json
{
  "detail": "대화 세션을 찾을 수 없습니다. 새 세션을 만들어 주세요."
}
```

400은 JSON이 아니라 일반 텍스트 `Invalid host header`입니다. 500은 별도 처리기가 없어 프레임워크 기본 응답입니다(코드 기준). 503을 반환하는 경로는 없습니다.

### 8.2 업무 오류 메시지

| 상태 | 메시지 | 발생 경로 |
| --- | --- | --- |
| 404 | 대화 세션을 찾을 수 없습니다. 새 세션을 만들어 주세요. | 세션 ID를 받는 모든 경로 |
| 404 | 활동을 찾을 수 없습니다. 다른 활동을 선택해 주세요. | 활동 ID를 받는 모든 경로 |
| 404 | 활동 실행 기록을 찾을 수 없습니다. | 실행 조회·체크·완료 기록(코드 기준) |
| 409 | 같은 request_id를 다른 질문에 사용할 수 없습니다. | /coach/messages |
| 409 | 같은 시작 요청 ID를 다른 활동에 사용할 수 없습니다. | 실행 시작 |
| 409 | 이미 완료된 실행의 진행 상태는 변경할 수 없습니다. | 단계 체크 |
| 409 | 모든 단계를 마친 뒤 완료를 기록해 주세요. | 완료 기록 |
| 409 | 실행 중인 활동과 기록 대상이 다릅니다. | 완료 기록 |
| 409 | 다른 요청이 이 대화를 먼저 갱신했습니다. 같은 요청을 다시 보내 주세요. | 세션을 저장하는 경로(worker가 둘 이상일 때, 코드 기준) |
| 422 | 등록되지 않은 취미입니다. | 세션 생성, /search, /rag/ask |
| 422 | 현재 대화의 취미와 다른 활동입니다. | 활동 선택(코드 기준) |
| 422 | 선택 활동의 단계 범위를 확인해 주세요. | /coach/messages의 current_step |
| 422 | 진행 상태의 개수는 활동의 전체 단계 수와 같아야 합니다. | 단계 체크 |

**후보 없음, 범위 밖 질문, 재질문은 오류가 아니라 HTTP 200입니다.**

<!-- pagebreak -->

## 9. 팀 테스트용 요청·응답 예시

### 9.1 Hobby Recommender와의 대응

두 서버를 이을 때 변환이 필요한 항목입니다. 변환 규칙은 아직 정해지지 않았습니다.

| 항목 | Hobby Recommender | 활동코치 V3 |
| --- | --- | --- |
| 취미 식별 | `hobby_id` (`cooking`, `instrument`, `reading`) | `hobby_key` (`cooking`만) |
| 활동 식별 | `cooking-pantry-dressing` 등 9개 | `cook-0-1` 등 162개. 대응표 없음 |
| 가용 시간 | Q06 선택지 또는 `q06_minutes`(정수 1~1,440) | `constraints.max_minutes` |
| 도구·자원 | `resources`의 코드(`pot`, `bowl`)에 true/false | `available_tools`·`excluded_tools`의 한글 이름 |
| 예산 | Q04·Q05, 활동별 추정 비용으로 필터 | `budget_max`는 받지만 비용 자료 없음 |
| `top_k` | 반환할 취미 수(1~10) | `/rag/ask`의 근거 활동 수(1~5) |
| 결과 상태 | `status` 네 가지 | `mode` 여섯 가지와 `diagnostics` |
| 저장 | 없음 | 세션·실행·완료 기록 저장 |

### 9.2 추천 요청과 응답

세션을 만든 뒤 보내는 추천 요청입니다.

```json
{
  "session_id": "b716b814-c6d0-4e5d-a6c8-a1e452a91231",
  "request_id": "demo-1",
  "message": "오늘 20분 안에 할 요리 활동 추천해줘"
}
```

실제 응답에서 배열을 한 건씩만 남기고 `text`, Activity, 검색 진단의 일부를 줄인 것입니다.

```json
{
  "message_id": "ef936f0d-d747-4c47-88b2-3f2342b94a8f",
  "session_id": "b716b814-c6d0-4e5d-a6c8-a1e452a91231",
  "mode": "direct",
  "text": "다음 활동을 찾았어요. 번호로 선택하면 준비물과 순서를 이어서 안내할게요.\n1. 주방 탐험: 도구 이름과 위생 습관\n2. 계량해서 간장 드레싱 만들기",
  "activities": [
    {
      "activity_id": "cook-0-2",
      "hobby_key": "cooking",
      "title": "계량해서 간장 드레싱 만들기",
      "description": "계량(큰술·작은술), 맛보기",
      "catalog_level": 0,
      "difficulty": null,
      "duration": {"total_minutes": 15, "active_minutes": 15, "wait_minutes": 0, "is_estimate": true}
    }
  ],
  "sources": [
    {"activity_id": "cook-0-2", "title": "계량해서 간장 드레싱 만들기", "kind": "activity", "excerpt": "계량(큰술·작은술), 맛보기"}
  ],
  "actions": [
    {"type": "open_activity", "activity_id": "cook-0-2", "label": "계량해서 간장 드레싱 만들기"}
  ],
  "selected_activity_id": null,
  "current_step": 1,
  "execution_id": null,
  "diagnostics": {
    "intent": "update_constraints",
    "search_performed": true,
    "search_mode": "catalog_browse",
    "llm_calls": 0,
    "llm_status": "not_needed",
    "total_documents": 162,
    "eligible_documents": 25,
    "retrieval_calls": 1
  },
  "issue_id": null,
  "issue_phase": "idle"
}
```

"20분 안에"가 세션 조건으로 저장되어 `intent`가 `update_constraints`이고, 162건 중 25건이 조건을 통과했습니다.

### 9.3 cURL 실행

```bash
curl --fail-with-body -X POST http://127.0.0.1:8003/sessions -H 'Content-Type: application/json' -d '{}'
```

위 응답의 `session_id`를 넣은 요청을 request.json으로 저장한 뒤 실행합니다.

```bash
curl --fail-with-body -X POST http://127.0.0.1:8003/coach/messages -H 'Content-Type: application/json' --data-binary @request.json
```

### 9.4 검증 자료

이 문서와 같은 폴더에 수집 결과를 두었습니다.

- [수집한 요청·응답 50건](responses.json): 정상 응답과 400·404·409·422 응답 전체
- [OpenAPI JSON 스냅샷](openapi.json): 현재 코드에서 생성한 명세

V3 패키지의 `verification/openapi.json`은 2026-11-10 계약 변경 이전 것이므로 이 스냅샷을 기준으로 합니다. 서버 실행 후 `/docs`와 `/openapi.json`에서도 확인할 수 있습니다.

<!-- pagebreak -->

## 10. 연동 합의 및 완료 체크리스트

아래 표는 현재 구현 완료 항목이 아니라 FE·BE·AI가 함께 결정할 항목입니다. 호출 경로, 접근 제어, 세션 소유, 활동 ID, 응답 변환, 재전송은 V3 쪽 구현을 11장에 반영했고, Spring 쪽 호출 코드와 11.8절의 항목이 남아 있습니다.

| 논의 항목 | 현재 상태 | 합의할 내용·담당 |
| --- | --- | --- |
| 호출 경로 | V3는 독립 API, 접두사 없음 | BE를 통한 호출, 배포 주소, 경로 접두사 / BE·AI |
| 허용 Host·CORS | 이 PC의 Host만, Authorization 헤더 불가 | `COACH_ALLOWED_HOSTS`·`COACH_CORS_ORIGINS` 값 / BE·AI |
| 인증·요청 제한 | 없음 | BE→V3 접근 제어, 유료 호출 제한 / BE·AI |
| 세션 소유 | `session_id`만 알면 접근 | 사용자 계정과 세션의 연결·보관 위치 / BE·AI |
| 추천기 연결 | 연결 없음 | 추천 결과를 V3 세션·조건으로 넘기는 규칙(9.1절) / 전 파트 |
| 활동 ID | V3 문자열 코드 | 추천기 활동·DB ID와의 대응표 / BE·AI |
| 응답 변환 | V3 응답을 직접 반환 | Spring 공통 응답 형식, `mode`별 화면 / 전 파트 |
| 기록 저장 | V3가 SQLite에 저장 | BE DB와 V3 중 기준 저장소 / BE·AI |
| 재전송 | `request_id`로 중복 방지 | `request_id` 생성 주체와 형식 / FE·BE |
| 장애 처리 | 404·409·422 구분, 500은 기본 응답 | 타임아웃(턴 한도 60초)·재시도·409 재전송 정책 / BE·AI |
| 비용·난이도 | 조건은 받지만 자료 없음 | 화면에서 조건을 숨길지, 자료를 채울지 / 전 파트 |

### 10.1 연동 완료 체크리스트

- [ ] FE의 발화가 합의된 형식으로 BE를 거쳐 V3에 전달된다.
- [ ] 세션 ID를 사용자와 연결해 보관하고 앱 재실행 시 복원한다.
- [ ] 모든 쓰기 요청에 `request_id`를 넣고 재전송 시 같은 값을 쓴다.
- [ ] 조건의 생략(유지)과 null(해제)을 구분해 보낸다.
- [ ] `mode` 여섯 가지와 `llm_status`를 화면에서 구분한다.
- [ ] `detail`의 배열·문자열 두 형태와 400의 텍스트 응답을 처리한다.
- [ ] 409를 받으면 메시지에 따라 같은 요청을 다시 보내거나 입력을 고친다.
- [ ] 추천, 선택, 설명, 실행, 완료 기록, 후보 없음, 범위 밖 질문을 통합 테스트한다.
- [ ] 추천기 결과에서 V3로 넘어가는 흐름을 통합 테스트한다.

### 10.2 참고 소스 및 문서 관리

V3는 git 저장소가 아니라 고정 링크가 없습니다. 아래 경로는 `rag 챗봇연결 v3/2026-11-09/` 기준입니다. 이후 계약을 변경하면 문서 버전, 검증 자료, 영향을 받는 FE·BE 구현을 함께 갱신합니다.

- 경로·오류 처리: `backend/app/main.py`
- 요청 모델: `backend/app/models.py`
- 서버 설정: `backend/app/config.py`, `backend/.env.example`
- 대화 처리·응답 구성: `backend/app/coach.py`, `backend/app/dialogue.py`
- 답변 생성·검증 상태: `backend/app/answer_policy.py`
- 검색·진단: `backend/app/retrieval.py`
- 저장·업무 오류: `backend/app/store.py`
- 실행 방법: `README.md`, `start_backend.bat`

<!-- pagebreak -->

## 11. Spring 백엔드 전용 경로

**POST /backend/coach/chat**

`hobby-tree-Backend`의 `POST /api/v1/coach/chat`(API-34)이 서버 대 서버로 호출하는 경로입니다. 2026-11-11에 추가했습니다. 백엔드 `develop`(커밋 `603c525`)의 `CoachService`·DTO를 읽고 맞췄으며, 임시 DB로 서버를 띄워 요청 9건의 실제 응답을 확인했습니다. Spring 쪽 호출 코드는 아직 없습니다.

백엔드가 대화와 메시지를 저장하고, V3는 답변만 만듭니다. 백엔드는 V3의 세션 ID를 보관하지 않습니다.

### 11.1 요청

| 헤더 | 값 |
| --- | --- |
| Content-Type | application/json |
| X-Coach-Service-Token | V3 서버의 `COACH_BACKEND_TOKEN`과 같은 값 |

| 필드 | 타입 | 필수 | 설명·제약 |
| --- | --- | --- | --- |
| conversationId | integer | 필수 | 백엔드 `coach_conversations.conversation_id`. 1 이상 |
| message | string | 필수 | 사용자 발화. 공백 제거 후 1~2,000자 |
| requestId | string | 필수 | 백엔드가 받은 `Idempotency-Key`를 그대로. 1~120자 |
| hobbyCode | string | 선택 | 백엔드 `hobbies.hobby_code`. 생략하면 `cooking`. 등록되지 않은 값은 422 |

- 이름은 camelCase만 받습니다. `conversation_id`처럼 보내면 422입니다.
- 사용자 ID는 받지 않습니다. 정의되지 않은 필드는 422입니다.
- `hobbyCode`는 그 대화의 첫 요청에서만 반영됩니다.

### 11.2 응답

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| conversationId | integer | 요청의 값 그대로 |
| answer | string | 사용자에게 보일 답변 |
| answerVersion | string | `coach-v3.0.0`. 백엔드 `coach_messages.answer_version`에 저장할 값 |
| mode | string | 4.1절의 응답 유형. `clarify`는 재질문, `insufficient_evidence`는 처리 중단 |
| references | object[] | 답변의 근거. 없으면 빈 배열 |
| suggestedActivities | object[] | 추천하거나 선택한 카탈로그 활동. 없으면 빈 배열 |
| replayed | boolean | 재전송이라 저장된 답을 돌려줬으면 true |

| references 항목 | 타입 | 설명 |
| --- | --- | --- |
| sourceType | string | `ACTIVITY`(활동 자료), `KNOWLEDGE`(활동에 딸린 코칭 자료), `RECIPE`(참고 레시피) |
| sourceCode | string | V3 안의 근거 식별자. `ACTIVITY`는 활동 코드와 같음 |
| activityCode | string 또는 null | 근거가 속한 활동의 코드. `RECIPE`는 null |
| title | string | 근거 제목 |

| suggestedActivities 항목 | 타입 | 설명 |
| --- | --- | --- |
| activityCode | string | 활동 코드(`cook-0-2`) |
| title | string | 활동 이름 |
| reason | string 또는 null | 활동 자료의 한 줄 요약. V3는 활동별 추천 이유를 따로 만들지 않음 |

레시피는 백엔드 DB의 활동이 아니므로 `suggestedActivities`에 넣지 않습니다. 활동을 선택한 턴에도 선택된 활동이 `suggestedActivities`에 한 건 들어갑니다.

### 11.3 Spring이 해야 하는 변환

V3는 백엔드의 숫자 ID를 모릅니다. 활동은 `activityCode`로 알리고, 백엔드가 `activities.activity_code`로 조회해 숫자 ID로 바꿉니다.

| Spring 필드 | 채우는 방법 |
| --- | --- |
| `answer`, `coach_messages.content` | `answer` |
| `coach_messages.answer_version` | `answerVersion` |
| `references[].sourceType`, `title` | 그대로 |
| `references[].sourceId` | `activityCode`로 조회한 `activity_id`. `activityCode`가 null이거나 조회되지 않으면 null |
| `suggestedActivities[].activityId` | `activityCode`로 조회한 `activity_id`. 조회되지 않는 항목은 제외 |
| `suggestedActivities[].title`, `reason` | 그대로 |
| `messageId`, `createdAt`, `usedContext` | 백엔드가 생성 |

`mode`와 `replayed`는 현재 Spring DTO에 없는 값입니다. 쓰지 않는다면 역직렬화에서 무시하도록 설정합니다.

**전제:** 백엔드 DB의 `activity_code`가 V3의 `activity_id`와 같아야 합니다. 활동 자료는 `hobby-tree-AI` 저장소 `#1-feat-rag-backend` 브랜치의 `backend/data/activities.jsonl`(162건)입니다. 백엔드 저장소에는 활동 초기 데이터가 없어 실제 DB의 코드는 확인하지 못했습니다.

### 11.4 동작 규칙

- **대화 연결:** `conversationId`가 같으면 같은 대화로 이어 갑니다. 추천 후보, 선택한 활동, 현재 단계는 V3가 기억합니다.
- **소유권:** V3는 사용자를 모릅니다. 그 `conversationId`가 로그인한 사용자의 것인지는 백엔드가 호출 전에 확인해야 합니다(현재 `CoachService`의 `findOwnedByIdWithHobby`).
- **재전송:** 같은 `conversationId`·`requestId`·`message`를 다시 보내면 저장된 답을 돌려주고 모델을 다시 호출하지 않습니다. 같은 `requestId`로 다른 `message`를 보내면 409입니다.
- **새 대화의 재시도:** 재전송 보호는 `conversationId`가 같을 때만 성립합니다. 첫 호출이 실패해 백엔드 트랜잭션이 취소되면 다음 시도는 새 번호를 받으므로 V3는 새 대화로 처리합니다.
- **시간:** 한 턴은 최대 60초입니다. 백엔드의 응답 대기 시간은 그보다 길게(75초 이상) 잡습니다. `CoachService.chat`이 `@Transactional`이므로, 그 안에서 호출하면 DB 트랜잭션이 호출 시간만큼 열려 있습니다.
- **중단된 턴:** 한도를 넘겨 중단한 턴도 HTTP 200이고 `mode`가 `insufficient_evidence`입니다. 같은 `requestId`로 다시 보내면 같은 답이 반복되므로, 다시 묻는 것은 새 키로 보냅니다.
- **DB 짝:** V3의 `var/coach.sqlite`는 백엔드 DB 하나와 짝을 이룹니다. 백엔드 DB를 초기화하거나 다른 환경의 백엔드가 같은 V3를 쓰면, 같은 번호의 옛 대화 맥락이 이어 붙습니다. 그때는 V3 DB도 함께 비우거나 분리합니다.
- **실행·완료 기록:** 이 경로에서 "시작할게", "완료했어"라고 말하면 V3 안에 실행과 완료 기록이 생깁니다. 백엔드의 활동 기록과 성장에는 반영되지 않습니다.

### 11.5 오류

| HTTP 상태 | 본문 | 상황 |
| --- | --- | --- |
| 400 | 일반 텍스트 `Invalid host header` | Host 헤더가 `COACH_ALLOWED_HOSTS`에 없음 |
| 401 | `{"detail": "X-Coach-Service-Token이 올바르지 않습니다."}` | 토큰이 없거나 다름 |
| 409 | `{"detail": "같은 request_id를 다른 질문에 사용할 수 없습니다."}` | 같은 `requestId`를 다른 `message`에 사용 |
| 409 | `{"detail": "다른 요청이 이 대화를 먼저 갱신했습니다. ..."}` | 같은 대화의 동시 갱신(worker가 둘 이상일 때). 같은 요청을 다시 보냄 |
| 422 | `{"detail": "등록되지 않은 취미입니다."}` | `hobbyCode`가 `cooking`이 아님 |
| 422 | `{"detail": [{"type", "loc", "msg", "input"}]}` | 필드 누락·범위·이름 오류 |
| 503 | `{"detail": "백엔드 연동 토큰(COACH_BACKEND_TOKEN)이 설정되지 않았습니다."}` | V3 서버에 토큰을 넣지 않음 |

`detail`은 문자열이거나 배열입니다. 400과 500은 JSON이 아닙니다.

### 11.6 서버 설정

| 환경 변수 | 설명 |
| --- | --- |
| COACH_BACKEND_TOKEN | 백엔드와 공유하는 토큰. 비우면 이 경로는 503. 32자보다 짧으면 서버가 시작하지 않음 |
| COACH_ALLOWED_HOSTS | 백엔드가 V3를 부를 때 쓰는 호스트 이름을 추가(쉼표 구분, 포트 제외) |

이 경로에는 요청 수 제한이 없습니다. 사용자별 호출 제한은 백엔드에서 겁니다. 토큰은 헤더에 평문으로 실리므로 백엔드와 V3 사이는 내부망이나 TLS로 둡니다.

### 11.7 예시

```bash
curl --fail-with-body -X POST http://127.0.0.1:8003/backend/coach/chat -H 'Content-Type: application/json' -H 'X-Coach-Service-Token: <토큰>' --data-binary @request.json
```

```json
{
  "conversationId": 7,
  "message": "오늘 20분 안에 할 요리 활동 추천해줘",
  "requestId": "key-1",
  "hobbyCode": "cooking"
}
```

실제 응답에서 배열을 한 건씩만 남긴 것입니다(원래 각 3건).

```json
{
  "conversationId": 7,
  "answer": "다음 활동을 찾았어요. 번호로 선택하면 준비물과 순서를 이어서 안내할게요.\n1. 주방 탐험: 도구 이름과 위생 습관\n2. 계량해서 간장 드레싱 만들기\n3. 전자레인지 달걀찜",
  "answerVersion": "coach-v3.0.0",
  "mode": "direct",
  "references": [
    {"sourceType": "ACTIVITY", "sourceCode": "cook-0-2", "activityCode": "cook-0-2", "title": "계량해서 간장 드레싱 만들기"}
  ],
  "suggestedActivities": [
    {"activityCode": "cook-0-2", "title": "계량해서 간장 드레싱 만들기", "reason": "계량(큰술·작은술), 맛보기"}
  ],
  "replayed": false
}
```

같은 대화에서 "두 번째 걸로 할게", "준비물이 뭐야?"를 이어 보내면 선택한 활동의 준비물을 답하고 `references`에 그 활동이 들어갑니다. 수집한 9건 전체는 [backend_chat_responses.json](backend_chat_responses.json)에 있습니다.

### 11.8 남은 결정

| 항목 | 현재 상태 | 정할 사람 |
| --- | --- | --- |
| 6단계 활동 12건 | 백엔드 단계는 0~5(`stage_number`)만 허용. V3 자료는 0~6단계 | BE·AI |
| 백엔드 DB의 활동 적재 | 저장소에 초기 데이터 없음. `activity_code`를 V3 코드로 넣어야 함 | BE |
| 생성 모델 | 백엔드 주석은 Gemini 연동을 가정. V3는 Claude 사용 | 전 파트 |
| 실행·완료 기록의 기준 | V3와 백엔드에 따로 있음(11.4절) | BE·AI |
| 취미 추천(`/api/v1/recommendations`) | 이 경로의 범위 밖. 추천기와 계약이 다름 | BE·AI |

참고 소스: `backend/app/backend_bridge.py`, `backend/app/models.py`의 `BackendChatRequest`, `backend/tests/test_backend_bridge.py`.

**문서 범위:** 활동코치 V3의 현재 구현 계약과 Spring 백엔드 전용 경로. Hobby Recommender API, 운영 배포 주소, Spring 쪽 호출 코드는 이 문서에서 확정하지 않습니다.
