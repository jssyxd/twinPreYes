"""Unified Orchestrator Engine for Neg-Risk Convexity Arbitrage & Market Making.
Incorporates Jane Street High-Frequency & Arbitrage Constraints:
- Phantom Arb Filter (latency skew & book staleness)
- True Hurdle Rate (5% net margin)
- Bottleneck-First Sequencing
- Auto-Unwind State Machine
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from neg_risk.arb_strategy import NegRiskArbitrageEngine
from neg_risk.execution import BasketExecutionEngine
from neg_risk.maker_strategy import NegRiskMarketMaker
from neg_risk.models import BasketArbOpportunity, BasketExecutionReport, EventMarket, MakerPlan
from neg_risk.scanner import NegRiskScanner

logger = logging.getLogger("twinPreYes.neg_risk")


class NegRiskEngine:
    def __init__(
        self,
        *,
        min_arb_profit_pct: Decimal = Decimal("0.05"),  # 5% minimum net hurdle
        target_maker_spread: Decimal = Decimal("0.04"),
        max_arb_position_usdc: Decimal = Decimal("25.0"),
        quote_size_usdc: Decimal = Decimal("5.0"),
        scanner_timeout: float = 6.0,
        max_book_skew_seconds: float = 0.300,
        probe_first: bool = True,
        dry_run: bool = True,
    ) -> None:
        self.scanner = NegRiskScanner(timeout_seconds=scanner_timeout)
        self.arb_engine = NegRiskArbitrageEngine(
            min_profit_pct=min_arb_profit_pct,
            max_position_usdc=max_arb_position_usdc,
            max_book_skew_seconds=max_book_skew_seconds,
        )
        self.maker_engine = NegRiskMarketMaker(
            target_spread=target_maker_spread,
            quote_size_usdc=quote_size_usdc,
        )
        self.execution_engine = BasketExecutionEngine(
            probe_first=probe_first,
            dry_run=dry_run,
        )

    def scan_and_evaluate(
        self,
        *,
        weather_rules: list[dict[str, Any]] | None = None,
        gamma_events_limit: int = 30,
        execute_top_arb: bool = False,
    ) -> dict[str, Any]:
        """Perform a single comprehensive scan for both Arbitrage and Market Making opportunities."""
        started_at = time.time()

        # 1. Discover events
        events: list[EventMarket] = []
        if weather_rules:
            events.extend(self.scanner.scan_weather_rules_as_events(weather_rules))

        gamma_events = self.scanner.scan_active_events_from_gamma(limit=gamma_events_limit)
        events.extend(gamma_events)

        # De-duplicate events by slug
        seen_slugs = set()
        unique_events: list[EventMarket] = []
        for ev in events:
            if ev.event_slug not in seen_slugs:
                seen_slugs.add(ev.event_slug)
                unique_events.append(ev)

        # 2. Gather all required token IDs
        all_tokens: set[str] = set()
        for ev in unique_events:
            for b in ev.buckets:
                all_tokens.add(b.yes_token_id)

        # 3. Fetch orderbooks concurrently
        books = self.scanner.fetch_orderbooks(all_tokens)

        # 4. Evaluate Arbitrage Opportunities (with skew and hurdle filters)
        arb_opportunities = self.arb_engine.scan_all_events(unique_events, books, now=time.time())

        # 5. Evaluate Maker Plans
        maker_plans: list[MakerPlan] = []
        for ev in unique_events:
            plan = self.maker_engine.generate_maker_plan(ev, books)
            if plan is not None and plan.is_structurally_safe:
                maker_plans.append(plan)

        # 6. Optional Execution of top arb opportunity with Auto-Unwind Guard
        execution_report: BasketExecutionReport | None = None
        if execute_top_arb and arb_opportunities:
            top_opp = arb_opportunities[0]
            execution_report = self.execution_engine.execute_opportunity(top_opp, books)

        duration = round(time.time() - started_at, 2)
        return {
            "timestamp": time.time(),
            "scan_duration_seconds": duration,
            "events_scanned": len(unique_events),
            "orderbooks_fetched": len(books),
            "arb_opportunities_count": len(arb_opportunities),
            "arb_opportunities": arb_opportunities,
            "maker_plans_count": len(maker_plans),
            "maker_plans": maker_plans,
            "execution_report": execution_report,
        }
