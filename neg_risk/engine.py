"""Unified Orchestrator Engine for Neg-Risk Convexity Arbitrage & Market Making.
Incorporates Jane Street High-Frequency & Arbitrage Constraints:
- Per-event pipelined scanning (eliminating batch latency skew)
- Dynamic Arbitrage Hurdle (default 1.5% net guaranteed margin)
- Bottleneck-First Sequencing & Auto-Unwind
- Active Two-Sided Maker Spread & Inventory Engine
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from neg_risk.arb_strategy import NegRiskArbitrageEngine
from neg_risk.execution import BasketExecutionEngine
from neg_risk.maker_strategy import NegRiskMarketMaker
from neg_risk.models import BasketArbOpportunity, BasketExecutionReport, BucketBook, EventMarket, MakerPlan
from neg_risk.paper_account import NegRiskPaperAccount
from neg_risk.scanner import NegRiskScanner

logger = logging.getLogger("twinPreYes.neg_risk")


class NegRiskEngine:
    def __init__(
        self,
        *,
        min_arb_profit_pct: Decimal | float = Decimal("0.015"),  # 1.5% net profit hurdle
        target_maker_spread: Decimal | float = Decimal("0.03"),  # 3¢ tight two-sided spread
        max_arb_position_usdc: Decimal | float = Decimal("20.0"),
        quote_size_usdc: Decimal | float = Decimal("5.0"),
        scanner_timeout: float = 6.0,
        max_book_skew_seconds: float = 0.300,
        probe_first: bool = True,
        dry_run: bool = True,
    ) -> None:
        self.scanner = NegRiskScanner(timeout_seconds=scanner_timeout)
        self.arb_engine = NegRiskArbitrageEngine(
            min_profit_pct=Decimal(str(min_arb_profit_pct)),
            max_position_usdc=Decimal(str(max_arb_position_usdc)),
            max_book_skew_seconds=max_book_skew_seconds,
        )
        self.maker_engine = NegRiskMarketMaker(
            target_spread=Decimal(str(target_maker_spread)),
            quote_size_usdc=Decimal(str(quote_size_usdc)),
        )
        self.execution_engine = BasketExecutionEngine(
            probe_first=probe_first,
            dry_run=dry_run,
        )

    def scan_and_evaluate_pipelined(
        self,
        *,
        gamma_events_limit: int = 30,
        paper_account: NegRiskPaperAccount | None = None,
    ) -> dict[str, Any]:
        """Pipelined scanner: Fetches orderbooks and evaluates per event to eliminate batch latency skew."""
        started_at = time.time()

        # 1. Discover events
        gamma_events = self.scanner.scan_active_events_from_gamma(
            limit=gamma_events_limit,
            require_closed_mece=True,
        )

        seen_slugs = set()
        unique_events: list[EventMarket] = []
        for ev in gamma_events:
            if ev.event_slug not in seen_slugs:
                seen_slugs.add(ev.event_slug)
                unique_events.append(ev)

        arb_opportunities: list[BasketArbOpportunity] = []
        maker_plans: list[MakerPlan] = []
        total_books_fetched = 0
        new_arb_opens = 0
        new_maker_fills = 0

        # 2. Pipelined execution: Process event-by-event
        for ev in unique_events:
            event_tokens = [b.yes_token_id for b in ev.buckets]
            event_books = self.scanner.fetch_orderbooks(event_tokens)
            total_books_fetched += len(event_books)

            if len(event_books) < len(ev.buckets):
                continue

            # Evaluate Arbitrage immediately after fetching this event's fresh books
            opp_long = self.arb_engine.evaluate_long_basket(ev, event_books, now=time.time())
            if opp_long is not None:
                arb_opportunities.append(opp_long)
                if paper_account is not None:
                    if paper_account.open_arbitrage_basket(opp_long):
                        new_arb_opens += 1
            else:
                opp_short = self.arb_engine.evaluate_short_basket(ev, event_books, now=time.time())
                if opp_short is not None:
                    arb_opportunities.append(opp_short)
                    if paper_account is not None:
                        if paper_account.open_arbitrage_basket(opp_short):
                            new_arb_opens += 1

            # Evaluate Maker Plan & Execute Active Maker Quoting Fills
            plan = self.maker_engine.generate_maker_plan(ev, event_books)
            if plan is not None and plan.is_structurally_safe:
                maker_plans.append(plan)
                if paper_account is not None:
                    fills = paper_account.process_maker_quotes(plan, event_books)
                    new_maker_fills += fills

        duration = round(time.time() - started_at, 2)
        return {
            "timestamp": time.time(),
            "scan_duration_seconds": duration,
            "events_scanned": len(unique_events),
            "orderbooks_fetched": total_books_fetched,
            "arb_opportunities_count": len(arb_opportunities),
            "arb_opportunities": arb_opportunities,
            "maker_plans_count": len(maker_plans),
            "maker_plans": maker_plans,
            "new_arb_opens": new_arb_opens,
            "new_maker_fills": new_maker_fills,
        }

    def scan_and_evaluate(
        self,
        *,
        weather_rules: list[dict[str, Any]] | None = None,
        gamma_events_limit: int = 30,
        execute_top_arb: bool = False,
    ) -> dict[str, Any]:
        """Legacy batch scan interface maintained for backward compatibility."""
        return self.scan_and_evaluate_pipelined(gamma_events_limit=gamma_events_limit)
