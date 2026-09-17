"""Unit tests for Polymarket Neg-Risk Convexity Arbitrage and Market Maker Strategies."""
from __future__ import annotations

import unittest
from decimal import Decimal

from neg_risk.models import (
    BookLevel,
    BucketBook,
    EventMarket,
    OutcomeBucket,
)
from neg_risk.arb_strategy import NegRiskArbitrageEngine
from neg_risk.maker_strategy import NegRiskMarketMaker


class TestNegRiskStrategies(unittest.TestCase):
    def setUp(self) -> None:
        self.buckets = (
            OutcomeBucket(bucket_id="b1", label="<20°C", yes_token_id="tok_1"),
            OutcomeBucket(bucket_id="b2", label="20-22°C", yes_token_id="tok_2"),
            OutcomeBucket(bucket_id="b3", label="22-24°C", yes_token_id="tok_3"),
            OutcomeBucket(bucket_id="b4", label=">24°C", yes_token_id="tok_4"),
        )
        self.event = EventMarket(
            event_id="ev_paris",
            event_slug="paris-high-temp-sep-17",
            title="Paris High Temperature",
            buckets=self.buckets,
            neg_risk=True,
        )

    def test_long_basket_arbitrage_detected(self) -> None:
        """Test detection of Long Basket Arbitrage when SumAsk < 1.00 (e.g. SumAsk = 0.88)."""
        # Best Asks sum = 0.10 + 0.35 + 0.30 + 0.13 = 0.88 (< 0.98 hurdle)
        books = {
            "tok_1": BucketBook(
                token_id="tok_1", label="<20",
                best_bid=Decimal("0.08"), best_ask=Decimal("0.10"),
                bid_size=Decimal("100"), ask_size=Decimal("50")
            ),
            "tok_2": BucketBook(
                token_id="tok_2", label="20-22",
                best_bid=Decimal("0.32"), best_ask=Decimal("0.35"),
                bid_size=Decimal("100"), ask_size=Decimal("40")
            ),
            "tok_3": BucketBook(
                token_id="tok_3", label="22-24",
                best_bid=Decimal("0.28"), best_ask=Decimal("0.30"),
                bid_size=Decimal("100"), ask_size=Decimal("60")
            ),
            "tok_4": BucketBook(
                token_id="tok_4", label=">24",
                best_bid=Decimal("0.11"), best_ask=Decimal("0.13"),
                bid_size=Decimal("100"), ask_size=Decimal("80")
            ),
        }

        engine = NegRiskArbitrageEngine(min_profit_pct=Decimal("0.02"), max_position_usdc=Decimal("50.0"))
        opp = engine.evaluate_long_basket(self.event, books)

        self.assertIsNotNone(opp)
        self.assertEqual(opp.arb_type, "LONG_BASKET_BUY")
        self.assertEqual(opp.sum_price, Decimal("0.88"))
        self.assertEqual(opp.executable_shares, Decimal("40"))  # constrained by tok_2 size=40
        self.assertEqual(opp.total_cost_usdc, (Decimal("0.88") * Decimal("40")).quantize(Decimal("0.0001")))
        self.assertEqual(opp.expected_payout_usdc, Decimal("40.0000"))
        self.assertGreater(opp.net_profit_usdc, Decimal("0"))
        self.assertEqual(opp.roi_percent, Decimal("13.64"))  # (40 - 35.2) / 35.2 = 13.64%

    def test_long_basket_no_arbitrage_when_efficient(self) -> None:
        """When SumAsk >= 1.00 (e.g. SumAsk = 1.02), no arbitrage should trigger."""
        books = {
            "tok_1": BucketBook("tok_1", "<20", Decimal("0.10"), Decimal("0.12"), Decimal("100"), Decimal("50")),
            "tok_2": BucketBook("tok_2", "20-22", Decimal("0.38"), Decimal("0.40"), Decimal("100"), Decimal("50")),
            "tok_3": BucketBook("tok_3", "22-24", Decimal("0.33"), Decimal("0.35"), Decimal("100"), Decimal("50")),
            "tok_4": BucketBook("tok_4", ">24", Decimal("0.13"), Decimal("0.15"), Decimal("100"), Decimal("50")),
        }
        engine = NegRiskArbitrageEngine(min_profit_pct=Decimal("0.02"))
        opp = engine.evaluate_long_basket(self.event, books)
        self.assertIsNone(opp)

    def test_short_basket_arbitrage_detected(self) -> None:
        """Test detection of Short Basket Arbitrage when SumBid > 1.00 (e.g. SumBid = 1.06)."""
        # Best Bids sum = 0.15 + 0.42 + 0.35 + 0.14 = 1.06 (> 1.02 hurdle)
        books = {
            "tok_1": BucketBook("tok_1", "<20", Decimal("0.15"), Decimal("0.17"), Decimal("30"), Decimal("50")),
            "tok_2": BucketBook("tok_2", "20-22", Decimal("0.42"), Decimal("0.45"), Decimal("25"), Decimal("50")),
            "tok_3": BucketBook("tok_3", "22-24", Decimal("0.35"), Decimal("0.38"), Decimal("40"), Decimal("50")),
            "tok_4": BucketBook("tok_4", ">24", Decimal("0.14"), Decimal("0.16"), Decimal("35"), Decimal("50")),
        }
        engine = NegRiskArbitrageEngine(min_profit_pct=Decimal("0.02"))
        opp = engine.evaluate_short_basket(self.event, books)

        self.assertIsNotNone(opp)
        self.assertEqual(opp.arb_type, "SHORT_BASKET_SELL")
        self.assertEqual(opp.sum_price, Decimal("1.06"))
        self.assertEqual(opp.executable_shares, Decimal("25"))  # constrained by tok_2 size=25
        self.assertEqual(opp.expected_payout_usdc, Decimal("26.5000"))  # 1.06 * 25
        self.assertEqual(opp.total_cost_usdc, Decimal("25.0000"))  # liability is 1.00 * 25
        self.assertEqual(opp.net_profit_usdc, Decimal("1.5000"))
        self.assertEqual(opp.roi_percent, Decimal("6.00"))

    def test_market_maker_generates_coherent_two_sided_quotes(self) -> None:
        """Test market maker generates quotes satisfying SumBid < 1.00 and SumAsk > 1.00."""
        books = {
            "tok_1": BucketBook("tok_1", "<20", Decimal("0.09"), Decimal("0.11"), Decimal("100"), Decimal("50")),
            "tok_2": BucketBook("tok_2", "20-22", Decimal("0.39"), Decimal("0.41"), Decimal("100"), Decimal("50")),
            "tok_3": BucketBook("tok_3", "22-24", Decimal("0.29"), Decimal("0.31"), Decimal("100"), Decimal("50")),
            "tok_4": BucketBook("tok_4", ">24", Decimal("0.19"), Decimal("0.21"), Decimal("100"), Decimal("50")),
        }
        mm = NegRiskMarketMaker(target_spread=Decimal("0.04"), quote_size_usdc=Decimal("5.0"))
        plan = mm.generate_maker_plan(self.event, books)

        self.assertIsNotNone(plan)
        self.assertTrue(plan.is_structurally_safe)
        self.assertLess(plan.sum_bid, Decimal("1.00"))
        self.assertGreater(plan.sum_ask, Decimal("1.00"))
        self.assertEqual(len(plan.quotes), 4)

        for q in plan.quotes:
            self.assertGreater(q.ask_price, q.bid_price)
            self.assertGreater(q.bid_size, Decimal("0"))
            self.assertGreater(q.ask_size, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
