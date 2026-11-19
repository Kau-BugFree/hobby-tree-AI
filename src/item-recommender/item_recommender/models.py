"""Internal BE snapshots and catalog selection; onboarding text is never evaluated."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

PositiveId = Annotated[int, Field(gt=0, le=9_223_372_036_854_775_807, strict=True)]
Version = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807, strict=True)]
Money = Annotated[int, Field(ge=0, le=1_000_000_000_000, strict=True)]


def validate_query(value: str) -> str:
    if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("query must be nonblank without control characters")
    return value.strip()


QueryText = Annotated[str, Field(min_length=1, max_length=160), AfterValidator(validate_query)]


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Material(Model):
    material_id: PositiveId
    name: QueryText
    quantity: Annotated[str, Field(max_length=160)] | None = None
    is_required: StrictBool = True
    acquisition_type: Annotated[str, Field(max_length=30)] | None = None

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("name must be nonblank without control characters")
        return value.strip()


class ProductFilters(Model):
    """Explicit product-list filters, independent of onboarding and budget comparison."""

    merchant_codes: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=10
    )
    min_price_krw: Money | None = None
    max_price_krw: Money | None = None
    exclude_offer_refs: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=100
    )

    @model_validator(mode="after")
    def valid_price_range(self):
        if (
            self.min_price_krw is not None
            and self.max_price_krw is not None
            and self.min_price_krw > self.max_price_krw
        ):
            raise ValueError("minPriceKrw cannot exceed maxPriceKrw")
        return self


class MaterialSelection(Model):
    material_id: PositiveId
    budget_krw: Money | None = None
    offset: Annotated[int, Field(ge=0, le=1000, strict=True)] = 0
    catalog_revision: Annotated[str, Field(max_length=100)] | None = None
    filters: ProductFilters = Field(default_factory=ProductFilters)


class RecommendationRequest(Model):
    activity_id: PositiveId
    activity_content_version: PositiveId
    review_status: Literal["PUBLISHED"]
    # Accepted for older BE callers, but never interpreted, persisted or sent to Adpick.
    preferences_version: Version = 0
    context_text: Annotated[str, Field(max_length=200)] | None = None
    materials: list[Material] = Field(max_length=20)
    material_selections: list[MaterialSelection] = Field(default_factory=list, max_length=20)
    max_products_per_material: Annotated[int, Field(ge=1, le=5, strict=True)] = 3

    @model_validator(mode="after")
    def valid_material_ids(self):
        ids = [material.material_id for material in self.materials]
        selected = [selection.material_id for selection in self.material_selections]
        if len(ids) != len(set(ids)) or len(selected) != len(set(selected)):
            raise ValueError("materialId values must be unique")
        if not set(selected).issubset(ids):
            raise ValueError("materialSelections must refer to requested materials")
        return self


class BudgetComparison(Model):
    status: Literal["WITHIN_BUDGET", "OVER_BUDGET", "PRICE_UNKNOWN", "NOT_SET"]
    budget_krw: int | None
    difference_krw: int | None = None
    message: str
    basis: Literal["DISPLAYED_PRODUCT_PRICE_EXCLUDING_UNCONFIRMED_SHIPPING_AND_OPTIONS"] = (
        "DISPLAYED_PRODUCT_PRICE_EXCLUDING_UNCONFIRMED_SHIPPING_AND_OPTIONS"
    )


class ProductResponse(Model):
    offer_ref: str
    provider: str = "ADPICK"
    provider_product_id: str | None = None
    title: str
    display_price: str
    price_krw: int | None
    currency: Literal["KRW"] = "KRW"
    image_url: str | None
    purchase_url: str
    merchant_code: str
    merchant_name: str
    fetched_at: datetime
    price_is_reference: Literal[True] = True
    availability: Literal["UNKNOWN"] = "UNKNOWN"
    budget_comparison: BudgetComparison


MaterialStatus = Literal[
    "AVAILABLE",
    "FALLBACK_AVAILABLE",
    "NOT_COLLECTED",
    "NO_MATCH",
    "EXPIRED",
    "TEMPORARY_FAILURE",
    "INVALID_CANDIDATES",
    "FILTER_EMPTY",
    "PAGE_EMPTY",
    "CATALOG_CHANGED",
    "STORAGE_NOT_APPROVED",
]


class MaterialResult(Model):
    material: Material
    status: MaterialStatus
    provider_status: str
    products: list[ProductResponse] = Field(default_factory=list)
    necessity_reason: str | None = None
    selection_source: Literal["PRIMARY", "ALIAS", "CURATED", "NONE"] = "NONE"
    catalog_revision: str | None = None
    expires_at: datetime | None = None
    last_attempt_at: datetime | None = None
    display_message: str
    fallback_action: Literal["NONE", "SHOW_MATERIAL_GUIDE", "RESET_LIST"] = "NONE"
    total_candidates: int = 0
    next_offset: int | None = None
    discarded_count: int = 0
    discard_reasons: dict[str, int] = Field(default_factory=dict)


class RecommendationResult(Model):
    activity_id: int
    activity_content_version: int
    preferences_version: int
    generated_at: datetime
    status: MaterialStatus | Literal["PARTIAL", "NO_MATERIALS"]
    onboarding_filtering_applied: Literal[False] = False
    sort: Literal["PRICE_ASC_NULLS_LAST"] = "PRICE_ASC_NULLS_LAST"
    material_results: list[MaterialResult]
    affiliate_disclosure: str
    notices: list[str]


class RecommendationResponse(Model):
    is_success: Literal[True] = True
    code: Literal["ITEM_RECOMMENDATIONS_PROCESSED"] = "ITEM_RECOMMENDATIONS_PROCESSED"
    message: str = "저장된 준비물별 상품을 가격순으로 조회했습니다."
    result: RecommendationResult


class QueryRegistration(Model):
    queries: list[QueryText] = Field(min_length=1, max_length=1000)


class FallbackRegistration(Model):
    primary_query: QueryText
    fallback_queries: list[QueryText] = Field(max_length=5)


class CuratedProduct(Model):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    price_krw: Money | None = None
    image_url: Annotated[str, Field(max_length=4096)] | None = None
    purchase_url: Annotated[str, Field(min_length=1, max_length=4096)]
    merchant_code: Annotated[str, Field(min_length=1, max_length=100)]
    merchant_name: Annotated[str, Field(min_length=1, max_length=200)]


class CuratedRegistration(Model):
    query: QueryText
    products: list[CuratedProduct] = Field(max_length=20)
    expires_at: datetime
