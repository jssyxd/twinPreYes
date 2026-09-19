"""Negative Risk (Neg-Risk) Convexity Arbitrage Strategy.

Incorporates Jane Street High-Frequency & Arbitrage Principles:
1. Book Synchrony / Stale Book Rejection (Zero Phantom Arbitrage):
   Rejects opportunities if book skew across legs > max_book_skew_seconds (default 300ms)
   or if any book is older than max_book_age_seconds (default 1.5s).
2. True Economic Hurdle (Risk Premium):
   Default min_profit_pct = 5.0% (0.05) to properly cover legging risk, exchange fees,
   and adverse selection costs.
3. Bottleneck-First Leg Ordering (Fragile-First Sequencing):
   Legs are deterministically ordered by available depth ascending.
   The least liquid / most fragile leg is placed at Index 0 (Probe Leg).
4. Worst-Acceptable-Price Limits:
   Pre-computes max executable slippage threshold per leg.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Sequence

from neg_risk.models import (
    BasketArbLeg,
    BasketArbOpportunity,
    BucketBook,
    EventMarket,
)


class NegRiskArbitrageEngine:
    def __init__(
        self,
        *,
        min_profit_pct: Decimal | float = Decimal("0.05"),  # 5% minimum net margin hurdle (Jane Street rule)
        max_position_usdc: Decimal | float = Decimal("50.0"),  # max USDC per arbitrage basket
        min_order_usdc: Decimal | float = Decimal("1.0"),
        max_book_skew_seconds: float = 0.300,  # 300ms maximum time discrepancy between books
        max_book_age_seconds: float = 5.0,  # 5.0s maximum snapshot staleness
        max_leg_slippage_pct: Decimal | float = Decimal("0.01"),  # 1% worst acceptable price buffer
    ) -> None:
        self.min_profit_pct = Decimal(str(min_profit_pct))
        self.max_position_usdc = Decimal(str(max_position_usdc))
        self.min_order_usdc = Decimal(str(min_order_usdc))
        self.max_book_skew_seconds = float(max_book_skew_seconds)
        self.max_book_age_seconds = float(max_book_age_seconds)
        self.max_leg_slippage_pct = Decimal(str(max_leg_slippage_pct))

    def _verify_books_freshness(
        self,
        event: EventMarket,
        books: dict[str, BucketBook],
        now: float,
    ) -> tuple[bool, float]:
        """Check if all books exist, are fresh, and have low latency skew across the basket."""
        timestamps: list[float] = []
        for bucket in event.buckets:
            book = books.get(bucket.yes_token_id)
            if book is None or book.fetched_at <= 0.0:
                return False, 0.0
            age = now - book.fetched_at
            if age > self.max_book_age_seconds or age < -0.1:  # future skew or too stale
                return False, 0.0
            timestamps.append(book.fetched_at)

        skew = max(timestamps) - min(timestamps)
        if skew > self.max_book_skew_seconds:
            return False, skew * 1000.0
        return True, skew * 1000.0

    def evaluate_long_basket(
        self,
        event: EventMarket,
        books: dict[str, BucketBook],
        now: float | None = None,
    ) -> BasketArbOpportunity | None:
        """Evaluate if buying 1 share of ALL outcome Yes tokens yields guaranteed profit (SumAsk < 1.00).

        Applies:
        - Latency skew / stale quote rejection
        - Bottleneck-first leg sorting (thinnest liquidity first)
        - Worst acceptable limit price calculation
        """
        if not event.buckets or len(event.buckets) < 2:
            return None

        current_time = time.time() if now is None else now
        is_fresh, skew_ms = self._verify_books_freshness(event, books, current_time)
        if not is_fresh:
            return None

        raw_legs: list[dict] = []
        sum_ask = Decimal("0")
        min_depth = Decimal("999999999")
        bottleneck_token = ""

        for bucket in event.buckets:
            tok = bucket.yes_token_id
            book = books.get(tok)
            if book is None or book.best_ask is None or book.ask_size <= Decimal("0"):
                # Complete set broken
                return None

            best_ask = book.best_ask
            avail_depth = book.ask_size

            sum_ask += best_ask
            if avail_depth < min_depth:
                min_depth = avail_depth
                bottleneck_token = tok

            # Worst acceptable price: best_ask + buffer (cap at 0.99)
            worst_px = min(Decimal("0.99"), best_ask + self.max_leg_slippage_pct).quantize(Decimal("0.01"))

            raw_legs.append({
                "token_id": tok,
                "label": bucket.label,
                "price": best_ask,
                "depth": avail_depth,
                "worst_px": worst_px,
            })

        # Hurdle: Sum of asks must be strictly below 1.00 - min_profit_pct (e.g., < 0.95)
        target_max_sum = Decimal("1.00") - self.min_profit_pct
        if sum_ask >= target_max_sum or sum_ask <= Decimal("0"):
            return None

        # Sizing: strictly constrained by the thinnest bottleneck depth and position cap
        shares_by_capital = self.max_position_usdc / sum_ask
        exec_shares = min(min_depth, shares_by_capital)

        total_cost = (sum_ask * exec_shares).quantize(Decimal("0.0001"))
        if total_cost < self.min_order_usdc or exec_shares <= Decimal("0"):
            return None

        # JANE STREET PRINCIPLE: Bottleneck-First Leg Sequencing
        # Sort legs by available_depth ascending so the most fragile leg is Index 0 (Probe Leg)
        raw_legs.sort(key=lambda x: x["depth"])

        final_legs: list[BasketArbLeg] = []
        for i, item in enumerate(raw_legs):
            leg_cost = (item["price"] * exec_shares).quantize(Decimal("0.0001"))
            final_legs.append(
                BasketArbLeg(
                    token_id=item["token_id"],
                    label=item["label"],
                    side="BUY",
                    price=item["price"],
                    size=exec_shares,
                    cost_or_proceed=leg_cost,
                    available_depth=item["depth"],
                    is_bottleneck=(i == 0),
                    worst_acceptable_price=item["worst_px"],
                )
            )

        expected_payout = exec_shares.quantize(Decimal("0.0001"))
        net_profit = (expected_payout - total_cost).quantize(Decimal("0.0001"))
        roi = ((net_profit / total_cost) * Decimal("100")).quantize(Decimal("0.01"))

        return BasketArbOpportunity(
            arb_type="LONG_BASKET_BUY",
            event_id=event.event_id,
            event_slug=event.event_slug,
            title=event.title,
            legs=tuple(final_legs),
            sum_price=sum_ask,
            executable_shares=exec_shares,
            total_cost_usdc=total_cost,
            expected_payout_usdc=expected_payout,
            net_profit_usdc=net_profit,
            roi_percent=roi,
            timestamp=current_time,
            max_book_skew_ms=round(skew_ms, 2),
            bottleneck_token_id=bottleneck_token,
            bottleneck_depth=min_depth,
        )

    def evaluate_short_basket(
        self,
        event: EventMarket,
        books: dict[str, BucketBook],
        now: float | None = None,
    ) -> BasketArbOpportunity | None:
        """Evaluate if selling all Yes outcomes against bids yields SumBid > 1.00."""
        if not event.buckets or len(event.buckets) < 2:
            return None

        current_time = time.time() if now is None else now
        is_fresh, skew_ms = self._verify_books_freshness(event, books, current_time)
        if not is_fresh:
            return None

        raw_legs: list[dict] = []
        sum_bid = Decimal("0")
        min_depth = Decimal("999999999")
        bottleneck_token = ""

        for bucket in event.buckets:
            tok = bucket.yes_token_id
            book = books.get(tok)
            if book is None or book.best_bid is None or book.bid_size <= Decimal("0"):
                return None

            best_bid = book.best_bid
            avail_depth = book.bid_size

            sum_bid += best_bid
            if avail_depth < min_depth:
                min_depth = avail_depth
                bottleneck_token = tok

            worst_px = max(Decimal("0.01"), best_bid - self.max_leg_slippage_pct).quantize(Decimal("0.01"))

            raw_legs.append({
                "token_id": tok,
                "label": bucket.label,
                "price": best_bid,
                "depth": avail_depth,
                "worst_px": worst_px,
            })

        target_min_sum = Decimal("1.00") + self.min_profit_pct
        if sum_bid <= target_min_sum:
            return None

        shares_by_capital = self.max_position_usdc / Decimal("1.00")
        exec_shares = min(min_depth, shares_by_capital)
        total_proceeds = (sum_bid * exec_shares).quantize(Decimal("0.0001"))
        liability = exec_shares.quantize(Decimal("0.0001"))

        if total_proceeds < self.min_order_usdc or exec_shares <= Decimal("0"):
            return None

        # Sort by depth ascending
        raw_legs.sort(key=lambda x: x["depth"])

        final_legs: list[BasketArbLeg] = []
        for i, item in enumerate(raw_legs):
            proceed = (item["price"] * exec_shares).quantize(Decimal("0.0001"))
            final_legs.append(
                BasketArbLeg(
                    token_id=item["token_id"],
                    label=item["label"],
                    side="SELL",
                    price=item["price"],
                    size=exec_shares,
                    cost_or_proceed=proceed,
                    available_depth=item["depth"],
                    is_bottleneck=(i == 0),
                    worst_acceptable_price=item["worst_px"],
                )
            )

        net_profit = (total_proceeds - liability).quantize(Decimal("0.0001"))
        roi = ((net_profit / liability) * Decimal("100")).quantize(Decimal("0.01"))

        return BasketArbOpportunity(
            arb_type="SHORT_BASKET_SELL",
            event_id=event.event_id,
            event_slug=event.event_slug,
            title=event.title,
            legs=tuple(final_legs),
            sum_price=sum_bid,
            executable_shares=exec_shares,
            total_cost_usdc=liability,
            expected_payout_usdc=total_proceeds,
            net_profit_usdc=net_profit,
            roi_percent=roi,
            timestamp=current_time,
            max_book_skew_ms=round(skew_ms, 2),
            bottleneck_token_id=bottleneck_token,
            bottleneck_depth=min_depth,
        )

    def scan_all_events(
        self,
        events: Sequence[EventMarket],
        books: dict[str, BucketBook],
        now: float | None = None,
    ) -> list[BasketArbOpportunity]:
        """Scan all candidate events with freshness and bottleneck filters."""
        opportunities: list[BasketArbOpportunity] = []
        for ev in events:
            opp_long = self.evaluate_long_basket(ev, books, now)
            if opp_long is not None:
                opportunities.append(opp_long)
                continue

            opp_short = self.evaluate_short_basket(ev, books, now)
            if opp_short is not None:
                opportunities.append(opp_short)

        return opportunities
