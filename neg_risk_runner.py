#!/usr/bin/env python3
"""CLI runner for Neg-Risk Convexity Arbitrage & Market Making Scanner."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from decimal import Decimal

from neg_risk.engine import NegRiskEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("neg_risk_cli")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Neg-Risk Convexity Arbitrage & Maker Engine")
    parser.add_argument("--loop", action="store_true", help="Run in continuous monitoring loop")
    parser.add_argument("--interval", type=int, default=15, help="Scan interval in seconds (default: 15)")
    parser.add_argument("--min-profit-pct", type=float, default=2.0, help="Min arb net profit % (default: 2.0%)")
    parser.add_argument("--target-spread", type=float, default=0.04, help="Target maker spread in cents (default: 0.04)")
    parser.add_argument("--events-limit", type=int, default=25, help="Number of Gamma events to scan (default: 25)")
    args = parser.parse_args()

    engine = NegRiskEngine(
        min_arb_profit_pct=Decimal(str(args.min_profit_pct / 100.0)),
        target_maker_spread=Decimal(str(args.target_spread)),
    )

    logger.info("Starting Neg-Risk Arbitrage & Maker Scanner...")
    logger.info(f"Parameters: MinArbProfit={args.min_profit_pct}%, TargetMakerSpread={args.target_spread}, Interval={args.interval}s")

    while True:
        try:
            logger.info("Scanning Polymarket multi-outcome orderbooks...")
            result = engine.scan_and_evaluate(gamma_events_limit=args.events_limit)
            logger.info(
                f"Scanned {result['events_scanned']} events ({result['orderbooks_fetched']} books) in {result['scan_duration_seconds']}s"
            )

            # Arbitrage display
            arbs = result["arb_opportunities"]
            if arbs:
                logger.info(f"🎯 FOUND {len(arbs)} NEG-RISK ARBITRAGE OPPORTUNITY(IES):")
                for opp in arbs:
                    logger.info(
                        f"  [{opp.arb_type}] {opp.title} | SumPrice={opp.sum_price} | "
                        f"Cost={opp.total_cost_usdc}U -> Payout={opp.expected_payout_usdc}U | "
                        f"NetProfit=+{opp.net_profit_usdc}U (ROI: +{opp.roi_percent}%)"
                    )
            else:
                logger.info("  No cross-book mispricings exceeding hurdle at this moment.")

            # Maker display (Top 3 sample quotes)
            maker_plans = result["maker_plans"]
            if maker_plans:
                logger.info(f"📊 Evaluated {len(maker_plans)} coherent two-sided Maker Plans:")
                for plan in maker_plans[:2]:
                    logger.info(
                        f"  [MAKER] {plan.title} | SumBid={plan.sum_bid} (Must <1.0) | "
                        f"SumAsk={plan.sum_ask} (Must >1.0) | MakerSpread={plan.maker_spread}"
                    )

        except KeyboardInterrupt:
            logger.info("Terminating scanner by operator request.")
            break
        except Exception as exc:
            logger.error(f"Error during scan loop: {exc}", exc_info=True)

        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
