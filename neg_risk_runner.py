#!/usr/bin/env python3
"""CLI daemon runner for Neg-Risk Convexity Arbitrage & Market Making Engine.

Features:
- Mode: Paper (default, 200.0 USDC initial, 20.0 USDC/basket) or Live
- Guards: Jane Street-grade latency skew check (<300ms) + 5% profit hurdle
- Auto Paper Execution: opens guaranteed complete-set arbitrage baskets
- Auto Settlement: polls resolution and settles complete sets to cash
- Health state: writes data/negrisk_health.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import time
from decimal import Decimal

from neg_risk.engine import NegRiskEngine
from neg_risk.paper_account import NegRiskPaperAccount

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("negrisk_runner")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Neg-Risk Arbitrage & Maker Engine")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper", help="Execution mode (default: paper)")
    parser.add_argument("--initial-capital", type=float, default=200.0, help="Paper initial capital (default: 200.0 USDC)")
    parser.add_argument("--budget", type=float, default=20.0, help="Per-basket budget in USDC (default: 20.0 USDC)")
    parser.add_argument("--interval", type=int, default=20, help="Scan interval in seconds (default: 20s)")
    parser.add_argument("--min-profit-pct", type=float, default=5.0, help="Min arb net profit pct hurdle (default: 5.0 pct)")
    parser.add_argument("--target-spread", type=float, default=0.04, help="Target maker spread in cents (default: 0.04)")
    parser.add_argument("--events-limit", type=int, default=30, help="Number of Gamma events to scan (default: 30)")
    parser.add_argument("--max-skew-ms", type=float, default=300.0, help="Max latency skew across books (default: 300ms)")
    parser.add_argument("--state-file", default="data/negrisk_state.json", help="Path to state file")
    parser.add_argument("--events-file", default="data/negrisk_events.jsonl", help="Path to events JSONL log")
    parser.add_argument("--health-file", default="data/negrisk_health.json", help="Path to health JSON snapshot")
    args = parser.parse_args()

    stop_flag = False

    def handle_signal(sig, frame):
        nonlocal stop_flag
        logger.info(f"Received termination signal {sig}. Initiating graceful shutdown...")
        stop_flag = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    paper_account = NegRiskPaperAccount(
        initial_capital=args.initial_capital,
        budget_per_order=args.budget,
        state_file=args.state_file,
        events_file=args.events_file,
    )

    engine = NegRiskEngine(
        min_arb_profit_pct=Decimal(str(args.min_profit_pct / 100.0)),
        target_maker_spread=Decimal(str(args.target_spread)),
        max_book_skew_seconds=args.max_skew_ms / 1000.0,
        probe_first=True,
        dry_run=(args.mode == "paper"),
    )

    logger.info(f"=== Polymarket Neg-Risk Engine [{args.mode.upper()}] Initialized ===")
    logger.info(
        f"Config: Initial={args.initial_capital}U, Budget/Order={args.budget}U, "
        f"Hurdle={args.min_profit_pct}%, MaxSkew={args.max_skew_ms}ms, Interval={args.interval}s"
    )
    logger.info(f"Current Cash: {paper_account.cash_balance} USDC | Equity: {paper_account.get_summary()['total_equity_usdc']} USDC")

    cycle_count = 0
    while not stop_flag:
        cycle_count += 1
        started_at = time.time()
        try:
            # 1. Check & settle existing open baskets
            settled = paper_account.check_and_settle_events()
            if settled > 0:
                logger.info(f"Cycle {cycle_count}: Settled {settled} completed event basket(s).")

            # 2. Scan market opportunities
            result = engine.scan_and_evaluate(gamma_events_limit=args.events_limit)
            duration = result["scan_duration_seconds"]
            arbs = result["arb_opportunities"]
            makers = result["maker_plans"]

            # 3. If paper mode and opportunities exist, open baskets
            new_opens = 0
            if args.mode == "paper" and arbs:
                for opp in arbs:
                    opened = paper_account.open_arbitrage_basket(opp)
                    if opened:
                        new_opens += 1

            summary = paper_account.get_summary()

            # 4. Write health status
            health_data = {
                "status": "healthy",
                "mode": args.mode,
                "cycle": cycle_count,
                "ts_epoch": time.time(),
                "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scan_duration_seconds": duration,
                "events_scanned": result["events_scanned"],
                "arb_opportunities_found": len(arbs),
                "maker_plans_found": len(makers),
                "cash_balance_usdc": summary["cash_balance"],
                "open_positions_count": summary["open_positions_count"],
                "open_cost_usdc": summary["open_cost_usdc"],
                "total_equity_usdc": summary["total_equity_usdc"],
                "realized_pnl_usdc": summary["realized_pnl_usdc"],
                "total_trades": summary["total_trades"],
                "open_positions": summary["open_positions"],
            }
            os.makedirs(os.path.dirname(os.path.abspath(args.health_file)), exist_ok=True)
            dirname = os.path.dirname(os.path.abspath(args.health_file))
            fd, tmp = tempfile.mkstemp(dir=dirname, prefix="negrisk_health_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(health_data, f, indent=2)
            os.replace(tmp, args.health_file)

            # Log periodic status
            logger.info(
                f"Cycle {cycle_count}: Scanned {result['events_scanned']} events in {duration}s | "
                f"Arb: {len(arbs)} (NewOpens={new_opens}) | MakerPlans: {len(makers)} | "
                f"Cash={summary['cash_balance']}U | Equity={summary['total_equity_usdc']}U | PnL={summary['realized_pnl_usdc']}U"
            )

        except Exception as exc:
            logger.error(f"Cycle {cycle_count} error: {exc}", exc_info=True)

        elapsed = time.time() - started_at
        sleep_time = max(1.0, args.interval - elapsed)
        # Sleep in short increments to respond to signals promptly
        for _ in range(int(sleep_time * 2)):
            if stop_flag:
                break
            time.sleep(0.5)

    logger.info("Neg-Risk Engine stopped gracefully.")


if __name__ == "__main__":
    main()
