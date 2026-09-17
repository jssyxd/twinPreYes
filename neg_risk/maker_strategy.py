"""Two-Sided Market Maker (Maker Strategy) for Polymarket Neg-Risk Markets.

Principle:
Instead of paying crossing fees and taking toxic flow as a Taker:
1. Model implied probability distribution across all N buckets.
2. Ensure Sum(Prob_i) == 1.00.
3. Post Bid at (Prob_i - HalfSpread) and Ask at (Prob_i + HalfSpread).
4. Structural Safety Invariant:
   - Sum(Bid_i) < 1.00 (Guarantees if all bids get filled, total cost is strictly less than 1.00)
   - Sum(Ask_i) > 1.00 (Guarantees if all asks get filled, total proceeds are strictly greater than 1.00)
5. Earn the Bid-Ask Spread on two-way order flow + Polymarket liquidity rewards.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Sequence

from neg_risk.models import (
    BucketBook,
    EventMarket,
    MakerPlan,
    MakerQuote,
)


class NegRiskMarketMaker:
    def __init__(
        self,
        *,
        target_spread: Decimal = Decimal("0.04"),  # 4¢ total spread (2¢ half-spread)
        quote_size_usdc: Decimal = Decimal("10.0"),  # quote size per bucket in USDC
        min_quote_price: Decimal = Decimal("0.01"),
        max_quote_price: Decimal = Decimal("0.99"),
        tick_size: Decimal = Decimal("0.01"),
    ) -> None:
        self.target_spread = target_spread
        self.quote_size_usdc = quote_size_usdc
        self.min_quote_price = min_quote_price
        self.max_quote_price = max_quote_price
        self.tick_size = tick_size

    def compute_fair_probabilities(
        self,
        event: EventMarket,
        books: dict[str, BucketBook],
    ) -> dict[str, Decimal]:
        """Derive normalized, risk-neutral probabilities from current book midpoints."""
        mids: dict[str, Decimal] = {}
        for b in event.buckets:
            tok = b.yes_token_id
            book = books.get(tok)
            if book and book.mid_price is not None:
                mids[tok] = book.mid_price
            else:
                mids[tok] = Decimal("0.01")  # small fallback prior

        total_mid = sum(mids.values(), Decimal("0"))
        if total_mid <= Decimal("0"):
            num_buckets = Decimal(str(len(event.buckets)))
            return {b.yes_token_id: Decimal("1.0") / num_buckets for b in event.buckets}

        # Normalize so sum equals exactly 1.0000
        probs = {tok: (mid / total_mid).quantize(Decimal("0.0001")) for tok, mid in mids.items()}
        return probs

    def generate_maker_plan(
        self,
        event: EventMarket,
        books: dict[str, BucketBook],
    ) -> MakerPlan | None:
        """Generate a complete, structurally coherent two-sided quoting plan for the event."""
        if not event.buckets or len(event.buckets) < 2:
            return None

        probs = self.compute_fair_probabilities(event, books)
        half_spread = (self.target_spread / Decimal("2")).quantize(Decimal("0.001"))

        quotes: list[MakerQuote] = []
        sum_bid = Decimal("0")
        sum_ask = Decimal("0")

        for bucket in event.buckets:
            tok = bucket.yes_token_id
            fair = probs.get(tok, Decimal("0.05"))

            # Calculate raw bid and ask
            raw_bid = fair - half_spread
            raw_ask = fair + half_spread

            # Clip to bounds and round to tick size
            bid_price = max(self.min_quote_price, min(raw_bid, self.max_quote_price - self.tick_size))
            ask_price = min(self.max_quote_price, max(raw_ask, bid_price + self.tick_size))

            # Quantize to tick size (e.g. 0.01)
            bid_price = (bid_price / self.tick_size).quantize(Decimal("1")) * self.tick_size
            ask_price = (ask_price / self.tick_size).quantize(Decimal("1")) * self.tick_size

            if ask_price <= bid_price:
                ask_price = bid_price + self.tick_size

            # Size calculation
            bid_size = (self.quote_size_usdc / bid_price).quantize(Decimal("1")) if bid_price > 0 else Decimal("0")
            ask_size = (self.quote_size_usdc / ask_price).quantize(Decimal("1")) if ask_price > 0 else Decimal("0")

            spread = ask_price - bid_price
            spread_pct = ((spread / fair) * Decimal("100")).quantize(Decimal("0.1")) if fair > 0 else Decimal("0")

            quotes.append(
                MakerQuote(
                    token_id=tok,
                    label=bucket.label,
                    fair_prob=fair,
                    bid_price=bid_price,
                    bid_size=bid_size,
                    ask_price=ask_price,
                    ask_size=ask_size,
                    spread=spread,
                    spread_pct=spread_pct,
                )
            )
            sum_bid += bid_price
            sum_ask += ask_price

        # Invariant checks
        # For true maker safety: sum_bid MUST be < 1.00 (buying all is sub-par)
        # and sum_ask MUST be > 1.00 (selling all is above-par)
        is_safe = (sum_bid < Decimal("1.00")) and (sum_ask > Decimal("1.00"))
        maker_spread = sum_ask - sum_bid

        return MakerPlan(
            event_id=event.event_id,
            event_slug=event.event_slug,
            title=event.title,
            quotes=tuple(quotes),
            sum_bid=sum_bid,
            sum_ask=sum_ask,
            maker_spread=maker_spread,
            timestamp=time.time(),
            is_structurally_safe=is_safe,
        )
