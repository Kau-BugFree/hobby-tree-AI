# 하비트리 준비물 상품 추천

BE의 기존 준비물명을 수집 목록에 등록하고, 매일 **03:00~05:00 KST**에 애드픽 상품을 조회해 AI 전용 DB에 저장합니다. 사용자 요청은 저장된 상품만 읽습니다. 화면 조회나 새로고침으로 외부 상품 API를 호출하지 않습니다.

- DB 예약 작업은 수집할 일을 등록하고, 별도 작업자가 분당 최대 6회로 처리합니다.
- 상품은 가격이 있는 항목부터 낮은 가격순으로 정렬하며, 가격이 없는 항목은 마지막에 표시합니다.
- Q14를 해석하거나 상품 제외에 사용하지 않습니다. 기존 `contextText`·`preferencesVersion`은 호환 입력이며 상품 선택에 영향을 주지 않습니다.
- 상품 상세에서 입력한 예산은 상품별 비교 표시용입니다. 예산 초과 상품도 유지합니다.
- 준비물이 필요한 이유인 `necessityReason`은 `null`입니다. 운영자가 근거 있는 설명을 등록하는 후속 작업 전까지 이유를 만들어 내지 않습니다.

BE·FE와 기존 준비물 DB는 수정하지 않습니다. 연동 담당자는 [API 계약](docs/integration.md), 운영 담당자는 [수집·배포 절차](docs/batch.md)를 참고하세요.

## 로컬 설치와 조회

Python 3.11 이상, 서비스 디렉터리에서 실행합니다. 소스에 포함된 SQL을 사용하는 구조이므로 현재 안내는 소스 체크아웃의 editable 설치 기준입니다.

```sh
cd src/item-recommender
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --no-deps -e .
```

외부 호출과 상품 저장을 허용하지 않은 기본 설정으로 시작합니다.

```sh
export APP_ENV=local
export SERVICE_TOKEN=
export ADPICK_API_KEY=
export ADPICK_FILTERING_APPROVED=false
export ADPICK_STORAGE_APPROVED=false
export CATALOG_DATABASE_URL=
export CATALOG_DB_PATH=.runtime/catalog.sqlite3
python -m item_recommender.cli init-db
python -m uvicorn item_recommender.main:app --host 127.0.0.1 --port 8001
```

다른 터미널에서 서비스 디렉터리로 이동한 뒤 조회합니다.

```sh
curl -sS http://127.0.0.1:8001/health
curl -sS http://127.0.0.1:8001/internal/v1/item-recommendations \
  -H 'Content-Type: application/json' \
  --data-binary @examples/request-basic.json
```

저장 허용이 꺼져 있으면 `STORAGE_NOT_APPROVED`와 빈 상품 목록을 반환합니다. 이는 상품 검색 0건이라는 뜻이 아닙니다. API 프로세스에는 애드픽 키가 필요하지 않습니다. 키는 실제 수집을 수행하는 작업자에게만 제공합니다.

앱은 `.env`를 자동으로 읽지 않습니다. `.env.example`은 참고용이며 셸에서 `export`하거나 배포 환경에서 변수를 주입하세요. Docker Compose의 변수 치환용 `.env` 읽기와 앱의 설정 로딩은 별개입니다.

## 준비물 등록과 예시

```sh
python -m item_recommender.cli register --file examples/request-basic.json
python -m item_recommender.cli status
```

등록은 검색어만 저장하며 외부 API를 호출하지 않습니다. 예시의 ID·준비물은 문서용 값입니다. 실제 운영 데이터 적재를 뜻하지 않습니다.

| 예시 | 용도 |
| --- | --- |
| [request-basic.json](examples/request-basic.json) | 기본 캐시 조회 및 준비물 등록 형식 |
| [request-budget.json](examples/request-budget.json) | 상품별 예산 비교 |
| [request-filters.json](examples/request-filters.json) | 명시적 상품 필터와 페이지 이동 |
| [request-context.json](examples/request-context.json) | 과거 Q14 입력을 받아도 사용하지 않는 호환 계약 |
| [request-no-materials.json](examples/request-no-materials.json) | 준비물이 없는 활동 |

## 검증과 실행 범위

```sh
python -m pytest -q
python scripts/smoke_local.py
```

실제 임시 PostgreSQL로 저장소 테스트를 실행할 수 있습니다. 전용 테스트 서버를 자동으로 시작·종료하며 운영 DB에는 연결하지 않습니다.

```sh
python -m pip install -r requirements-postgres-test.txt
python scripts/test_postgres_local.py
```

외부 상품 응답은 테스트에서 대체합니다. 테스트 결과를 실제 재고·현재 판매가·제휴 전환 검증으로 해석하면 안 됩니다. PostgreSQL·pg_cron과 Docker 배포 파일은 포함하지만, 실제 pg_cron 예약 실행 및 컨테이너 빌드는 별도 검증이 필요합니다. 수행한 검증 범위는 [검증 기록](docs/validation.md)을 확인하세요.

## 저장 허용과 가격 한계

이전의 필터·표시 사용 허용 확인은 **상품 DB 보관과 보관 기간의 허용까지 포함하지 않습니다.** 기본값 `ADPICK_STORAGE_APPROVED=false`를 유지합니다. 상품 정보·이미지 URL의 저장, 재사용, 만료 후 삭제 범위를 별도로 확인한 뒤 설정하세요. 이미지 파일을 다운로드해 저장하는 기능은 없습니다.

`CATALOG_RETENTION_SECONDS=86400`은 구현 기본값이며 24시간 저장 권한이 확인됐다는 뜻이 아닙니다. 실제 합의된 보관 기간으로 조정해야 합니다. 수집이 지연되거나 실패하면 만료된 상품은 표시하지 않습니다.

[애드픽 공식 가이드](https://biz.adpick.co.kr/?ac=api&sub=guide)는 참고용 가격을 안내하지만, 실제 응답에서 가격이 빠질 수 있습니다. 없는 가격을 추정하거나 0원으로 바꾸지 않습니다. 이 경우 예산 비교는 `PRICE_UNKNOWN`, 화면에는 “판매처에서 가격 확인”을 표시합니다. 상품 조회 시각은 판매처의 가격 검증 시각이 아니며, 배송비·옵션·재고는 판매처에서 확인해야 합니다.
