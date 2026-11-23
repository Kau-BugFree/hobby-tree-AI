# BE·FE 연동 계약

## 두 경로의 분리

수집 경로는 BE 준비물 export 또는 선택 SQL 동기화 → 검색어 등록 → PostgreSQL 예약 → 작업자 → AI 상품 DB입니다. 화면 경로는 FE → 인증된 BE → 이 서비스의 DB 조회 → 상품 표시입니다. **사용자 요청은 외부 검색, 수집 예약, 상품 DB 쓰기를 실행하지 않습니다.**

BE는 사용자 인증·활동 공개 상태·접근 권한을 확인하고 준비물 스냅샷을 전달합니다. 이 서비스는 전달된 활동의 실제 존재·권한·최신 버전을 운영 DB와 대조하지 않습니다. BE·FE 코드는 변경하지 않았으므로 어댑터와 화면 연결은 별도 작업입니다.

## 기존 준비물 매핑

읽기 검토에 사용한 BE 사본은 커밋 `938077b7b3c6494929f86d3eee846a4ee1427508`입니다. 실제 운영 DB를 조회한 결과는 아닙니다.

| BE 원본 | 내부 요청 |
| --- | --- |
| 활동 ID, `contentVersion`, `reviewStatus` | `activityId`, `activityContentVersion`, `reviewStatus` |
| `activity_material_id` | `materials[].materialId` |
| `material_name` | `materials[].name` |
| `quantity_description` | `materials[].quantity` |
| `is_required` | `materials[].isRequired` |
| `acquisition_type` | `materials[].acquisitionType` |

기존 활동 상세의 `result.materials[]` 구조를 재사용합니다. 상세 응답에 없는 `reviewStatus`는 BE가 확인해야 합니다. `quantity`·`acquisitionType`의 nullable 의미를 유지하고 임의 규격·소유 여부로 변환하지 않습니다. [기존 상세 DTO](https://github.com/Kau-BugFree/hobby-tree-Backend/blob/938077b7b3c6494929f86d3eee846a4ee1427508/src/main/java/com/hobbytree/hobbytree/domain/activity/dto/ActivityDetailResponse.java#L24).

기존 `sponsored-content`는 활동에 수동 연결한 광고 콘텐츠이며 이 상품 DB와 별개입니다. 광고 콘텐츠에 없는 가격을 만들어 넣지 않습니다.

## 조회 요청

```http
POST /internal/v1/item-recommendations
Content-Type: application/json
X-Service-Token: <내부 서비스 토큰>
```

운영 모드는 PostgreSQL 연결과 32자 이상의 `SERVICE_TOKEN`을 요구합니다. 로컬에서 토큰을 설정하지 않았을 때만 헤더를 생략할 수 있습니다. 공급자 API 키나 내부 토큰을 FE에 전달하지 않습니다.

| 필드 | 계약 |
| --- | --- |
| `activityId`, `activityContentVersion` | 1 이상 정수 |
| `reviewStatus` | `PUBLISHED` |
| `materials` | 최대 20개, ID 중복 금지, 빈 목록 허용 |
| `materials[].materialId/name` | 1 이상 정수 / 최대 200자, 공백만인 이름·제어문자 금지 |
| `materials[].quantity/isRequired/acquisitionType` | 최대 160자 또는 null / boolean 기본 true / 최대 30자 또는 null |
| `maxProductsPerMaterial` | 1~5, 기본 3 |
| `materialSelections` | 요청에 포함된 준비물 ID별 선택. 최대 20개, ID 중복 금지 |
| `contextText`, `preferencesVersion` | 과거 호출 호환용. Q14 해석·상품 선택·외부 검색에 사용하지 않음 |

`contextText`를 새 호출에서 전달할 필요가 없습니다. 요청을 받아도 상품·작업 DB에 저장하지 않습니다. 응답의 `onboardingFilteringApplied`는 항상 `false`이고 과거의 `filterPlan`·`unverifiedProducts`는 새 계약에 없습니다.

준비물별 선택 구조는 다음과 같습니다.

```json
{
  "materialId": 1001,
  "budgetKrw": 10000,
  "offset": 0,
  "catalogRevision": null,
  "filters": {
    "merchantCodes": [],
    "minPriceKrw": null,
    "maxPriceKrw": null,
    "excludeOfferRefs": []
  }
}
```

`budgetKrw`는 상품 하나와 비교할 예산입니다. 0도 유효하며 생략 또는 null이면 비교하지 않습니다. 예산이 초과되어도 상품 순서·노출은 바뀌지 않습니다. FE가 이 값을 자동으로 `filters.maxPriceKrw`에 복사하면 계약과 달라집니다.

명시적 상품 필터는 별개 기능입니다. 쇼핑몰 코드 목록, 최소·최대 가격, 제외할 후보 ID를 지원합니다. 가격 필터를 켜면 가격을 모르는 후보는 해당 범위를 확인할 수 없으므로 제외합니다. 필터 결과가 없을 때는 `FILTER_EMPTY`를 표시하고 필터 해제를 안내합니다. 가격·예산 범위는 0~1조 원이며 최소 가격이 최대 가격보다 클 수 없습니다.

## 정렬·대체 목록·페이지 이동

유효한 기본 검색 목록 → 운영자가 지정한 동의어 목록 순서 → 운영자 지정 상품 순서로 사용할 목록을 선택합니다. 출처는 `selectionSource=PRIMARY/ALIAS/CURATED/NONE`입니다. 대체 후보도 미리 저장돼 있어야 하며, 조회 중 새 검색어를 만들거나 외부 요청을 보내지 않습니다.

선택한 목록 안에서 명시적 필터를 적용하고 **숫자 가격 오름차순, 가격 미확인 마지막**으로 정렬합니다. 같은 가격은 내부 후보 ID로 순서를 고정합니다. 전 쇼핑몰의 실시간 최저가를 찾았다는 뜻은 아닙니다. 기본 목록에 후보가 있으나 화면 필터로 모두 제외된 경우에는 다른 출처로 자동 변경하지 않습니다.

`offset`은 0~1000입니다. 첫 요청은 0으로 보내고, 다음 페이지에서는 응답의 `nextOffset`과 `catalogRevision`을 함께 전달하세요. 버전이 달라지면 `CATALOG_CHANGED`가 되므로 목록을 비우고 offset 0, revision null로 다시 조회합니다. 필터를 바꿀 때도 offset을 0으로 초기화합니다. `totalCandidates`는 선택한 저장 목록에서 필터를 적용한 뒤의 개수이며 전체 커머스 상품 수가 아닙니다.

새로고침 버튼은 DB 목록을 다시 읽습니다. 공급자 재수집을 의미하지 않습니다. 향후 갱신 요청을 추가할 경우 예약 큐와 호출량 정책을 별도 계약으로 확장해야 합니다.

## 응답과 FE 상태

wrapper는 `isSuccess/code/message/result`이며 정상 처리 code는 `ITEM_RECOMMENDATIONS_PROCESSED`입니다. HTTP 200은 DB 조회 요청을 처리했다는 뜻이며 모든 준비물에 상품이 있다는 뜻이 아닙니다.

`result`는 활동 ID·버전, 생성 시각, 전체 `status`, `sort=PRICE_ASC_NULLS_LAST`, `materialResults`, 제휴 문구·안내를 포함합니다. 준비물별 상태가 다르면 `PARTIAL`, 준비물이 없으면 `NO_MATERIALS`입니다.

| 준비물 상태 | FE 처리 |
| --- | --- |
| `AVAILABLE` | 기본 검색의 저장 상품 표시 |
| `FALLBACK_AVAILABLE` | 동의어 또는 운영자 지정 상품임을 구분해 표시 |
| `NOT_COLLECTED` | 구매 정보를 준비 중이라고 표시 |
| `NO_MATCH` | 수집은 완료했지만 표시할 상품이 없으므로 준비물 안내 유지 |
| `EXPIRED` | 만료 상품을 표시하지 않고 갱신 대기 안내 |
| `TEMPORARY_FAILURE` | 최근 수집 오류. 정상 검색 0건과 구분 |
| `INVALID_CANDIDATES` | 반환 후보가 표시 조건을 충족하지 못함. 준비물 안내 유지 |
| `FILTER_EMPTY` | 상품 필터 해제·변경 안내 |
| `PAGE_EMPTY` | 페이지 끝. 목록 재설정 가능 |
| `CATALOG_CHANGED` | 수집 버전이 바뀌었으므로 첫 페이지로 재설정 |
| `STORAGE_NOT_APPROVED` | 상품 보관 사용 확인 전. 빈 상품 목록과 준비물 안내 |

`displayMessage`를 기본 안내로 사용할 수 있고 `fallbackAction=SHOW_MATERIAL_GUIDE/RESET_LIST/NONE`을 UI 행동으로 연결합니다. 빈 상품 목록 때문에 준비물 자체를 삭제하거나 관련 없는 인기 상품을 채우면 안 됩니다.

준비물 결과에는 원본 `material`, `selectionSource`, `catalogRevision`, `expiresAt`, `products`, `totalCandidates`, `nextOffset`, `necessityReason`이 있습니다. `necessityReason`은 현재 null이며 화면에서 이유 영역을 숨깁니다. 앞으로 활동 단계·설명에 근거한 운영자 작성 내용을 연결할 수 있습니다.

`providerStatus`, `lastAttemptAt`, `discardedCount`, `discardReasons`는 기본 검색의 수집 진단입니다. `lastAttemptAt`은 기본 검색의 최근 시도 시각이며 성공 수집을 뜻하지 않습니다. 대체 상품을 표시하는 중에도 기본 검색의 오류가 남을 수 있습니다. 표시 상품의 수집 시각은 각 상품의 `fetchedAt`으로 확인하세요.

상품은 `offerRef/providerProductId/title/displayPrice/priceKrw/imageUrl/purchaseUrl/merchantCode/merchantName/fetchedAt`과 `budgetComparison`을 제공합니다. 원본에 상품 ID·가격·이미지가 없으면 null일 수 있습니다. 내부 `offerRef`를 판매처 SKU로 취급하지 마세요.

| `budgetComparison.status` | 의미 |
| --- | --- |
| `NOT_SET` | 예산 미입력 |
| `PRICE_UNKNOWN` | 예산은 있지만 비교할 숫자 가격이 없음 |
| `WITHIN_BUDGET` | 상품 표시 가격이 예산 이하 |
| `OVER_BUDGET` | 상품 표시 가격이 예산 초과. 상품은 그대로 노출 |

`differenceKrw`는 상품 가격에서 예산을 뺀 값입니다. 비교는 배송비·옵션 등 확인되지 않은 비용을 제외한 상품 하나의 표시 가격 기준이며 준비물 수량·장바구니 합계를 계산하지 않습니다. `priceIsReference=true`, `availability=UNKNOWN`이므로 실제 재고와 최종 결제 금액 확인을 안내합니다.

## 운영자 API

아래 내부 경로는 같은 서비스 토큰을 사용하며 FE의 일반 사용자 기능으로 공개하면 안 됩니다.

| 경로 | 본문·역할 |
| --- | --- |
| `POST /internal/v1/catalog/queries` | `{"queries":["계량스푼"]}` — 수집 검색어 등록 |
| `PUT /internal/v1/catalog/fallbacks` | `{"primaryQuery":"계량스푼","fallbackQueries":["계량 스푼"]}` — 확인된 동의어 등록 |
| `PUT /internal/v1/catalog/curated` | `query/products/expiresAt` — 운영자가 확인한 대체 상품 등록 |

동의어는 API에서 최대 5개를 받습니다. 운영자 지정 상품은 제목·숫자 가격 또는 null·이미지 URL 또는 null·구매 URL·쇼핑몰 코드와 이름을 받습니다. 제품 사양이나 가격을 임의 생성하지 말고 실제 허용된 근거로 입력합니다. 만료 시각은 저장 허용 기간 안에서 설정해야 합니다. 이 경로들도 즉시 외부 API를 호출하지 않습니다.

## 오류·연동 체크

인증 오류는 401, 64 KiB 초과 요청은 413, 입력 검증 오류는 422입니다. DB 조회·관리 실패는 503 `CATALOG_UNAVAILABLE`입니다. 저장 허용이 꺼진 상태에서 운영자 지정 상품 등록을 시도하면 403 `STORAGE_NOT_APPROVED`를 반환합니다. 오류는 `isSuccess=false`·`code`·`message`·`result=null` 형식이며 사용자 원문이나 공급자 키를 그대로 반환하지 않습니다. 운영에서는 자동 API 문서가 꺼지고, 로컬에서는 `/docs`에서 현재 스키마를 확인할 수 있습니다.

BE는 활동 공개·권한 검증과 내부 토큰, 자료형·버전 매핑을 담당합니다. FE는 캐시 상태·대체 출처·페이지 버전·가격 미확인·예산 비교·제휴 문구를 담당합니다. 구매 버튼은 반환된 제휴 링크를 사용하되, 링크 열기는 구매 성공이나 커미션 지급 확인이 아닙니다.

`GET /health`는 `servingMode=CATALOG_ONLY`, `catalogBackend`, `storageApproved`, `upstreamCallsOnRequest=false`, `batchWindow` 등의 실행 설정을 반환합니다. `providerConnectivityChecked=false`이며 실제 공급자 연결 성공을 뜻하지 않습니다. 공급자 키는 조회 API의 필수 설정이 아닙니다.

수집 준비와 권한·보관 설정은 [batch.md](batch.md)를 참고하세요. 기존 Q14 상품 필터 파서는 제거했습니다. 호환 입력 필드만 유지합니다.
