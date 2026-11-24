"""Shared internal values; provider payloads and user text are never executable instructions."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Offer:
    offer_ref: str
    title: str
    display_price: str
    price_krw: int | None
    image_url: str | None
    purchase_url: str
    merchant_code: str
    merchant_name: str
    fetched_at: datetime
    provider: str = "ADPICK"
    provider_product_id: str | None = None


@dataclass(frozen=True)
class SearchResult:
    offers: tuple[Offer, ...] = ()
    discarded_count: int = 0
    discard_reasons: tuple[tuple[str, int], ...] = ()


class ProviderError(Exception):
    def __init__(self, code: str, retry_after_seconds: int | None = None):
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)
