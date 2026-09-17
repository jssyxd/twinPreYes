"""Negative Risk (Neg-Risk) Convexity Arbitrage Strategy.

Core Principle:
In any mutually exclusive, collectively exhaustive event (e.g., Temperature Buckets, Election Winner, Rate Cut Tier):
Exactly ONE outcome resolves to $1.00, all other outcomes resolve to $0.00.
Therefore:
1. Long Basket Convexity: If Sum(Best Asks for all Yes tokens) < 1.00 - fee_hurdle (e.g., < 0.96)
   -> BUY 1 share of ALL Yes tokens simultaneously.
   Cost: Sum(Ask_i) < 0.96 USDC.
   Guaranteed Payout at Resolution: Exactly 1.00 USDC.
   Net Guaranteed Arbitrage Profit: 1.00 - Sum(Ask_i) > 0.04 USDC (Risk-free 4%+ ROI).

2. Short Basket Convexity (Negative Risk Collateral): If Sum(Best Bids for all Yes tokens) > 1.00 + fee_hurdle (e.g., > 1.04)
   -> SELL 1 share of ALL Yes tokens (or mint complete sets and sell into bids).
   Revenue: Sum(Bid_i) > 1.04 USDC.
   Liability at Resolution: Exactly 1.00 USDC.
   Net Guaranteed Arbitrage Profit: Sum(Bid_i) - 1.00 > 0.04 USDC.
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
        min_profit_pct: Decimal = Decimal("0.02"),  # 2% minimum net margin hurdle
        max_position_usdc: Decimal = Decimal("50.0"),  # max USDC per arbitrage basket
        min_order_usdc: Decimal = Decimal("1.0"),
    ) -> None:
        self.min_profit_pct = min_profit_pct
        self.max_position_usdc = max_position_usdc
        self.min_order_usdc = min_order_usdc

    def evaluate_long_basket(
        self,
        event: EventMarket,
        books: dict[str, BucketBook],
    ) -> BasketArbOpportunity | None:
        """Evaluate if buying 1 share of ALL outcome Yes tokens yields guaranteed profit (SumAsk < 1.00)."""
        if not event.buckets or len(event.buckets) < 2:
            return None

        legs: list[BasketArbLeg] = []
        sum_ask = Decimal("0")
        max_executable_shares = Decimal("999999999")

        for bucket in event.buckets:
            tok = bucket.yes_token_id
            book = books.get(tok)
            if book is None or book.best_ask is None or book.ask_size <= Decimal("0"):
                # Complete set broken: cannot guarantee 100% payoff coverage
                return None

            best_ask = book.best_ask
            avail_size = book.ask_size

            sum_ask += best_ask
            if avail_size < max_executable_shares:
                max_executable_shares = avail_size

            legs.append(
                BasketArbLeg(
                    token_id=tok,
                    label=bucket.label,
                    side="BUY",
                    price=best_ask,
                    size=Decimal("0"),  # calculated below
                    cost_or_proceed=Decimal("0"),
                )
            )

        # Hurdle: Sum of asks must be strictly below 1.00 - min_profit_pct
        target_max_sum = Decimal("1.00") - self.min_profit_pct
        if sum_ask >= target_max_sum or sum_ask <= Decimal("0"):
            return None

        # Determine executable sizing based on book depth and capital cap
        shares_by_capital = self.max_position_usdc / sum_ask
        exec_shares = min(max_executable_shares, shares_by_capital)

        # Minimum order check
        total_cost = (sum_ask * exec_shares).quantize(Decimal("0.0001"))
        if total_cost < self.min_order_usdc:
            return None

        final_legs: list[BasketArbLeg] = []
        for leg in legs:
            leg_cost = (leg.price * exec_shares).quantize(Decimal("0.0001"))
            final_legs.append(
                BasketArbLeg(
                    token_id=leg.token_id,
                    label=leg.label,
                    side="BUY",
                    price=leg.price,
                    size=exec_shares,
                    cost_or_proceed=leg_cost,
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
            timestamp=time.time(),
        )

    def evaluate_short_basket(
        self,
        event: EventMarket,
        books: dict[str, BucketBook],
    ) -> BasketArbOpportunity | None:
        """Evaluate if selling all Yes outcomes against bids yields SumBid > 1.00."""
        if not event.buckets or len(event.buckets) < 2:
            return None

        legs: list[BasketArbLeg] = []
        sum_bid = Decimal("0")
        max_executable_shares = Decimal("999999999")

        for bucket in event.buckets:
            tok = bucket.yes_token_id
            book = books.get(tok)
            if book is None or book.best_bid is None or book.bid_size <= Decimal("0"):
                return None

            best_bid = book.best_bid
            avail_size = book.bid_size

            sum_bid += best_bid
            if avail_size < max_executable_shares:
                max_executable_shares = avail_size

            legs.append(
                BasketArbLeg(
                    token_id=tok,
                    label=bucket.label,
                    side="SELL",
                    price=best_bid,
                    size=Decimal("0"),
                    cost_or_proceed=Decimal("0"),
                )
            )

        target_min_sum = Decimal("1.00") + self.min_profit_pct
        if sum_bid <= target_min_sum:
            return None

        # Calculate executable shares
        shares_by_capital = self.max_position_usdc / Decimal("1.00")
        exec_shares = min(max_executable_shares, shares_by_capital)
        total_proceeds = (sum_bid * exec_shares).quantize(Decimal("0.0001"))
        liability = exec_shares.quantize(Decimal("0.0001"))  # Maximum payout liability is 1.00 * shares

        if total_proceeds < self.min_order_usdc:
            return None

        final_legs: list[BasketArbLeg] = []
        for leg in legs:
            proceed = (leg.price * exec_shares).quantize(Decimal("0.0001"))
            final_legs.append(
                BasketArbLeg(
                    token_id=leg.token_id,
                    label=leg.label,
                    side="SELL",
                    price=leg.price,
                    size=exec_shares,
                    cost_or_proceed=proceed,
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
            timestamp=time.time(),
        )

    def scan_all_events(
        self,
        events: Sequence[EventMarket],
        books: dict[str, BucketBook],
    ) -> list[BasketArbOpportunity]:
        """Scan all candidate events for either long or short basket arbitrage."""
        opportunities: list[BasketArbOpportunity] = []
        for ev in events:
            opp_long = self.evaluate_long_basket(ev, books)
            if opp_long is not None:
                opportunities.append(opp_long)
                continue  # If long exists, short cannot exist simultaneously

            opp_short = self.evaluate_short_basket(ev, books)
            if opp_short is not None:
                opportunities.append(opp_short)

        return opportunities
