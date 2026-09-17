"""Data models for Polymarket Negative Risk (Neg-Risk) Basket Arbitrage and Market Making."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class OutcomeBucket:
    bucket_id: str
    label: str
    yes_token_id: str
    no_token_id: str | None = None
    lo: float | None = None
    hi: float | None = None
    neg_risk: bool = True
    market_id: str | None = None


@dataclass(frozen=True)
class EventMarket:
    event_id: str
    event_slug: str
    title: str
    buckets: tuple[OutcomeBucket, ...]
    neg_risk: bool = True
    category: str = "temperature"  # temperature, politics, fed_rates, crypto_range
    resolution_date: str | None = None


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class BucketBook:
    token_id: str
    label: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    bids: tuple[BookLevel, ...] = field(default_factory=tuple)
    asks: tuple[BookLevel, ...] = field(default_factory=tuple)
    fetched_at: float = 0.0

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / Decimal("2")
        return self.best_bid if self.best_bid is not None else self.best_ask


@dataclass(frozen=True)
class BasketArbLeg:
    token_id: str
    label: str
    side: Literal["BUY", "SELL"]
    price: Decimal
    size: Decimal
    cost_or_proceed: Decimal


@dataclass(frozen=True)
class BasketArbOpportunity:
    arb_type: Literal["LONG_BASKET_BUY", "SHORT_BASKET_SELL"]
    event_id: str
    event_slug: str
    title: str
    legs: tuple[BasketArbLeg, ...]
    sum_price: Decimal
    executable_shares: Decimal
    total_cost_usdc: Decimal
    expected_payout_usdc: Decimal
    net_profit_usdc: Decimal
    roi_percent: Decimal
    timestamp: float


@dataclass(frozen=True)
class MakerQuote:
    token_id: str
    label: str
    fair_prob: Decimal
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal
    spread: Decimal
    spread_pct: Decimal


@dataclass(frozen=True)
class MakerPlan:
    event_id: str
    event_slug: str
    title: str
    quotes: tuple[MakerQuote, ...]
    sum_bid: Decimal
    sum_ask: Decimal
    maker_spread: Decimal
    timestamp: float
    is_structurally_safe: bool  # True if sum_bid < 1.00 and sum_ask > 1.00
