"""Paper Trading Account Ledger & Settlement Tracker for Neg-Risk Arbitrage & Maker Strategies.

Features:
- Initial Capital: 200.0 USDC (configurable)
- Per-Basket Budget: 20.0 USDC (configurable)
- Arbitrage Execution: Buys entire MECE Yes complete-set basket at SumAsk < 1.00.
- Maker Two-Sided Paper Fill Engine:
  Simulates passive fills when market orderbook best bid/ask crosses our quoted Maker spread.
  Captures bid-ask spread profits + records maker volume.
- Complete Set Settlement: When any event resolves on Polymarket, credits 1.00 * shares to cash.
- Persistent JSON State: Atomic saves to state file and events log.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from neg_risk.models import BasketArbOpportunity, BucketBook, MakerPlan

logger = logging.getLogger("twinPreYes.neg_risk.paper")
GAMMA_EVENT_ENDPOINT = "https://gamma-api.polymarket.com/events/slug/"


@dataclass
class PaperBasketPosition:
    position_id: str
    event_slug: str
    event_title: str
    strategy_type: str  # "ARBITRAGE" or "MAKER_TWO_SIDED"
    cost_usdc: float
    shares: float
    sum_price: float
    expected_payout_usdc: float
    expected_profit_usdc: float
    roi_percent: float
    legs: list[dict[str, Any]]
    opened_at_epoch: float
    status: str = "OPEN"  # OPEN, WON_SETTLED, UNWOUND, CLOSED
    realized_pnl: float = 0.0
    settled_at_epoch: float | None = None
    winning_token_id: str | None = None


class NegRiskPaperAccount:
    def __init__(
        self,
        *,
        initial_capital: float = 200.0,
        budget_per_order: float = 20.0,
        state_file: str = "data/negrisk_state.json",
        events_file: str = "data/negrisk_events.jsonl",
    ) -> None:
        self.initial_capital = initial_capital
        self.budget_per_order = budget_per_order
        self.state_file = state_file
        self.events_file = events_file

        self.cash_balance = initial_capital
        self.realized_pnl = 0.0
        self.total_trades = 0
        self.maker_trades = 0
        self.arb_trades = 0
        self.positions: dict[str, PaperBasketPosition] = {}

        os.makedirs(os.path.dirname(os.path.abspath(self.state_file)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.events_file)), exist_ok=True)
        self.load_state()

    def load_state(self) -> None:
        if not os.path.exists(self.state_file):
            self.save_state()
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.cash_balance = float(data.get("cash_balance", self.initial_capital))
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.total_trades = int(data.get("total_trades", 0))
            self.maker_trades = int(data.get("maker_trades", 0))
            self.arb_trades = int(data.get("arb_trades", 0))
            raw_pos = data.get("positions", {})
            self.positions = {}
            for pid, pdata in raw_pos.items():
                self.positions[pid] = PaperBasketPosition(**pdata)
        except Exception as exc:
            logger.error(f"Failed to load paper state from {self.state_file}: {exc}")

    def save_state(self) -> None:
        data = {
            "initial_capital": self.initial_capital,
            "cash_balance": round(self.cash_balance, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "total_trades": self.total_trades,
            "maker_trades": self.maker_trades,
            "arb_trades": self.arb_trades,
            "open_positions_count": sum(1 for p in self.positions.values() if p.status == "OPEN"),
            "open_cost_usdc": round(sum(p.cost_usdc for p in self.positions.values() if p.status == "OPEN"), 4),
            "total_equity_usdc": round(
                self.cash_balance + sum(p.cost_usdc for p in self.positions.values() if p.status == "OPEN"), 4
            ),
            "positions": {pid: asdict(p) for pid, p in self.positions.items()},
            "updated_at": time.time(),
        }
        dirname = os.path.dirname(os.path.abspath(self.state_file))
        fd, tmp = tempfile.mkstemp(dir=dirname, prefix="negrisk_state_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.state_file)

    def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        entry = {
            "type": event_type,
            "ts": time.time(),
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **payload,
        }
        with open(self.events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def open_arbitrage_basket(self, opp: BasketArbOpportunity) -> bool:
        """Open a paper basket arbitrage position within budget."""
        for p in self.positions.values():
            if p.event_slug == opp.event_slug and p.status == "OPEN":
                return False

        if self.cash_balance < 5.0:
            return False

        budget = min(float(self.budget_per_order), self.cash_balance)
        sum_px = float(opp.sum_price)
        if sum_px <= 0.0:
            return False

        # Strictly verify positive net profit hurdle
        if opp.arb_type == "LONG_BASKET_BUY":
            if sum_px >= 1.00:
                return False
            shares = round(budget / sum_px, 4)
            actual_cost = round(shares * sum_px, 4)
            expected_payout = round(shares * 1.00, 4)
            expected_profit = round(expected_payout - actual_cost, 4)
        else:  # SHORT_BASKET_SELL
            if sum_px <= 1.00:
                return False
            shares = round(budget / 1.00, 4)
            actual_cost = round(shares * 1.00, 4)  # Maximum liability
            expected_payout = round(shares * sum_px, 4)  # Premium collected
            expected_profit = round(expected_payout - actual_cost, 4)

        if expected_profit <= 0.0 or actual_cost <= 0.0:
            return False

        roi = round((expected_profit / actual_cost) * 100.0, 2)

        pos_id = f"arb-{opp.event_slug}-{int(time.time())}"
        legs_data = [
            {
                "token_id": leg.token_id,
                "label": leg.label,
                "side": leg.side,
                "price": float(leg.price),
                "size": shares,
                "cost": round(float(leg.price) * shares, 4),
                "is_bottleneck": leg.is_bottleneck,
            }
            for leg in opp.legs
        ]

        pos = PaperBasketPosition(
            position_id=pos_id,
            event_slug=opp.event_slug,
            event_title=opp.title,
            strategy_type="ARBITRAGE",
            cost_usdc=actual_cost,
            shares=shares,
            sum_price=sum_px,
            expected_payout_usdc=expected_payout,
            expected_profit_usdc=expected_profit,
            roi_percent=roi,
            legs=legs_data,
            opened_at_epoch=time.time(),
            status="OPEN",
        )

        self.cash_balance -= actual_cost
        self.total_trades += 1
        self.arb_trades += 1
        self.positions[pos_id] = pos
        self.save_state()

        self.log_event("paper_arb_open", {
            "position_id": pos_id,
            "event_slug": opp.event_slug,
            "title": opp.title,
            "cost_usdc": actual_cost,
            "shares": shares,
            "sum_price": sum_px,
            "expected_profit_usdc": expected_profit,
            "roi_percent": roi,
            "legs_count": len(legs_data),
            "cash_remaining": round(self.cash_balance, 4),
        })
        logger.info(
            f"🎯 [PAPER ARB OPEN] {opp.title} | Cost: {actual_cost}U ({shares} sh @ SumPx {sum_px}) -> "
            f"Expected Profit: +{expected_profit}U (ROI: +{roi}%) | Cash: {round(self.cash_balance, 2)}U"
        )
        return True

    def process_maker_plan_fills(
        self,
        plan: MakerPlan,
        books: dict[str, BucketBook],
    ) -> int:
        """Process passive Maker fills when external market crossing matches our quoted bids/asks.
        
        If a market order crosses our quote:
        - We buy Yes at Bid (< Fair) or sell Yes at Ask (> Fair).
        - If both sides of our quote or complete sets are filled across quotes, captures round-trip spread profit.
        """
        if not plan.is_structurally_safe or self.cash_balance < 5.0:
            return 0

        # Check existing active maker position on this event
        existing_pos_id = f"maker-{plan.event_slug}"
        pos = self.positions.get(existing_pos_id)

        fills_count = 0
        now = time.time()

        for q in plan.quotes:
            book = books.get(q.token_id)
            if not book:
                continue

            # Check if market best_ask <= our bid_price (Market Takers selling into our bid)
            bid_hit = (book.best_ask is not None and book.best_ask <= q.bid_price)
            # Check if market best_bid >= our ask_price (Market Takers buying from our ask)
            ask_lift = (book.best_bid is not None and book.best_bid >= q.ask_price)

            if bid_hit and ask_lift:
                # Two-way round-trip capture! Pure bid-ask spread profit
                traded_shares = min(float(q.bid_size), float(q.ask_size), 50.0)
                spread_captured = float(q.spread) * traded_shares
                profit = round(spread_captured, 4)

                self.cash_balance += profit
                self.realized_pnl += profit
                self.total_trades += 1
                self.maker_trades += 1
                fills_count += 1

                self.log_event("paper_maker_roundtrip_fill", {
                    "event_slug": plan.event_slug,
                    "title": plan.title,
                    "token_id": q.token_id,
                    "label": q.label,
                    "bid_price": float(q.bid_price),
                    "ask_price": float(q.ask_price),
                    "shares": traded_shares,
                    "spread_profit_usdc": profit,
                    "cash_balance": round(self.cash_balance, 4),
                })
                logger.info(
                    f"💰 [MAKER SPREAD FILL] {plan.title} [{q.label}] | Traded: {traded_shares} sh @ "
                    f"[{q.bid_price}/{q.ask_price}] | Captured Spread Profit: +{profit} USDC | Cash: {round(self.cash_balance, 2)}U"
                )

        if fills_count > 0:
            self.save_state()
        return fills_count

    def check_and_settle_events(self) -> int:
        """Poll Gamma resolution status for open positions and settle complete sets."""
        settled_count = 0
        now = time.time()

        for pos in list(self.positions.values()):
            if pos.status != "OPEN":
                continue

            url = f"{GAMMA_EVENT_ENDPOINT}{pos.event_slug}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "twinPreYes-Settler/1.0"})
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    event_data = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue

            if not isinstance(event_data, dict):
                continue

            markets = event_data.get("markets") or []
            is_closed = event_data.get("closed") or all(m.get("closed") for m in markets if isinstance(m, dict))

            resolved_token = None
            for m in markets:
                if not isinstance(m, dict):
                    continue
                try:
                    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
                    clob_tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else (m.get("clobTokenIds") or [])
                    if len(prices) >= 2 and prices[0] == "1" and prices[1] == "0":
                        resolved_token = str(clob_tokens[0])
                        break
                except Exception:
                    continue

            if is_closed or resolved_token:
                payout = pos.expected_payout_usdc
                profit = payout - pos.cost_usdc
                pos.status = "WON_SETTLED"
                pos.realized_pnl = round(profit, 4)
                pos.settled_at_epoch = now
                pos.winning_token_id = resolved_token

                self.cash_balance += payout
                self.realized_pnl += profit
                settled_count += 1

                self.log_event("paper_arb_settled", {
                    "position_id": pos.position_id,
                    "event_slug": pos.event_slug,
                    "title": pos.event_title,
                    "cost_usdc": pos.cost_usdc,
                    "payout_usdc": payout,
                    "profit_usdc": round(profit, 4),
                    "roi_percent": pos.roi_percent,
                    "cash_balance": round(self.cash_balance, 4),
                })
                logger.info(
                    f"🏆 [PAPER ARB SETTLED] {pos.event_title} | Payout: {payout}U | "
                    f"Net Profit: +{round(profit, 4)}U | Cash Balance: {round(self.cash_balance, 2)}U"
                )

        if settled_count > 0:
            self.save_state()
        return settled_count

    def get_summary(self) -> dict[str, Any]:
        open_positions = [p for p in self.positions.values() if p.status == "OPEN"]
        open_cost = sum(p.cost_usdc for p in open_positions)
        total_equity = self.cash_balance + open_cost
        return {
            "initial_capital": self.initial_capital,
            "cash_balance": round(self.cash_balance, 4),
            "open_positions_count": len(open_positions),
            "open_cost_usdc": round(open_cost, 4),
            "total_equity_usdc": round(total_equity, 4),
            "realized_pnl_usdc": round(self.realized_pnl, 4),
            "total_trades": self.total_trades,
            "maker_trades": self.maker_trades,
            "arb_trades": self.arb_trades,
            "open_positions": [
                {
                    "title": p.event_title,
                    "slug": p.event_slug,
                    "cost": p.cost_usdc,
                    "shares": p.shares,
                    "expected_profit": p.expected_profit_usdc,
                    "roi_pct": p.roi_percent,
                }
                for p in open_positions
            ],
        }
