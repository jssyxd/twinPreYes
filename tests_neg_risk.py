"""Unit tests for Polymarket Neg-Risk Convexity Arbitrage, Market Maker, and Jane Street Execution Guards."""
from __future__ import annotations

import time
import unittest
from decimal import Decimal

from neg_risk.models import (
    BasketArbOpportunity,
    BucketBook,
    EventMarket,
    OutcomeBucket,
    LegStatus,
)
from neg_risk.arb_strategy import NegRiskArbitrageEngine
from neg_risk.maker_strategy import NegRiskMarketMaker
from neg_risk.execution import BasketExecutionEngine
from neg_risk.paper_account import NegRiskPaperAccount


class TestNegRiskStrategies(unittest.TestCase):
    def setUp(self) -> None:
        self.now = time.time()
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

    def _create_fresh_books(self, skew_offset: float = 0.0) -> dict[str, BucketBook]:
        return {
            "tok_1": BucketBook(
                token_id="tok_1", label="<20",
                best_bid=Decimal("0.08"), best_ask=Decimal("0.10"),
                bid_size=Decimal("100"), ask_size=Decimal("50"),
                fetched_at=self.now,
            ),
            "tok_2": BucketBook(
                token_id="tok_2", label="20-22",
                best_bid=Decimal("0.32"), best_ask=Decimal("0.35"),
                bid_size=Decimal("100"), ask_size=Decimal("15"),  # Smallest depth = Bottleneck!
                fetched_at=self.now,
            ),
            "tok_3": BucketBook(
                token_id="tok_3", label="22-24",
                best_bid=Decimal("0.28"), best_ask=Decimal("0.30"),
                bid_size=Decimal("100"), ask_size=Decimal("60"),
                fetched_at=self.now,
            ),
            "tok_4": BucketBook(
                token_id="tok_4", label=">24",
                best_bid=Decimal("0.11"), best_ask=Decimal("0.13"),
                bid_size=Decimal("100"), ask_size=Decimal("80"),
                fetched_at=self.now - skew_offset,
            ),
        }

    def test_long_basket_arbitrage_with_bottleneck_first(self) -> None:
        """Test long basket arbitrage detects opportunity and places bottleneck leg first."""
        books = self._create_fresh_books()
        engine = NegRiskArbitrageEngine(min_profit_pct=Decimal("0.05"), max_position_usdc=Decimal("50.0"))
        opp = engine.evaluate_long_basket(self.event, books, now=self.now)

        self.assertIsNotNone(opp)
        self.assertEqual(opp.arb_type, "LONG_BASKET_BUY")
        self.assertEqual(opp.sum_price, Decimal("0.88"))  # 0.10 + 0.35 + 0.30 + 0.13 = 0.88
        self.assertEqual(opp.executable_shares, Decimal("15"))  # constrained by tok_2 size=15

        # Jane Street constraint: Bottleneck leg MUST be at Index 0
        self.assertEqual(opp.legs[0].token_id, "tok_2")
        self.assertTrue(opp.legs[0].is_bottleneck)
        self.assertEqual(opp.legs[0].available_depth, Decimal("15"))

    def test_phantom_arbitrage_rejected_due_to_latency_skew(self) -> None:
        """Jane Street rule: If latency skew across books > 300ms, opportunity must be rejected."""
        books = self._create_fresh_books(skew_offset=0.450)
        engine = NegRiskArbitrageEngine(
            min_profit_pct=Decimal("0.05"),
            max_book_skew_seconds=0.300,
        )
        opp = engine.evaluate_long_basket(self.event, books, now=self.now)
        self.assertIsNone(opp, "Arbitrage with >300ms skew must be rejected as phantom arb")

    def test_stale_book_rejected_due_to_age(self) -> None:
        """Jane Street rule: If book age > 5.0s, opportunity must be rejected."""
        books = self._create_fresh_books()
        engine = NegRiskArbitrageEngine(max_book_age_seconds=5.0)
        opp = engine.evaluate_long_basket(self.event, books, now=self.now + 6.0)
        self.assertIsNone(opp, "Arbitrage with stale book >5.0s must be rejected")

    def test_hurdle_rate_filter(self) -> None:
        """Hurdle rate of 5% must reject small 3% margins."""
        books = {
            "tok_1": BucketBook("tok_1", "<20", Decimal("0.10"), Decimal("0.12"), Decimal("100"), Decimal("50"), fetched_at=self.now),
            "tok_2": BucketBook("tok_2", "20-22", Decimal("0.38"), Decimal("0.40"), Decimal("100"), Decimal("50"), fetched_at=self.now),
            "tok_3": BucketBook("tok_3", "22-24", Decimal("0.30"), Decimal("0.32"), Decimal("100"), Decimal("50"), fetched_at=self.now),
            "tok_4": BucketBook("tok_4", ">24", Decimal("0.11"), Decimal("0.13"), Decimal("100"), Decimal("50"), fetched_at=self.now),
        }
        engine = NegRiskArbitrageEngine(min_profit_pct=Decimal("0.05"))
        opp = engine.evaluate_long_basket(self.event, books, now=self.now)
        self.assertIsNone(opp, "3% profit must be rejected when hurdle is 5%")

    def test_auto_unwind_on_partial_fill(self) -> None:
        """Jane Street rule: If partial fill occurs, filled legs must be aggressively unwound."""
        books = self._create_fresh_books()
        engine = NegRiskArbitrageEngine(min_profit_pct=Decimal("0.05"))
        opp = engine.evaluate_long_basket(self.event, books, now=self.now)
        self.assertIsNotNone(opp)

        exec_engine = BasketExecutionEngine(probe_first=True, dry_run=False)

        def mock_submit(leg, limit_px):
            if leg.token_id == "tok_2":
                return {"filled": True, "price": leg.price, "size": leg.size}
            elif leg.token_id == "tok_1":
                return {"filled": False, "error": "Order rejected by CLOB"}
            return {"filled": True, "price": leg.price, "size": leg.size}

        report = exec_engine.execute_opportunity(opp, books, submit_order_fn=mock_submit)

        self.assertEqual(report.status, "UNWOUND")
        self.assertEqual(report.legs_unwound, 1)  # Only tok_2 was filled
        self.assertIn("Aggressively unwound", report.unwind_notes)
        self.assertEqual(report.net_pnl_usdc, Decimal("-0.4500"))

    def test_probe_first_aborted_zero_exposure(self) -> None:
        """When probe leg (Index 0) fails, remaining legs are never touched."""
        books = self._create_fresh_books()
        engine = NegRiskArbitrageEngine(min_profit_pct=Decimal("0.05"))
        opp = engine.evaluate_long_basket(self.event, books, now=self.now)
        self.assertIsNotNone(opp)

        exec_engine = BasketExecutionEngine(probe_first=True, dry_run=False)

        calls: list[str] = []
        def mock_submit(leg, limit_px):
            calls.append(leg.token_id)
            return {"filled": False, "error": "Probe order rejected"}

        report = exec_engine.execute_opportunity(opp, books, submit_order_fn=mock_submit)

        self.assertEqual(report.status, "PROBE_ABORTED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], "tok_2")
        self.assertEqual(report.total_spent_usdc, Decimal("0"))
        self.assertEqual(report.net_pnl_usdc, Decimal("0"))

    def test_market_maker_fills_and_spread_capture(self) -> None:
        """Test market maker two-sided spread profit capture upon order crossing."""
        # Our quotes: Fair=0.25, Bid=0.23, Ask=0.27 (Spread=0.04)
        # Market orderbook crosses: Market Ask=0.22 (Taker hits our bid), Market Bid=0.28 (Taker lifts our ask)
        books = {
            "tok_1": BucketBook("tok_1", "<20", Decimal("0.28"), Decimal("0.22"), Decimal("100"), Decimal("50"), fetched_at=self.now),
            "tok_2": BucketBook("tok_2", "20-22", Decimal("0.24"), Decimal("0.26"), Decimal("100"), Decimal("50"), fetched_at=self.now),
            "tok_3": BucketBook("tok_3", "22-24", Decimal("0.24"), Decimal("0.26"), Decimal("100"), Decimal("50"), fetched_at=self.now),
            "tok_4": BucketBook("tok_4", ">24", Decimal("0.24"), Decimal("0.26"), Decimal("100"), Decimal("50"), fetched_at=self.now),
        }
        mm = NegRiskMarketMaker(target_spread=Decimal("0.04"), quote_size_usdc=Decimal("5.0"))
        plan = mm.generate_maker_plan(self.event, books)

        self.assertIsNotNone(plan)
        self.assertTrue(plan.is_structurally_safe)

        account = NegRiskPaperAccount(initial_capital=200.0, state_file="/tmp/test_mm_state.json", events_file="/tmp/test_mm_events.jsonl")
        fills = account.process_maker_plan_fills(plan, books)
        self.assertGreater(fills, 0)
        self.assertGreater(account.realized_pnl, 0.0)
        self.assertGreater(account.cash_balance, 200.0)


if __name__ == "__main__":
    unittest.main()
