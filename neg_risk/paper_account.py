"""Production-Grade Paper Trading Account Ledger & Settlement Tracker for Neg-Risk Arbitrage & Maker Strategies.

Features:
- Initial Capital: 200.0 USDC (configurable)
- Per-Order Budget: 20.0 USDC (configurable)
- Arbitrage Engine: Buys complete-set baskets when SumAsk < 1.00 - hurdle.
- Market Maker Inventory Engine:
  - Tracks individual token inventory and average cost basis.
  - Simulates realistic Maker fills:
    * Bid Fill: When market best_ask <= our quoted bid (market takers sell into our bid).
    * Ask Fill: When market best_bid >= our quoted ask (market takers buy from our ask), realizing spread profit!
  - Position Capping: Limits maximum inventory per token to avoid over-exposure.
- Event Settlement:
  - Automatically queries Gamma API for resolution.
  - Winning token receives $1.00 payout per share.
  - Losing tokens resolve to $0.00.
  - Complete sets guaranteed positive profit because SumBid < 1.00.
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
class TokenInventory:
    token_id: str
    label: str
    event_slug: str
    event_title: str
    shares: float
    total_cost: float
    avg_price: float
    last_market_price: float = 0.0
    unrealized_pnl: float = 0.0

    def update_cost_basis(self, new_shares: float, fill_price: float) -> None:
        self.shares += new_shares
        self.total_cost += new_shares * fill_price
        self.avg_price = self.total_cost / self.shares if self.shares > 0 else 0.0

    def reduce_shares(self, sell_shares: float, fill_price: float) -> float:
        """Reduce shares and return realized PnL."""
        realized = sell_shares * (fill_price - self.avg_price)
        self.shares -= sell_shares
        self.total_cost = self.shares * self.avg_price
        return realized


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
    status: str = "OPEN"  # OPEN, WON_SETTLED, CLOSED
    realized_pnl: float = 0.0
    settled_at_epoch: float | None = None
    winning_token_id: str | None = None


class NegRiskPaperAccount:
    def __init__(
        self,
        *,
        initial_capital: float = 200.0,
        budget_per_order: float = 20.0,
        max_token_inventory_usdc: float = 15.0,  # Max inventory per token in USDC
        state_file: str = "data/negrisk_state.json",
        events_file: str = "data/negrisk_events.jsonl",
    ) -> None:
        self.initial_capital = initial_capital
        self.budget_per_order = budget_per_order
        self.max_token_inventory_usdc = max_token_inventory_usdc
        self.state_file = state_file
        self.events_file = events_file

        self.cash_balance = initial_capital
        self.realized_pnl = 0.0
        self.total_trades = 0
        self.maker_trades = 0
        self.arb_trades = 0

        self.inventory: dict[str, TokenInventory] = {}
        self.basket_positions: dict[str, PaperBasketPosition] = {}

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

            raw_inv = data.get("inventory", {})
            self.inventory = {
                tok: TokenInventory(**item)
                for tok, item in raw_inv.items()
                if item.get("shares", 0) > 0.0001
            }

            raw_pos = data.get("basket_positions", {})
            self.basket_positions = {
                pid: PaperBasketPosition(**pdata)
                for pid, pdata in raw_pos.items()
            }
        except Exception as exc:
            logger.error(f"Failed to load paper state from {self.state_file}: {exc}")

    def save_state(self) -> None:
        active_inv = {k: v for k, v in self.inventory.items() if v.shares > 0.0001}
        inv_cost = sum(v.total_cost for v in active_inv.values())
        open_baskets = [p for p in self.basket_positions.values() if p.status == "OPEN"]
        basket_cost = sum(p.cost_usdc for p in open_baskets)
        total_open_cost = inv_cost + basket_cost
        total_equity = self.cash_balance + total_open_cost

        data = {
            "initial_capital": self.initial_capital,
            "cash_balance": round(self.cash_balance, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "total_trades": self.total_trades,
            "maker_trades": self.maker_trades,
            "arb_trades": self.arb_trades,
            "open_positions_count": len(active_inv) + len(open_baskets),
            "open_cost_usdc": round(total_open_cost, 4),
            "total_equity_usdc": round(total_equity, 4),
            "inventory": {tok: asdict(v) for tok, v in active_inv.items()},
            "basket_positions": {pid: asdict(p) for pid, p in self.basket_positions.items()},
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
        for p in self.basket_positions.values():
            if p.event_slug == opp.event_slug and p.status == "OPEN":
                return False

        if self.cash_balance < 5.0:
            return False

        budget = min(float(self.budget_per_order), self.cash_balance)
        sum_px = float(opp.sum_price)

        if opp.arb_type == "LONG_BASKET_BUY":
            if sum_px >= 1.00 or sum_px <= 0.0:
                return False
            shares = round(budget / sum_px, 4)
            actual_cost = round(shares * sum_px, 4)
            expected_payout = round(shares * 1.00, 4)
            expected_profit = round(expected_payout - actual_cost, 4)
        else:  # SHORT_BASKET_SELL
            if sum_px <= 1.00:
                return False
            shares = round(budget / 1.00, 4)
            actual_cost = round(shares * 1.00, 4)
            expected_payout = round(shares * sum_px, 4)
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
        self.basket_positions[pos_id] = pos
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

    def process_maker_quotes(
        self,
        plan: MakerPlan,
        books: dict[str, BucketBook],
    ) -> int:
        """Process realistic two-sided Maker quoting, Bid fills, and Ask fills with inventory management."""
        if not plan.is_structurally_safe or self.cash_balance < 3.0:
            return 0

        fills_count = 0

        for q in plan.quotes:
            book = books.get(q.token_id)
            if not book:
                continue

            # Update mark-to-market price for existing inventory
            inv = self.inventory.get(q.token_id)
            if inv and inv.shares > 0.0001:
                cur_px = float(book.best_bid) if book.best_bid is not None else float(q.fair_prob)
                inv.last_market_price = cur_px
                inv.unrealized_pnl = round(inv.shares * (cur_px - inv.avg_price), 4)

            # --- Check Maker Ask Fill (Market Taker lifts our ask) ---
            # Condition: Market best_bid >= our quoted ask_price, and we have inventory to sell
            if inv and inv.shares >= 1.0 and book.best_bid is not None and book.best_bid >= q.ask_price:
                sell_price = float(q.ask_price)
                sell_shares = min(inv.shares, float(q.ask_size), 25.0)
                if sell_shares >= 1.0:
                    realized = inv.reduce_shares(sell_shares, sell_price)
                    proceed = round(sell_shares * sell_price, 4)
                    self.cash_balance += proceed
                    self.realized_pnl += realized
                    self.total_trades += 1
                    self.maker_trades += 1
                    fills_count += 1

                    self.log_event("paper_maker_ask_fill", {
                        "event_slug": plan.event_slug,
                        "title": plan.title,
                        "token_id": q.token_id,
                        "label": q.label,
                        "side": "SELL",
                        "fill_price": sell_price,
                        "shares": sell_shares,
                        "proceed_usdc": proceed,
                        "realized_pnl_usdc": round(realized, 4),
                        "cash_balance": round(self.cash_balance, 4),
                    })
                    logger.info(
                        f"💰 [MAKER ASK FILL] {plan.title} [{q.label}] | Sold {sell_shares} sh @ {sell_price} -> "
                        f"Realized PnL: {round(realized, 4):+} USDC | Cash: {round(self.cash_balance, 2)}U"
                    )

            # --- Check Maker Bid Fill (Market Taker hits our bid) ---
            # Condition: Market best_ask <= our quoted bid_price, and we have cash & haven't exceeded inventory cap
            inv_cost_now = inv.total_cost if inv else 0.0
            if inv_cost_now < self.max_token_inventory_usdc and self.cash_balance >= 3.0:
                if book.best_ask is not None and book.best_ask <= q.bid_price:
                    buy_price = float(q.bid_price)
                    buy_budget = min(self.max_token_inventory_usdc - inv_cost_now, self.cash_balance, 5.0)
                    buy_shares = round(buy_budget / buy_price, 2) if buy_price > 0 else 0.0

                    if buy_shares >= 1.0:
                        cost = round(buy_shares * buy_price, 4)
                        self.cash_balance -= cost
                        self.total_trades += 1
                        self.maker_trades += 1
                        fills_count += 1

                        if not inv:
                            inv = TokenInventory(
                                token_id=q.token_id,
                                label=q.label,
                                event_slug=plan.event_slug,
                                event_title=plan.title,
                                shares=buy_shares,
                                total_cost=cost,
                                avg_price=buy_price,
                                last_market_price=buy_price,
                            )
                            self.inventory[q.token_id] = inv
                        else:
                            inv.update_cost_basis(buy_shares, buy_price)

                        self.log_event("paper_maker_bid_fill", {
                            "event_slug": plan.event_slug,
                            "title": plan.title,
                            "token_id": q.token_id,
                            "label": q.label,
                            "side": "BUY",
                            "fill_price": buy_price,
                            "shares": buy_shares,
                            "cost_usdc": cost,
                            "total_inventory_shares": inv.shares,
                            "cash_balance": round(self.cash_balance, 4),
                        })
                        logger.info(
                            f"📥 [MAKER BID FILL] {plan.title} [{q.label}] | Bought {buy_shares} sh @ {buy_price} "
                            f"(Cost: {cost}U) | Total Inventory: {inv.shares} sh | Cash: {round(self.cash_balance, 2)}U"
                        )

        if fills_count > 0:
            self.save_state()
        return fills_count

    def check_and_settle_events(self) -> int:
        """Poll Gamma resolution status for open positions/inventory and settle."""
        settled_count = 0
        now = time.time()

        # Check unique event slugs across inventory and baskets
        all_slugs = set()
        for p in self.basket_positions.values():
            if p.status == "OPEN":
                all_slugs.add(p.event_slug)
        for inv in self.inventory.values():
            if inv.shares > 0.0001:
                all_slugs.add(inv.event_slug)

        for slug in all_slugs:
            url = f"{GAMMA_EVENT_ENDPOINT}{slug}"
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

            resolved_winning_token = None
            for m in markets:
                if not isinstance(m, dict):
                    continue
                try:
                    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
                    clob_tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else (m.get("clobTokenIds") or [])
                    if len(prices) >= 2 and prices[0] == "1" and prices[1] == "0":
                        resolved_winning_token = str(clob_tokens[0])
                        break
                except Exception:
                    continue

            if not (is_closed or resolved_winning_token):
                continue

            # 1. Settle Arbitrage Baskets on this event
            for pos in list(self.basket_positions.values()):
                if pos.event_slug == slug and pos.status == "OPEN":
                    payout = pos.expected_payout_usdc
                    profit = payout - pos.cost_usdc
                    pos.status = "WON_SETTLED"
                    pos.realized_pnl = round(profit, 4)
                    pos.settled_at_epoch = now
                    pos.winning_token_id = resolved_winning_token

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

            # 2. Settle Individual Token Inventories on this event
            for tok, inv in list(self.inventory.items()):
                if inv.event_slug == slug and inv.shares > 0.0001:
                    is_winner = (resolved_winning_token is not None and tok == resolved_winning_token)
                    payout = inv.shares * 1.00 if is_winner else 0.0
                    realized = payout - inv.total_cost

                    self.cash_balance += payout
                    self.realized_pnl += realized
                    inv.shares = 0.0
                    inv.total_cost = 0.0
                    settled_count += 1

                    self.log_event("paper_inventory_settled", {
                        "event_slug": slug,
                        "title": inv.event_title,
                        "token_id": tok,
                        "label": inv.label,
                        "is_winner": is_winner,
                        "payout_usdc": round(payout, 4),
                        "realized_pnl_usdc": round(realized, 4),
                        "cash_balance": round(self.cash_balance, 4),
                    })
                    logger.info(
                        f"🎯 [INVENTORY SETTLED] {inv.event_title} [{inv.label}] | Winner: {is_winner} | "
                        f"Payout: {payout}U | Realized: {round(realized, 4):+}U | Cash: {round(self.cash_balance, 2)}U"
                    )

        if settled_count > 0:
            self.save_state()
        return settled_count

    def get_summary(self) -> dict[str, Any]:
        active_inv = [v for v in self.inventory.values() if v.shares > 0.0001]
        inv_cost = sum(v.total_cost for v in active_inv)
        open_baskets = [p for p in self.basket_positions.values() if p.status == "OPEN"]
        basket_cost = sum(p.cost_usdc for p in open_baskets)
        total_open_cost = inv_cost + basket_cost
        total_equity = self.cash_balance + total_open_cost

        return {
            "initial_capital": self.initial_capital,
            "cash_balance": round(self.cash_balance, 4),
            "open_positions_count": len(active_inv) + len(open_baskets),
            "open_cost_usdc": round(total_open_cost, 4),
            "total_equity_usdc": round(total_equity, 4),
            "realized_pnl_usdc": round(self.realized_pnl, 4),
            "total_trades": self.total_trades,
            "maker_trades": self.maker_trades,
            "arb_trades": self.arb_trades,
            "inventory": [
                {
                    "token_id": inv.token_id,
                    "label": inv.label,
                    "event": inv.event_title,
                    "shares": inv.shares,
                    "avg_price": round(inv.avg_price, 4),
                    "cost": round(inv.total_cost, 4),
                    "unrealized_pnl": inv.unrealized_pnl,
                }
                for inv in active_inv
            ],
            "open_baskets": [
                {
                    "title": p.event_title,
                    "slug": p.event_slug,
                    "cost": p.cost_usdc,
                    "shares": p.shares,
                    "expected_profit": p.expected_profit_usdc,
                    "roi_pct": p.roi_percent,
                }
                for p in open_baskets
            ],
        }
