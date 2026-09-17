"""Unified Orchestrator Engine for Neg-Risk Convexity Arbitrage & Market Making."""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from neg_risk.arb_strategy import NegRiskArbitrageEngine
from neg_risk.maker_strategy import NegRiskMarketMaker
from neg_risk.models import BasketArbOpportunity, EventMarket, MakerPlan
from neg_risk.scanner import NegRiskScanner

logger = logging.getLogger("twinPreYes.neg_risk")


class NegRiskEngine:
    def __init__(
        self,
        *,
        min_arb_profit_pct: Decimal = Decimal("0.02"),
        target_maker_spread: Decimal = Decimal("0.04"),
        max_arb_position_usdc: Decimal = Decimal("25.0"),
        quote_size_usdc: Decimal = Decimal("5.0"),
        scanner_timeout: float = 6.0,
    ) -> None:
        self.scanner = NegRiskScanner(timeout_seconds=scanner_timeout)
        self.arb_engine = NegRiskArbitrageEngine(
            min_profit_pct=min_arb_profit_pct,
            max_position_usdc=max_arb_position_usdc,
        )
        self.maker_engine = NegRiskMarketMaker(
            target_spread=target_maker_spread,
            quote_size_usdc=quote_size_usdc,
        )

    def scan_and_evaluate(
        self,
        *,
        weather_rules: list[dict[str, Any]] | None = None,
        gamma_events_limit: int = 30,
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

        # 4. Evaluate Arbitrage Opportunities
        arb_opportunities = self.arb_engine.scan_all_events(unique_events, books)

        # 5. Evaluate Maker Plans
        maker_plans: list[MakerPlan] = []
        for ev in unique_events:
            plan = self.maker_engine.generate_maker_plan(ev, books)
            if plan is not None and plan.is_structurally_safe:
                maker_plans.append(plan)

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
        }
