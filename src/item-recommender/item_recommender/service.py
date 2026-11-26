"""Read-only catalog serving. No network calls, query rewriting or onboarding filtering."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol, Sequence

from .catalog_domain import CatalogStore, StoredSnapshot, query_key
from .config import Settings
from .domain import Offer
from .matching import material_matches
from .models import (
    BudgetComparison,
    MaterialResult,
    MaterialSelection,
    ProductFilters,
    ProductResponse,
    RecommendationRequest,
    RecommendationResponse,
    RecommendationResult,
)
from .provider import purchase_url


def compare_budget(price: int | None, budget: int | None) -> BudgetComparison:
    if budget is None:
        return BudgetComparison(
            status="NOT_SET", budget_krw=None, message="상품 1개의 예산을 설정할 수 있어요."
        )
    if price is None:
        return BudgetComparison(
            status="PRICE_UNKNOWN",
            budget_krw=budget,
            message="가격 확인 후 예산과 비교할 수 있어요.",
        )
    difference = price - budget
    if difference > 0:
        message = f"설정한 예산보다 {difference:,}원 높아요."
    elif difference < 0:
        message = f"설정한 예산보다 {-difference:,}원 낮아요."
    else:
        message = "설정한 예산과 같아요."
    return BudgetComparison(
        status="OVER_BUDGET" if difference > 0 else "WITHIN_BUDGET",
        budget_krw=budget,
        difference_krw=difference,
        message=message,
    )


class CandidateFilter(Protocol):
    def apply(self, offers: list[Offer], filters: ProductFilters) -> list[Offer]: ...


class MetadataFilter:
    """Extension point for explicit, supported product attributes only."""

    def apply(self, offers: list[Offer], filters: ProductFilters) -> list[Offer]:
        selected = []
        for offer in offers:
            if filters.merchant_codes and offer.merchant_code not in filters.merchant_codes:
                continue
            if offer.offer_ref in filters.exclude_offer_refs:
                continue
            if filters.min_price_krw is not None and (
                offer.price_krw is None or offer.price_krw < filters.min_price_krw
            ):
                continue
            if filters.max_price_krw is not None and (
                offer.price_krw is None or offer.price_krw > filters.max_price_krw
            ):
                continue
            selected.append(offer)
        return selected


class RecommendationService:
    def __init__(
        self,
        store: CatalogStore,
        settings: Settings,
        clock=None,
        candidate_filters: Sequence[CandidateFilter] | None = None,
    ):
        self.store = store
        self.settings = settings
        self.clock = (
            clock or getattr(store, "current_time", None) or (lambda: datetime.now(timezone.utc))
        )
        self.candidate_filters = (
            tuple(candidate_filters) if candidate_filters is not None else (MetadataFilter(),)
        )

    def _usable(self, snapshot: StoredSnapshot, now: datetime) -> list[Offer]:
        if not snapshot.expires_at or snapshot.expires_at <= now:
            return []
        valid, seen = [], set()
        for offer in snapshot.offers:
            if not purchase_url(
                offer.purchase_url,
                self.settings.allowed_purchase_hosts,
                self.settings.adpick_api_key,
            ):
                continue
            # Curated associations have been explicitly registered by an operator.
            if snapshot.source != "CURATED" and not material_matches(
                snapshot.query_key, offer.title
            ):
                continue
            identity = (offer.merchant_code, offer.purchase_url)
            if identity in seen:
                continue
            seen.add(identity)
            valid.append(offer)
        return valid

    def _select_snapshot(self, key: str, now: datetime):
        primary = self.store.read_snapshot(key)
        offers = self._usable(primary, now)
        if offers:
            return primary, offers, "PRIMARY", primary
        for alias in self.store.fallback_keys(key):
            snapshot = self.store.read_snapshot(alias)
            offers = self._usable(snapshot, now)
            if offers:
                return snapshot, offers, "ALIAS", primary
        curated = self.store.read_snapshot(key, source="CURATED")
        offers = self._usable(curated, now)
        if offers:
            return curated, offers, "CURATED", primary
        return primary, [], "NONE", primary

    @staticmethod
    def _empty_status(snapshot: StoredSnapshot, now: datetime):
        if snapshot.last_status == "PROVIDER_UNUSABLE_RESULTS":
            return (
                "INVALID_CANDIDATES",
                "상품 정보를 확인 중이에요. 준비물 안내를 먼저 확인해주세요.",
            )
        if snapshot.last_status.startswith("PROVIDER_") or snapshot.last_status == "LEASE_EXPIRED":
            return "TEMPORARY_FAILURE", "상품 정보를 확인 중이에요. 잠시 후 다시 확인해주세요."
        if snapshot.expires_at and snapshot.expires_at <= now:
            return "EXPIRED", "상품 정보를 갱신하고 있어요. 준비물 안내를 먼저 확인해주세요."
        if snapshot.last_status in {"NOT_COLLECTED", "BATCH_WINDOW_CLOSED"}:
            return "NOT_COLLECTED", "이 준비물의 구매 정보를 준비하고 있어요."
        if snapshot.offers or snapshot.discarded_count:
            return (
                "INVALID_CANDIDATES",
                "상품 정보를 확인 중이에요. 준비물 안내를 먼저 확인해주세요.",
            )
        return "NO_MATCH", "이 준비물의 구매 정보를 준비하고 있어요."

    def _recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        now = self.clock()
        selections = {selection.material_id: selection for selection in request.material_selections}
        results = []
        # Same-name materials share one read inside this request. Nothing is persisted here.
        pools = {}
        for material in request.materials:
            if not self.settings.storage_approved:
                results.append(
                    MaterialResult(
                        material=material,
                        status="STORAGE_NOT_APPROVED",
                        provider_status="NOT_QUERIED",
                        display_message="이 준비물의 구매 정보를 준비하고 있어요.",
                        fallback_action="SHOW_MATERIAL_GUIDE",
                    )
                )
                continue
            key = query_key(material.name)
            if key not in pools:
                pools[key] = self._select_snapshot(key, now)
            snapshot, offers, source, primary = pools[key]
            shared = dict(
                material=material,
                provider_status=primary.last_status,
                catalog_revision=snapshot.revision,
                expires_at=snapshot.expires_at,
                last_attempt_at=primary.last_attempt_at,
                discarded_count=primary.discarded_count,
                discard_reasons=dict(primary.discard_reasons),
            )
            if not offers:
                status, message = self._empty_status(primary, now)
                results.append(
                    MaterialResult(
                        **shared,
                        status=status,
                        display_message=message,
                        fallback_action="SHOW_MATERIAL_GUIDE",
                    )
                )
                continue
            selection = selections.get(
                material.material_id, MaterialSelection(material_id=material.material_id)
            )
            if (
                selection.catalog_revision is not None
                and selection.catalog_revision != snapshot.revision
            ):
                results.append(
                    MaterialResult(
                        **shared,
                        status="CATALOG_CHANGED",
                        selection_source=source,
                        display_message="상품 목록이 갱신되었어요. 처음부터 다시 확인해주세요.",
                        fallback_action="RESET_LIST",
                    )
                )
                continue
            selected = list(offers)
            for candidate_filter in self.candidate_filters:
                selected = candidate_filter.apply(selected, selection.filters)
            # Budget is never used as a filter or ranking signal.
            selected.sort(
                key=lambda offer: (offer.price_krw is None, offer.price_krw or 0, offer.offer_ref)
            )
            if not selected:
                results.append(
                    MaterialResult(
                        **shared,
                        status="FILTER_EMPTY",
                        selection_source=source,
                        display_message="선택한 상품 필터에 맞는 결과가 없어요. 필터를 변경해주세요.",
                        fallback_action="RESET_LIST",
                    )
                )
                continue
            end = selection.offset + request.max_products_per_material
            page = selected[selection.offset : end]
            products = [
                ProductResponse(
                    **asdict(offer),
                    budget_comparison=compare_budget(offer.price_krw, selection.budget_krw),
                )
                for offer in page
            ]
            results.append(
                MaterialResult(
                    **shared,
                    status=("AVAILABLE" if source == "PRIMARY" else "FALLBACK_AVAILABLE")
                    if products
                    else "PAGE_EMPTY",
                    selection_source=source,
                    products=products,
                    total_candidates=len(selected),
                    next_offset=end if end < len(selected) else None,
                    display_message="낮은 가격순으로 보여드려요."
                    if products
                    else "목록의 마지막 상품까지 확인했어요.",
                    fallback_action="NONE" if products else "RESET_LIST",
                )
            )
        statuses = {result.status for result in results}
        overall = (
            "NO_MATERIALS"
            if not statuses
            else next(iter(statuses))
            if len(statuses) == 1
            else "PARTIAL"
        )
        return RecommendationResponse(
            result=RecommendationResult(
                activity_id=request.activity_id,
                activity_content_version=request.activity_content_version,
                preferences_version=request.preferences_version,
                generated_at=now,
                status=overall,
                material_results=results,
                affiliate_disclosure="이 링크를 통한 구매 시 하비트리는 일정액의 수수료를 제공받을 수 있습니다.",
                notices=[
                    "가격은 수집 시점의 참고 가격입니다. 최종 가격·배송비·옵션·재고는 판매처에서 확인해주세요.",
                    "예산은 현재 상품 1개의 표시 가격과 비교하며, 예산을 초과해도 상품을 제외하지 않습니다.",
                ],
            )
        )

    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        return await asyncio.to_thread(self._recommend, request)
