# 새벽 수집과 PostgreSQL 배포

## 실행 구조

```text
기존 BE 준비물 export 또는 읽기 전용 조회
 → AI 수집 목록 등록·검색어 중복 제거
 → PostgreSQL pg_cron: 매분 예약 함수 실행
 → 함수: 한국 시간 03:00 이상 05:00 미만에 작업 등록
 → worker: 등록된 작업을 분당 최대 6회로 처리
 → AI 전용 상품 스냅샷 저장
 → API: 유효한 스냅샷 조회·가격순 정렬·예산 비교
```

DB 예약 함수가 외부 HTTP 요청을 보내지는 않습니다. API 조회도 외부 API 호출이나 즉석 갱신을 실행하지 않습니다. 키·처리 허용·저장 허용을 갖춘 작업자만 수집하며, 작업 시작과 공급자 호출 직전에 시간 범위를 확인합니다. 시간은 `Asia/Seoul` 기준으로 판정하므로 서버·cron의 기본 시간대를 한국 시간으로 가정하지 않습니다.

03~05시는 120분으로, **6 × 120 = 이론상 최대 720회**입니다. 대체 검색어, 실패 요청과 재시도도 같은 호출 예산에 포함됩니다. 등록 건수와 재시도량에 따라 하루에 모두 수집하지 못할 수 있습니다. 미수집·오래된 수집을 우선 처리해 같은 항목만 반복 갱신하는 일을 줄입니다.

PostgreSQL의 호출 예약과 잠금으로 같은 DB·같은 키를 사용하는 여러 호스트의 작업자 한도를 공유합니다. 호출 제한은 잠금 획득 후 DB 서버 시각으로 계산합니다. 운영 작업자의 수집 시간창과 API의 상품 만료 판단도 DB 시각을 사용해 호스트 시계 차이에 따른 우회를 방지합니다. DB 시각을 조회하지 못하면 외부 호출과 상품 제공을 중단합니다. 같은 키를 별도 서비스가 이 저장소를 통하지 않고 사용하면 그 호출까지 집계할 수 없으므로 운영상 호출 창구를 통합해야 합니다.

## 데이터와 보관 범위

AI 소유 스키마는 `item_recommender`이며 주요 테이블은 `catalog_queries`, `query_fallbacks`, `catalog_snapshots`, `refresh_jobs`, `provider_calls`, `provider_locks`입니다. BE의 `public.activities`·`public.activity_materials`를 수정하지 않습니다.

수집 단위는 활동 행이 아니라 정규화한 검색어입니다. 공백·문자 표기를 정리해 같은 이름의 반복 호출을 줄이지만, 의미가 다른 규격이나 대체품을 자동으로 합치지 않습니다. 동의어는 운영자가 별칭으로 등록합니다.

상품 정보 저장은 `ADPICK_STORAGE_APPROVED=false`가 기본입니다. 기존 필터·표시 허용과 상품 DB 보관은 별도이며, 상품 정보·이미지 URL의 저장 범위와 기간을 확인한 뒤에만 활성화합니다. 이미지 파일을 복제하는 기능은 없습니다.

`CATALOG_RETENTION_SECONDS` 기본값은 86400초입니다. **기본값은 저장 허가나 최신성 보증이 아닙니다.** 합의된 기간으로 조정하고, 만료된 상품은 응답에서 제외·정리합니다. 매일 배치를 실행해도 처리 순서 변화와 실패로 24시간 이내 갱신을 보장할 수 없습니다. DB 백업·복제본의 보관 기간 역시 운영자가 같은 허용 범위에 맞춰야 합니다.

## 준비물 등록

BE가 export한 `{materials:[{name:...},...]}` 또는 활동 상세 응답의 `{result:{materials:[...]}}`를 사용합니다.

```sh
python -m item_recommender.cli init-db
python -m item_recommender.cli register --origin BE_MATERIAL --file examples/request-basic.json
python -m item_recommender.cli status
```

등록 명령은 검색어를 DB에 넣을 뿐 애드픽을 호출하지 않습니다. 여러 활동의 export를 반복 등록할 수 있으며 같은 정규화 검색어는 중복 등록하지 않습니다. 현재 전달받은 준비물 목록이 전체 BE 활동을 대표한다고 가정하면 안 됩니다. CLI는 최대 1 MiB의 JSON 객체를 받으며 활동별 export를 하나씩 처리합니다.

CLI 동의어 목록은 최대 10개이며 파일 형식은 다음과 같습니다. 내부 운영자 HTTP API의 한도는 5개입니다.

```json
{"primary": "계량스푼", "fallbacks": ["계량 스푼"]}
```

운영자가 작성한 파일로 등록합니다.

```sh
python -m item_recommender.cli fallbacks --file /absolute/path/fallbacks.json
```

명시적으로 지정한 동의어만 대체 후보로 사용합니다. 빈 검색 결과를 관련 없는 인기 상품으로 채우지 않습니다.

## PostgreSQL와 pg_cron

운영 모드는 `CATALOG_DATABASE_URL`과 32자 이상의 `SERVICE_TOKEN`이 필요합니다. DB URL이 없는 로컬 모드는 SQLite를 사용하지만, 운영 예약 실행은 PostgreSQL의 pg_cron이 담당합니다.

호스트 Python에서 PostgreSQL을 사용할 때는 드라이버 선택 의존성을 설치합니다. Docker 이미지에는 이 선택 의존성이 포함됩니다.

```sh
python -m pip install -e '.[postgres]'
```

서비스 소스 디렉터리에서 다음 순서로 적용합니다. DSN은 실행 환경에 주입한 값을 사용합니다.

```sh
psql "$CATALOG_DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/001_catalog.sql
psql "$CATALOG_DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/002_pg_cron.sql
```

PostgreSQL 서버에 pg_cron이 설치되어 있어야 하며, `shared_preload_libraries=pg_cron` 및 `cron.database_name` 설정에는 서버 재시작이 필요할 수 있습니다. 확장 설치와 예약 등록은 필요한 DB 관리 권한으로 수행합니다.

예약은 `hobbytree-item-catalog-enqueue`와 `hobbytree-item-catalog-purge` 두 개이며 모두 `* * * * *`로 실행됩니다. `item_recommender.enqueue_daily_refresh()`가 내부에서 KST 시간 범위를 확인합니다. 매분 호출해도 같은 날짜·검색어 작업을 중복 등록하지 않도록 DB에서 관리합니다. 만료 상품은 시각 도달 즉시 응답에서 제외하고 DB의 상품 payload는 다음 정리 작업에서 지웁니다. 예약 중단 시 실제 정리가 늦어질 수 있으므로 처리 지연과 백업 보관도 허용 조건에 맞춰야 합니다.

기존 BE와 같은 PostgreSQL에 AI 스키마를 두는 경우에만 선택적으로 다음 SQL을 적용합니다.

```sh
psql "$CATALOG_DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/003_sync_activity_materials.sql
```

이 SQL은 BE의 공개 활동·준비물을 읽어 AI 수집 목록에 등록하기 위한 선택 경로입니다. 별도의 AI DB에는 BE 테이블이 없으므로 사용할 수 없습니다. 그 경우 지원 취미·공개 활동만 담긴 export에 `register --origin BE_MATERIAL`을 사용하세요. 이 경로는 등록만 수행하며 누락 항목의 비활성화는 별도 동기화 계약이 필요합니다. BE 테이블에는 SELECT 권한만 부여하고, AI 쓰기 권한은 전용 스키마로 제한하는 운영 역할 구성이 필요합니다. 이 동기화는 한 번 실행하는 명령이며 기본 cron에 포함되지 않습니다. BE에서 준비물이 바뀌면 다시 등록·동기화해야 하고, 주기적 원본 동기화는 담당자와 별도로 연결해야 합니다.

## 작업자와 상태 확인

```sh
python -m item_recommender.cli worker
```

작업자는 실행 상태를 유지하며 예약된 일을 처리합니다. `worker --once`는 한 번의 처리 주기이며, 시간 범위나 허용 설정을 우회하지 않습니다. 종료 코드 0만으로 수집 성공을 판단하지 말고 JSON의 `status`를 확인하세요. 새벽 시간 밖에서는 외부 호출을 하지 않습니다. 강제 실호출이나 시간 위조 옵션은 제공하지 않습니다.

```sh
python -m item_recommender.cli worker --once
python -m item_recommender.cli status
python -m item_recommender.cli purge
```

로컬 SQLite에서만 `python -m item_recommender.cli schedule`로 예약 함수를 시험할 수 있습니다. 이 명령도 실제 03~05 KST 범위를 지킵니다. PostgreSQL에서는 pg_cron을 사용합니다. SQLite에는 DB cron이 없으므로 로컬 만료 데이터 정리는 `purge`를 명시적으로 실행합니다.

설정이 없어서 대기하는 상태와 실제 공급자 오류를 구분하세요. API 키 오류, 제한 응답, 시간 초과를 정상 검색 0건으로 바꾸지 않습니다. 마지막 성공 수집과 마지막 시도는 별개이며, 실패했다고 오래된 상품의 유효기간을 늘리면 안 됩니다.

## Compose 예시

`Dockerfile.postgres`는 PostgreSQL 16 bookworm에 pg_cron 패키지를 설치합니다. `compose.yaml`은 독립 AI DB·API·작업자 세 개의 서비스를 구성하며 BE·FE를 실행하거나 연결하지 않습니다. 이미지 빌드 및 실제 pg_cron 예약 동작은 별도 검증이 필요합니다. 일반 PostgreSQL SQL·저장소 테스트와 예약 확장 동작 검증은 구분합니다.

zsh 터미널에서 DB 암호를 화면에 표시하지 않고 입력하고, 내부 서비스 토큰을 생성할 수 있습니다.

```sh
read -rs 'POSTGRES_PASSWORD?로컬 DB 암호: '; print
export POSTGRES_PASSWORD
export SERVICE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ADPICK_FILTERING_APPROVED=false
export ADPICK_STORAGE_APPROVED=false
export ADPICK_API_KEY=
docker compose up -d --build
```

이 기본 설정에서는 작업자가 시작돼도 외부 수집을 하지 않습니다. API는 loopback의 8001 포트로만 노출되고 DB 포트는 호스트에 공개하지 않습니다. API 컨테이너에는 공급자 키를 전달하지 않습니다. DB 암호는 URL에 넣지 않고 `PGPASSWORD`로 주입합니다.

Compose에서는 `cron.use_background_workers=on`으로 예약 SQL을 실행합니다. 기본 libpq 접속 방식에는 별도의 로컬 DB 인증 설정이 필요할 수 있으므로, 단순히 확장을 설치한 것만으로 예약이 실행된다고 가정하면 안 됩니다. 이 두 작업의 동시 실행 한도는 2로 설정했습니다. [pg_cron 공식 설정 안내](https://github.com/citusdata/pg_cron#ensuring-pg_cron-can-start-jobs).

준비물 export를 등록할 때는 파일을 API 컨테이너에 복사한 뒤 실행합니다.

```sh
docker compose cp examples/request-basic.json api:/tmp/materials.json
docker compose exec api python -m item_recommender.cli register --origin BE_MATERIAL --file /tmp/materials.json
docker compose exec api python -m item_recommender.cli status
```

상품 저장까지 포함한 허용 범위를 확인한 뒤에만 작업자에 공급자 키와 허용 설정을 주입해 컨테이너를 재생성하세요. 키를 대화·명령 인수·이미지 빌드 인수에 넣지 않습니다. 실행 중인 프로세스는 환경 변경을 자동으로 읽지 않습니다.

최초 빈 DB 볼륨에서만 `001_catalog.sql`·`002_pg_cron.sql`이 자동 적용됩니다. 기존 볼륨의 SQL 변경은 명시적으로 적용해야 합니다. 예를 들어 서비스 디렉터리에서:

```sh
docker compose exec -T db psql -U hobbytree_items -d hobbytree_items -v ON_ERROR_STOP=1 < sql/001_catalog.sql
docker compose exec -T db psql -U hobbytree_items -d hobbytree_items -v ON_ERROR_STOP=1 < sql/002_pg_cron.sql
```

Compose 예시는 로컬 검증을 위한 단일 DB 계정 구성입니다. 실제 운영에서는 DB 관리·API 읽기·작업자 쓰기 역할과 비밀 설정을 분리해야 합니다. 데이터 볼륨 삭제는 이 안내의 갱신 절차에 포함하지 않습니다.

## 후속 확장

페이지 이동과 화면 필터는 저장된 후보 안에서 동작합니다. 화면 새로고침은 같은 DB를 다시 조회할 뿐 수집하지 않습니다. 앞으로 사용자 갱신 요청을 추가하더라도 큐에 등록하고, 일일 처리량·중복 요청·허용 시간 규칙을 별도로 정해야 합니다.

관측할 항목은 고유 검색어 수, 대기·실행·실패 작업 수, 당일 호출 수, 수집 지연, 가격 없는 상품 비율, 후보 제외 사유입니다. 상품 수·오류 코드 같은 진단과 API 키·사용자 입력·원본 요청 URL을 구분해 기록하세요.


## 검색어 출처와 수동 유지 정책

`origin`은 검색어를 자동 비활성화할 수 있는지 구분합니다. 전체 출처 이력은 아닙니다.

| 등록 경로 | 지정 출처 | 기존 검색어 처리 |
| --- | --- | --- |
| SQL 003 / 공개 준비물 export (`--origin BE_MATERIAL`) | BE_MATERIAL | 기존 MANUAL 유지 |
| 운영자 등록 API / CLI 기본값 또는 `--origin MANUAL` | MANUAL | 기존 BE_MATERIAL도 MANUAL로 전환 |
| 운영자 fallback / curated 등록 | MANUAL | 관련 검색어를 수동 유지 대상으로 전환 |

Python 자동 등록 호출은 `register_queries(names, now, origin="BE_MATERIAL")`을 사용합니다.
기본값 MANUAL은 기존 운영자 호출과 호환하기 위한 값이며 자동 동기화에 사용하지 않습니다.
HTTP 등록 API는 운영자용으로 유지하고 요청 본문에 origin 필드를 추가하지 않습니다.

BE_MATERIAL 등록은 입력의 공개 여부를 검증하지 않습니다. 호출자는 지원 취미의 공개 활동에서
얻은 준비물만 전달해야 합니다. 활동별 또는 일부 export에서 빠진 검색어를 비활성화하지 않습니다.
SQL 003은 전체 BE 테이블의 공개 조건을 조회하여 자동 등록 검색어만 비활성화합니다.
MANUAL 검색어는 운영자가 명시적으로 유지한 것이므로 원본 활동이 내려가도 유지합니다.

이 변경은 이미 MANUAL로 저장된 행을 일괄 BE_MATERIAL로 바꾸지 않습니다.
기존 데이터의 출처 판별·정리는 담당자 확인 후 별도로 수행합니다.
SQL lower와 Python casefold 정규화 일치 및 export 삭제 동기화도 별도 작업입니다.


## 준비물 동기화 실패와 재실행

`sql/003_sync_activity_materials.sql`은 등록·재활성화와 비활성화를 하나의
`BEGIN`/`COMMIT` 트랜잭션으로 처리합니다. 실행 중 SQL 오류가 발생하면 앞선 등록 변경도
커밋되지 않습니다. `psql -v ON_ERROR_STOP=1 -f ...`로 실행하고 실패를 성공으로 처리하지
않습니다. Python 등에서 연결을 재사용한다면 실패한 트랜잭션을 롤백하거나 연결을 종료한
뒤 원인을 해결하고 전체 스크립트를 재실행합니다. 실패한 비활성화 문장만 따로 재실행하지 않습니다.

`tests/test_material_sync.py`의 실패 테스트는 가짜 BE 테이블과 카탈로그를 고유한 테스트
스키마에 만들고 기준 데이터를 먼저 커밋합니다. 동기화 SQL의 트랜잭션 경계를 유지해 문장별로
실행하며, 비활성화 시점에 테스트 전용 트리거로 오류를 발생시킵니다. 별도 연결에서 신규 행이
남지 않고 기존 행의 활성 상태·출처·생성 및 수정 시각이 그대로인지 검증합니다.
오류 제거 후 정상 재실행·반복 실행·수동 검색어 보호도 검증합니다.

트랜잭션 경계를 제거한 테스트용 실행에서는 앞선 등록 변경이 남는 것을 확인합니다.
이 대조 테스트는 테스트 환경의 자동 롤백이 실제 결함을 숨기지 않는지 검사하며,
원본 SQL을 수정하거나 운영 DB에 오류를 주입하지 않습니다.

로컬에서는 `.venv/bin/python scripts/test_postgres_local.py`로 임시 PostgreSQL에서
검증합니다. 테스트 종료 시 해당 스키마와 오류 트리거를 정리합니다.
SQL 오류가 없는 불완전한 export나 잘못된 원본 데이터는 트랜잭션만으로 탐지할 수 없으며,
전체·부분 목록 및 삭제 동기화 계약은 별도 합의가 필요합니다.

## BE 준비물 전체 export v1 파일 검증

로컬 macOS·Linux에서 아래 명령으로 합성 파일을 검증합니다.

```sh
python -m item_recommender.cli validate-material-export \
  --file tests/fixtures/item-material-export-v1.json \
  --source-id synthetic-hobbytree \
  --source-epoch 11111111-1111-4111-8111-111111111111
```

허용된 원본과 epoch는 전달받은 파일과 독립적으로 지정합니다. 예시 값은 테스트 전용입니다.
정규 UTF-8 JSON과 마지막 LF를 포함한 최종 일반 파일을 사용합니다. 임시 파일과 심볼릭 링크는 거절합니다.
파일 크기(1,048,576바이트), 행 수(10,000), 계약 버전·범위·완결성, 메타데이터, ID·버전, 이름과 건수,
준비물 ID의 숫자 순서 및 원문 payload의 SHA-256을 검증합니다. 단일 합성 예시의 기대 건수는 활동 4개, 준비물 4개입니다.

종료 코드 0은 파일 검사를 마쳤다는 뜻입니다. VALIDATED는 정상 파일, HOLD_EMPTY는 유효하지만 적용을 보류할 빈 전체 목록입니다.
검증 실패는 종료 코드 2와 REJECT_ 사유를 반환합니다. 카탈로그 반영·재전송 판정·검색어 정규화는 후속 동기화 단계에 속합니다.
검증 명령은 DB·서비스 설정이나 외부 API 키를 요구하지 않습니다.

[BE export v1 계약](https://github.com/Kau-BugFree/hobby-tree-Backend/blob/1ee535fc2cc9ced38ce5bd1a7bc64905bf92bd2c/docs/item-material-export.md) ·
[합성 예시 원본](https://github.com/Kau-BugFree/hobby-tree-Backend/blob/1ee535fc2cc9ced38ce5bd1a7bc64905bf92bd2c/docs/examples/item-material-export-v1.json)

## 전체 export의 원본 참조와 검색어 비교

`preview-material-references`는 검증에 통과한 동일한 파일 내용으로 준비물 참조를 구성하고,
초기화된 AI 카탈로그를 읽어 변경 후보를 출력합니다. 참조는 원본 ID, epoch, 준비물 ID로
식별하며 활동 ID, 취미 ID, 활동 버전, 원문 이름과 정규화 검색어를 보존합니다.
정규화는 추천 조회와 같은 Python NFKC·casefold·공백 정리를 사용합니다. 원문이 160자
이하여도 정규화한 키가 비거나 200자를 넘으면 전체 입력을 거절합니다.

로컬에서는 임시 디렉터리에 검증용 DB를 만들고 다음 명령을 실행합니다.

```sh
(
  set -e
  unset CATALOG_DATABASE_URL ADPICK_API_KEY SERVICE_TOKEN
  export APP_ENV=local ADPICK_FILTERING_APPROVED=false ADPICK_STORAGE_APPROVED=false
  export CATALOG_DB_PATH="$(mktemp -d)/catalog.sqlite3"
  .venv/bin/python -m item_recommender.cli init-db
  .venv/bin/python -m item_recommender.cli preview-material-references \
    --file tests/fixtures/item-material-export-v1.json \
    --source-id synthetic-hobbytree \
    --source-epoch 11111111-1111-4111-8111-111111111111
)
```

빈 DB에서 BE 단일 합성 예시는 참조 4개, 검색어 3개를 반환합니다.
`insertKeys`, `reactivateKeys`, `keepKeys`, `deactivationCandidates`는 비교 결과이며
명령을 반복해도 카탈로그는 그대로입니다. 마지막 참조가 사라진 자동 검색어만 비활성화
검토 후보가 됩니다. MANUAL 항목, 다른 원본에서도 참조하는 검색어, 이전 참조를 확인할
수 없는 기존 검색어는 자동 비활성화 대상에서 제외합니다. 빈 전체 목록은 HOLD_EMPTY로 보류합니다.
출력에는 검토용 검색어가 포함됩니다. 실제 반영·순번 관리·승인은 후속 동기화 단계에서 처리합니다.

기존 DB에서는 `init-db`가 참조 테이블과 인덱스를 추가하며 기존 검색어를 자동 분류하지
않습니다. 미리보기를 실행할 DB에는 이 스키마가 먼저 적용되어 있어야 합니다.
