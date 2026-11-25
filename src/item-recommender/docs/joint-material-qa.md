# 준비물 전체 export와 BE 상품 조회 공동 검증

목표는 BE 테스트 DB에서 새로 만든 파일이 AI 카탈로그에 반영되고, **같은 BE의 공개 추천 API**에서 합성 상품과 구매 링크를 반환하는지 확인하는 것이다. AI와 BE 프로세스는 같은 호스트의 루프백에서 실행한다. 파일을 주고받아 서로 다른 노트북에서 각각 실행하는 경우 한쪽에서 다른 쪽의 127.0.0.1로 연결할 수 없으므로 한 대에서 공동 실행한다.

| 구간 | 담당 | 필요한 결과 |
| --- | --- | --- |
| 원본과 새 export | BE | PostgreSQL 17 테스트 DB, 순번 1, 공개 준비물 4행, 활동 4개, 원본 파일과 payload hash |
| importer와 합성 상품 | AI | 참조 4개와 검색어 3개, APPLY → APPLIED → NOOP_REPLAY, AI_LOCAL_PASS |
| 공개 추천 API | BE·AI | 테스트 사용자의 요청으로 BE_PUBLIC_PASS, 상품 7,500원, 예산 초과 1,500원, 새 파일에 대응하는 합성 HTTPS 링크 |

## 1. AI 단독 리허설

AI 모듈 `src/item-recommender`에서 실행한다.

```sh
.venv/bin/python scripts/joint_material_qa.py ai \
  --file tests/fixtures/be-item-sync-v1/01-initial.json
```

성공 표시는 `AI_LOCAL_PASS`다. 임시 SQLite에서 실제 importer를 호출하고 합성 응답을 사용하는 배치 작업자로 상품을 채운 뒤 내부 HTTP 요청으로 결과를 확인한다. 합성 전용 시각은 서울 04시이며, 공급자 키와 상품 저장 옵션은 테스트 인스턴스 안에서만 사용한다. 종료 시 테스트 DB를 정리한다. 구매 링크는 문자열로 비교한다.

## 2. BE가 테스트 DB와 export 준비

BE의 기준은 `8e467fa2a577dd4a92c82cebfb0382842efac2c2`다. 기존 Flyway 마이그레이션을 적용한 빈 테스트 DB에 `scenarios.json`의 `initialSource`를 넣는다. 기존 BE 테스트의 `test_shared_source_scenarios_match_real_export_with_content_version_triggers`와 같이 초기 데이터를 한 트랜잭션으로 넣어 활동 11의 버전을 1로 맞춘다. 공개 활동 11과 12의 서로 다른 준비물 101·102는 같은 검색어 ‘계량스푼’에 대응한다. 같은 DB를 사용할 BE 로컬 프로필과 ACTIVE 합성 사용자도 준비한다.

BE 루트에서 실행한다. `approved-local-test`는 BE가 실제 구성한 읽기 전용 libpq 서비스 이름으로 바꾼다. 아래 명령은 새 출력 디렉터리를 만든다. 터미널을 바꾸면 출력된 JOINT_DIR의 절대 경로를 다시 지정한다.

```sh
JOINT_DIR="$(mktemp -d)"
printf 'JOINT_DIR=%s\n' "$JOINT_DIR"
python3.11 -m venv "$JOINT_DIR/export-venv"
"$JOINT_DIR/export-venv/bin/python" -m pip install -r scripts/item-export-requirements.txt
"$JOINT_DIR/export-venv/bin/python" scripts/export-item-materials.py \
  --service approved-local-test \
  --source-id synthetic-hobbytree \
  --source-epoch 11111111-1111-4111-8111-111111111111 \
  --source-sequence 1 --output "$JOINT_DIR/materials-1.json"
shasum -a 256 "$JOINT_DIR/materials-1.json"
```

성공 기준은 `EXPORTED`, `materialCount=4`, 비어 있지 않은 최종 파일이다. 기존 파일을 재전송할 때는 동일 파일을 쓴다. 이 시나리오의 payload SHA-256은 `922a8d6f0fa84ab9687316860bd62be3e03dcd816c3f8c768ffde82158f089b0`이다. `exportId`와 생성 시각이 새로 발급되므로 최종 파일 전체의 SHA-256은 실행마다 달라진다.

## 3. AI와 BE를 같은 호스트에서 연결

공동 테스트용 서비스 토큰을 한 번 발급한다. 이 디렉터리는 같은 사용자 계정의 로컬 터미널에서만 공유한다.

```sh
(umask 077 && openssl rand -hex 32 > "$JOINT_DIR/service-token")
```

**터미널 A · AI 모듈 루트**

```sh
export JOINT_SERVICE_TOKEN="$(cat "$JOINT_DIR/service-token")"
.venv/bin/python scripts/joint_material_qa.py ai \
  --file "$JOINT_DIR/materials-1.json" --serve-port 8011
```

`AI_LOCAL_PASS`에서 `fileSha256`과 BE가 계산한 파일 해시가 일치하고 Uvicorn이 127.0.0.1:8011에서 대기하면 이 터미널을 유지한다. 검증용 구매 링크 끝에는 파일 전체의 SHA-256이 들어간다. 서로 다른 export로 준비한 서버를 조회하면 최종 검사가 실패한다.

**터미널 B · BE 루트** — BE팀이 기존 로컬 테스트 프로필, 합성 DB 연결, 인증 설정을 먼저 구성한다. 토큰 파일과 JOINT_DIR 절대 경로는 터미널 A와 같다. 아래 세 속성은 실제 Spring 설정명이다.

```sh
export JOINT_SERVICE_TOKEN="$(cat "$JOINT_DIR/service-token")"
cat > "$JOINT_DIR/item-qa.properties" <<'PROPERTIES'
item-recommendation.ai.enabled=true
item-recommendation.ai.base-url=http://127.0.0.1:8011
item-recommendation.ai.service-token=${JOINT_SERVICE_TOKEN}
PROPERTIES
./gradlew bootRun --args="--spring.config.additional-location=file:$JOINT_DIR/item-qa.properties"
```

BE가 활동 11·버전 1과 준비물 101을 가진 테스트 DB로 기동해야 한다. 로컬 프로필에 필요한 기존 설정은 그대로 사용한다.

## 4. BE의 외부 API로 확인

**터미널 C · AI 모듈 루트** — 동일한 JOINT_DIR를 지정한다. zsh에서 합성 ACTIVE 사용자의 액세스 토큰만 로컬에 입력한다. 서비스 토큰은 BE→AI 인증에만 사용한다.

```zsh
read -rs 'JOINT_USER_TOKEN?로컬 테스트 사용자 액세스 토큰: '
echo
export JOINT_USER_TOKEN
.venv/bin/python scripts/joint_material_qa.py check-be \
  --file "$JOINT_DIR/materials-1.json" --port 8080
unset JOINT_USER_TOKEN
```

검사기는 다음 요청을 보낸다. 포트는 BE의 실제 로컬 포트에 맞춘다.

```http
POST /api/v1/activities/11/item-recommendations
Authorization: Bearer <테스트 사용자 토큰>
Content-Type: application/json

{"activityContentVersion":1,"materialSelections":[{"materialId":101,"budgetKrw":6000}],"maxProductsPerMaterial":1}
```

HTTP 200, 활동·버전 11/1, 준비물 101, AVAILABLE, 합성 계량스푼 1개, 7,500원, OVER_BUDGET 및 차액 1,500원, 새 파일 해시가 붙은 HTTPS 링크까지 일치하면 `BE_PUBLIC_PASS`가 출력된다. 로그에는 BE·AI SHA, 파일/payload 해시, 실행자, AI_LOCAL_PASS 및 BE_PUBLIC_PASS만 기록한다. 끝나면 두 로컬 서비스를 Ctrl+C로 종료하고 임시 토큰을 폐기한다.

| 증상 | 확인할 내용 |
| --- | --- |
| EXPORTED가 안 나오거나 파일이 이미 존재 | libpq 서비스·SELECT 권한·원본 DB·출력 디렉터리를 확인한다. 기존 파일 덮어쓰기는 지원하지 않는다. |
| expected sequence 1 또는 새 export 요구 | initialSource의 공개 상태, 활동 버전, 원문 공백을 확인하고 BE가 생성한 최종 파일을 사용한다. |
| 401·403 | 테스트 사용자 JWT와 ACTIVE 상태, 또는 BE와 AI의 서비스 토큰 일치 여부를 확인한다. |
| 404·409 | 요청 활동의 공개 상태와 현재 활동 버전, BE가 연결한 DB를 확인한다. |
| 502·503·연결 오류 | 실제 BE 로컬 포트, 어댑터 enabled, AI 주소, 서비스 토큰, 서버 실행 상태를 확인한다. |
| 상품·가격·링크 불일치 | 두 프로세스가 같은 신규 export로 준비됐는지 전체 파일 해시와 요청 준비물 ID를 비교한다. |

[BE 전체 export 계약](https://github.com/Kau-BugFree/hobby-tree-Backend/blob/8e467fa2a577dd4a92c82cebfb0382842efac2c2/docs/item-material-export.md) · [BE 공동 검증 자료](https://github.com/Kau-BugFree/hobby-tree-Backend/blob/8e467fa2a577dd4a92c82cebfb0382842efac2c2/docs/item-recommendation-joint-validation.md)
