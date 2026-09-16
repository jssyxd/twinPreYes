#!/usr/bin/env python3
"""tests_live_exit.py — live 真实 SELL 出场通道（消除 live 下的「虚拟平仓」）全套测试（2026-09-13）。

被验证的实现：
  * ``live/exit.py`` —— 纯决策（``exit_settings`` / ``plan_live_exit``）+ 网络编排
    （``LiveExitChannel`` / ``get_channel`` / ``live_exit_leg``）+ 按真实成交记账
    （``apply_live_exit_fill``）
  * ``live/v2_transport.execute_leg`` —— SELL 侧 ``floor`` 价格护栏（BUY 仍用 ``cap``）
  * ``_r_cycle.py`` —— 4 个退场调用点的 ``if mode == "live"`` 分支 + 续卖扫 + 不变式哨兵

原则：
  * **零真实订单 / 零网络**：transport 是桩，SDK 是桩（``tests_port._stub_v2_lib``），
    但 ``execute_leg`` 的**真身**在跑（价格/地板/tick/精度/签名域判定全部是真代码）。
  * **真值来自真实函数**：地板矩阵、部分成交、拒单、记账、撤单顺序都走真实函数，
    桩只提供"交易所的嘴"。
  * **paper 逐字不变**：4 个调用点的 paper 分支源码逐字比对 + paper 行为 golden 相同。

Run:  python3.13 tests_live_exit.py   → 每个 check 打印 PASS/FAIL，退出码 = 失败数。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import tests_port as tp  # noqa: E402  （复用 v2 SDK 桩 + 脚本化 client）
import _r_cycle  # noqa: E402
import _r_state  # noqa: E402
import paper_capital  # noqa: E402
import re_execution  # noqa: E402
from _r_globals import book_cache  # noqa: E402
from live import exit as live_exit  # noqa: E402
from live import port as port_mod  # noqa: E402
from live import submit, v2_transport  # noqa: E402

ZERO = Decimal("0")
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
TMP = Path(tempfile.mkdtemp(prefix="live-exit-tests-"))
#: 默认审计落点（**测试进程内**的临时目录；生产默认路径绝不被本测试写入）
submit.AUDIT_PATH = TMP / "live_events.jsonl"

GATES_OK = dict(tp.GATES_OK)


def _live_env(**overrides) -> dict:
    """一套**全过**的三闸门 env（+ 合法凭据格式）。逐个 override 可造出缺闸门的用例。"""
    env = {
        "POLY_PRIVATE_KEY": "0x" + "a1" * 32,
        "POLY_FUNDER_ADDRESS": "0x" + "b2" * 20,
        "POLY_SIGNATURE_TYPE": "1",
        "LIVE_SUBMIT_ENABLED": "1",
        "YES2RE_LIVE_ENABLE_SUBMIT": "1",
        "YES2RE_LIVE_CONFIRM": submit.phrase(),
    }
    env.update(overrides)
    return env


def _audit(tag: str) -> Path:
    d = TMP / tag
    d.mkdir(parents=True, exist_ok=True)
    return d / "live_events.jsonl"


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------- 桩（只有"交易所的嘴"是假的）

class ExitTransport:
    """模块状 transport 桩：``execute_leg`` 是**真身**，其余是脚本化的交易所读口。"""

    SUBMIT_METHODS = v2_transport.SUBMIT_METHODS
    ADMIN_METHODS = v2_transport.ADMIN_METHODS
    STATE_WRITE_METHODS = v2_transport.STATE_WRITE_METHODS
    RFQ_SUBMIT_METHODS = v2_transport.RFQ_SUBMIT_METHODS
    RELEASE_WRITE_METHODS = v2_transport.RELEASE_WRITE_METHODS
    RELEASE_READ_METHODS = v2_transport.RELEASE_READ_METHODS

    def __init__(self, *, client, open_orders=(), refetch=None, account=None, cancel_ok=True,
                 cancel_detail="cancel refused"):
        self.client = client
        self.calls: list[tuple] = []
        self.open_orders = list(open_orders)
        self.refetch_map = dict(refetch or {})
        self.account = dict(account or {"usdc_balance": 51.0, "open_orders": 0, "positions": [],
                                        "positions_value_usdc": 0.0})
        self.cancel_ok = cancel_ok
        self.cancel_detail = cancel_detail

    # -- write / read surfaces the exit channel uses -------------------------
    def build_client(self, creds):
        self.calls.append(("build_client",))
        return self.client

    def execute_leg(self, client, **kwargs):
        """**真** ``v2_transport.execute_leg``（在 SDK 桩下运行）。"""
        self.calls.append(("execute_leg", kwargs))
        with tp._stub_v2_lib():
            return v2_transport.execute_leg(client, **kwargs)

    def list_open_orders(self, client):
        self.calls.append(("list_open_orders",))
        return {"ok": True, "count": len(self.open_orders), "orders": list(self.open_orders)}

    def cancel_with_retry(self, client, order_id, *, attempts=3, sleep=None, audit_path=None):
        self.calls.append(("cancel_with_retry", str(order_id)))
        if not self.cancel_ok:
            return {"ok": False, "reason": "cancel_failed", "detail": self.cancel_detail,
                    "order_id": str(order_id)}
        return {"ok": True, "reason": "ok", "detail": "canceled", "order_id": str(order_id)}

    def refetch_book(self, client, token_id):
        self.calls.append(("refetch_book", token_id))
        return self.refetch_map.get(token_id)

    def read_account(self, client, *, address=None, timeout=25):
        self.calls.append(("read_account",))
        return dict(self.account)

    # ------------------------------------------------------------------ 断言助手
    def sent(self, side=None) -> list[dict]:
        return [kw for name, *rest in self.calls for kw in rest
                if name == "execute_leg" and (side is None or kw.get("side") == side)]

    def names(self) -> list[str]:
        return [name for name, *_ in self.calls]

    def orders(self) -> list[tuple]:
        """**真正提交出去的单**（``post_order`` 落到 client 上）。

        与 :meth:`sent` 的区别很关键：``sent`` 数的是"尝试发单"（``execute_leg`` 被调用，
        可能已在本地被拒），``orders`` 数的是交易所真的收到了的单（签名不算）。
        """
        return [c for c in self.client.calls if c[0] == "post_order"]


def _client(*, matched="25", size="25", price="0.60", token="Y31", status="matched",
            book=None, cancel_raises=False, trades=None, post_error=None):
    """脚本化的 v2 client（**唯一**的"交易所"）。"""
    cls = tp._StubV2Client
    client = cls(book=book, order_states=[{"status": status, "size_matched": matched,
                                           "original_size": size, "price": price,
                                           "asset_id": token}],
                 trades=trades if trades is not None else (
                     [{"orderID": "ORD-1", "size": matched, "price": price}]
                     if Decimal(str(matched)) > 0 else []),
                 cancel_raises=cancel_raises)
    if post_error is not None:
        def _post(signed, order_type, post_only=False):
            client.calls.append(("post_order", signed, str(order_type), post_only))
            raise post_error
        client.post_order = _post
    return client


class _NoMatch(Exception):
    def __init__(self):
        super().__init__("no orders found to match with FAK order")
        self.error_msg = {"error": "no orders found to match with FAK order", "orderID": "ORD-KILLED"}


def _channel(transport, *, env=None, gates=None, limits=None, audit=None, poll_attempts=2,
             account_reader=None, cancel_sweep=None, preopened=True):
    """真 ``LivePort`` + 真 ``LiveExitChannel``（只有 transport 是桩）。"""
    env = dict(env if env is not None else _live_env())
    gates = dict(gates if gates is not None else GATES_OK)
    port = port_mod.LivePort(transport, env=env, gates=gates, limits=limits or {},
                             audit_path=audit, poll_attempts=poll_attempts,
                             account_reader=account_reader)
    if preopened:
        # 模拟"本进程已经握过手"：客户端已在缓存里 ⇒ ensure_client 走 cached 分支（零凭据 I/O）
        port._client = transport.client
        port._credentials = {"funder_address": env.get("POLY_FUNDER_ADDRESS")}
    return live_exit.LiveExitChannel(port, env=env, gates=gates, audit_path=audit,
                                     cancel_sweep=cancel_sweep)


def _live_cfg(**over) -> dict:
    cfg = {"mode": "live", "log_path": str(TMP / "engine_events.jsonl"),
           "base_fee_rate": "0.02", "fire_budget_usdc": 10.0,
           "consensus_lock": {"live_sell_enabled": True, "live_sell_floor": "0.05",
                              "live_sell_max_attempts_per_cycle": 1}}
    cfg.update(over)
    return cfg


def _leg(shares="25", token="Y31", outcome="YES", name="buy_yes_lock", cost="15.0", settled=False):
    return {"leg": name, "token_id": token, "side": "BUY", "outcome": outcome,
            "shares": shares, "cost_usdc": cost, "avg_price": "0.60", "bucket_id": "b31",
            "settled": settled}


def _pos(*legs, settled=False, liquidated=False):
    return {"key": "paris|2026-09-13|high", "kind": "consensus_lock", "settled": settled,
            "liquidated": liquidated, "legs": list(legs)}


def _state(debit="15.0"):
    return {"paper_initial_capital_usdc": 700.0, "paper_total_debit_usdc": float(debit),
            "positions": {}, "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {},
                                                  "last_obs_time": {}}}


def _book(bid, *, token="Y31", tick="0.001", neg_risk=True):
    book = {"best_bid": str(bid) if bid is not None else None, "best_ask": "0.99",
            "tick_size": tick,
            "bids": [{"price": str(bid), "size": "500"}] if bid is not None else [],
            "asks": [{"price": "0.99", "size": "500"}]}
    if neg_risk is not None:
        book["neg_risk"] = neg_risk
    return book


# ===========================================================================================
# 1) 地板矩阵（真 plan_live_exit + 真 execute_leg）
# ===========================================================================================
def check_floor_matrix():
    """``best_bid ∈ {0.049, 0.050, 0.051}`` × ``floor = 0.05`` ⇒ 弃 / 卖 / 卖。

    端点语义：**下界含**（``bid == floor`` ⇒ 卖），``bid < floor`` ⇒ 弃。决策层
    （``plan_live_exit``：``bid < floor`` ⇒ defer）与下单层（``execute_leg``：``want < floor``
    ⇒ ``below_exit_floor``）**同一条边界**，两层都同意。
    """
    floor = live_exit.parse_floor("0.05")
    assert floor == Decimal("0.05")
    results = {}
    for bid in ("0.049", "0.050", "0.051"):
        # (1) 纯决策
        plan = live_exit.plan_live_exit(mode="live", settings=live_exit.exit_settings(
            {"consensus_lock": {"live_sell_floor": "0.05", "live_sell_enabled": True}}),
            best_bid=bid, gates=GATES_OK, attempts=0, shares=Decimal("25"))
        # (2) 端到端（真 execute_leg，桩 client）
        client = _client(matched="25", price=bid, token="Y31")
        transport = ExitTransport(client=client)
        audit = _audit(f"floor-{bid}")
        leg = _leg()
        state, pos = _state(), _pos(leg)
        res = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=leg, pos=pos,
                                     books={"Y31": _book(bid)}, channel=live_exit.CH_SLEEVE_TIMEOUT,
                                     now_utc=NOW, channel_obj=_channel(transport, audit=audit))
        orders = transport.sent(side="SELL")
        results[bid] = (plan["action"], plan["reason"], res, len(orders))
        if bid == "0.049":
            assert plan["action"] == "defer" and plan["reason"] == live_exit.REASON_BELOW_FLOOR, plan
            assert res["sold"] is False and res["deferred"] is True, res
            assert res["reason"] == live_exit.REASON_BELOW_FLOOR, res
            assert orders == [] and transport.orders() == [], transport.calls
            assert leg["settled"] is False and pos.get("liquidated") is not True, (leg, pos)
        else:
            assert plan["action"] == "sell", plan
            assert res["sold"] is True and res["deferred"] is False, res
            assert len(orders) == 1, transport.calls
            kw = orders[0]
            assert kw["side"] == "SELL" and kw["taker"] is True and kw["clamp"] is False, kw
            assert kw["price"] == Decimal(bid) and kw["floor"] == floor, kw
            assert kw["exit_channel"] == live_exit.CH_SLEEVE_TIMEOUT, kw
            assert res["bid"] == bid and res["filled_shares"] == "25", res
            assert leg["settled"] is True and leg.get("closed_by") == live_exit.CH_SLEEVE_TIMEOUT, leg
            assert pos["liquidated"] is True, pos
    print("PASS check_floor_matrix: 0.049⇒弃(below_sell_floor) / 0.050⇒卖 / 0.051⇒卖；"
          f"端点语义 bid<floor 弃、bid==floor 卖；发单价 == best_bid 且 floor 保护 = {floor}")


# ===========================================================================================
# 2) floor 缺失 / 非法 ⇒ 拒单且零发单（绝无回退）
# ===========================================================================================
def check_floor_missing_or_invalid():
    audit = _audit("floor-invalid")
    bad = [(None, live_exit.REASON_FLOOR_REQUIRED), ("0", live_exit.REASON_FLOOR_INVALID),
           ("-0.1", live_exit.REASON_FLOOR_INVALID), ("1.5", live_exit.REASON_FLOOR_INVALID),
           ("abc", live_exit.REASON_FLOOR_INVALID)]
    for raw, want_reason in bad:
        # 决策层
        # 显式开启开关（代码默认已改 fail-safe=False）；本用例测的是**开启后**的地板校验
        settings = live_exit.exit_settings({"consensus_lock": {"live_sell_enabled": True,
                                                              "live_sell_floor": raw}})
        assert settings["floor"] is None and settings["floor_reason"] == want_reason, settings
        plan = live_exit.plan_live_exit(mode="live", settings=settings, best_bid="0.60",
                                        gates=GATES_OK, attempts=0, shares=Decimal("25"))
        assert plan["action"] == "defer" and plan["reason"] == want_reason, (raw, plan)
        # 下单层（真 execute_leg）：SELL 没有可用地板 ⇒ 拒单、零签名、零发单
        client = _client(matched="25", price="0.60")
        transport = ExitTransport(client=client)
        log = _audit(f"floor-invalid-{raw}")
        with tp._stub_v2_lib():
            out = v2_transport.execute_leg(
                client, token_id="Y31", side="SELL", price="0.60", size="25",
                book=_book("0.60"), gates=GATES_OK, taker=True, floor=raw, poll_attempts=2,
                sleep=lambda _s: None, audit_path=log, exit_channel=live_exit.CH_BREACH_RC)
        assert out["ok"] is False and out["status"] == want_reason, (raw, out)
        assert not [c for c in client.calls if c[0] in ("post_order", "create_market_order")], \
            (raw, client.calls)
        rows = _rows(log)
        assert [r["action"] for r in rows] == ["deny"] and rows[0]["reason"] == want_reason, rows
        assert "sell_floor" not in rows[0]["params"], rows[0]
    # 合法地板仍然发单（对照组证明上面的拒单来自地板而不是路径坏了）
    client = _client(matched="25", price="0.60")
    transport = ExitTransport(client=client)
    with tp._stub_v2_lib():
        out = v2_transport.execute_leg(
            client, token_id="Y31", side="SELL", price="0.60", size="25", book=_book("0.60"),
            gates=GATES_OK, taker=True, floor="0.05", poll_attempts=2, sleep=lambda _s: None,
            audit_path=_audit("floor-valid"), exit_channel=live_exit.CH_BREACH_RC)
    assert out["ok"] is True and len(transport.orders()) == 1, out
    print("PASS check_floor_missing_or_invalid: floor={None,0,-0.1,1.5,abc} ⇒ "
          "exit_floor_required/exit_floor_invalid，零签名零发单（合法地板对照组发单 1 笔）")


# ===========================================================================================
# 3) 部分成交：余量留仓 + 下一轮清空
# ===========================================================================================
def check_partial_fill_two_rounds():
    audit = _audit("partial")
    transport = ExitTransport(client=_client(matched="15", size="25", price="0.60"))
    chan = _channel(transport, audit=audit)
    state = _state(debit="15.0")
    yes, no_leg = _leg(shares="25"), _leg(shares="10", token="N31", outcome="NO", name="buy_no_broken")
    pos = _pos(yes, no_leg)
    r1 = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=yes, pos=pos,
                                books={"Y31": _book("0.60")}, channel=live_exit.CH_EARLY_STOP,
                                now_utc=NOW, channel_obj=chan)
    # 只减股数；腿/仓都不算了结
    assert r1["sold"] is True and r1["filled_shares"] == "15", r1
    assert r1["remaining_shares"] == "10.0000" and r1["leg_settled"] is False, r1
    assert yes["shares"] == "10.0000" and yes.get("settled") is not True, yes
    assert pos.get("liquidated") is not True, pos
    assert pos["pending_exit"]["Y31"]["shares"] == "10.0000", pos["pending_exit"]
    gross1, fee1, net1 = (Decimal(r1["gross_usdc"]), Decimal(r1["fee_usdc"]),
                          Decimal(r1["proceeds_usdc"]))
    assert gross1 == Decimal("9.0000") and fee1 == Decimal("0.1800") and net1 == Decimal("8.8200"), r1
    assert Decimal(str(state["paper_total_debit_usdc"])) == Decimal("6.1800"), state
    # 第二轮（**下一轮**：run_cycle 会 bump 轮次序号 ⇒ 尝试计数归零）：余量 10 股全部卖出
    # ⇒ leg.settled + pos.liquidated（YES 腿全了结）
    live_exit.bump_cycle(state)
    transport2 = ExitTransport(client=_client(matched="10", size="10", price="0.58"))
    chan2 = _channel(transport2, audit=audit)
    r2 = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=yes, pos=pos,
                                books={"Y31": _book("0.58")}, channel=live_exit.CH_EARLY_STOP,
                                now_utc=NOW, channel_obj=chan2)
    assert r2["sold"] is True and r2["leg_settled"] is True and r2["remaining_shares"] == "0.0000", r2
    assert yes["shares"] == "0.0000" and yes["settled"] is True, yes
    assert yes["payout_credit_usdc"] == str((net1 + Decimal("5.6840"))), yes
    assert pos["liquidated"] is True and "pending_exit" not in pos, pos
    assert no_leg.get("settled") is not True and pos.get("settled") is not True, (no_leg, pos)
    assert len([c for c in transport.sent("SELL")]) == 1 and len(transport2.sent("SELL")) == 1
    # 累计明细分两笔（审计可查）
    assert len(yes["exit_fills"]) == 2, yes["exit_fills"]
    assert [f["shares"] for f in yes["exit_fills"]] == ["15", "10"], yes["exit_fills"]
    print("PASS check_partial_fill_two_rounds: 15/25 成交 ⇒ 留 10 股(未 settled、仓未 liquidated、"
          "登记 pending_exit)；第二轮清空 ⇒ leg.settled + pos.liquidated（NO 腿仍待 settle）")


# ===========================================================================================
# 4) 拒单 / 无买盘 / 无深度 ⇒ exit_deferred，仓仍 open、无 liquidated、下轮可重试
# ===========================================================================================
def check_defer_keeps_position_open():
    audit = _audit("defer")
    # (a) 被拒（submit 抛异常）⇒ submit_failed
    t_a = ExitTransport(client=_client(matched="0", post_error=RuntimeError("venue 400: rejected")))
    leg = _leg()
    state, pos = _state(), _pos(leg)
    r = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=leg, pos=pos,
                                books={"Y31": _book("0.60")}, channel=live_exit.CH_BREACH_RC,
                                now_utc=NOW, channel_obj=_channel(t_a, audit=audit))
    assert r["deferred"] is True and r["sold"] is False and r["reason"] == "submit_failed", r
    assert leg["shares"] == "25" and leg.get("settled") is not True, leg
    assert pos.get("liquidated") is not True and pos.get("settled") is not True, pos
    assert pos["pending_exit"]["Y31"]["last_reason"] == "submit_failed", pos["pending_exit"]
    assert Decimal(str(state["paper_total_debit_usdc"])) == Decimal("15.0"), state
    # (b) 无买盘（best_bid 缺失 + 重取也拿不到）
    t_b = ExitTransport(client=_client(matched="0"), refetch={"Y31": {"tick_size": "0.001"}})
    leg_b = _leg()
    state_b, pos_b = _state(), _pos(leg_b)
    r_b = live_exit.live_exit_leg(cfg=_live_cfg(), state=state_b, leg=leg_b, pos=pos_b,
                                  books={"Y31": _book(None)}, channel=live_exit.CH_SLEEVE_TIMEOUT,
                                  now_utc=NOW, channel_obj=_channel(t_b, audit=audit))
    assert r_b["deferred"] is True and r_b["reason"] == live_exit.REASON_NO_BID, r_b
    assert t_b.sent(side="SELL") == [] and ("refetch_book", "Y31") in t_b.calls, t_b.calls
    assert leg_b["shares"] == "25" and pos_b.get("liquidated") is not True, (leg_b, pos_b)
    # (c) 无深度：FAK 被交易所冲掉 0 成交
    t_c = ExitTransport(client=_client(matched="0", post_error=_NoMatch()))
    leg_c = _leg()
    state_c, pos_c = _state(), _pos(leg_c)
    r_c = live_exit.live_exit_leg(cfg=_live_cfg(), state=state_c, leg=leg_c, pos=pos_c,
                                  books={"Y31": _book("0.60")}, channel=live_exit.CH_SLEEVE_TIMEOUT,
                                  now_utc=NOW, channel_obj=_channel(t_c, audit=audit))
    assert r_c["deferred"] is True and r_c["reason"] == live_exit.REASON_DEPTH, r_c
    assert leg_c["shares"] == "25" and leg_c.get("settled") is not True, leg_c
    assert Decimal(str(state_c["paper_total_debit_usdc"])) == Decimal("15.0"), state_c
    # (d) 离价（bid 不在 tick 上）⇒ 真 execute_leg 拒单，绝不改价
    t_d = ExitTransport(client=_client(matched="25"))
    leg_d = _leg()
    state_d, pos_d = _state(), _pos(leg_d)
    r_d = live_exit.live_exit_leg(cfg=_live_cfg(), state=state_d, leg=leg_d, pos=pos_d,
                                  books={"Y31": _book("0.055", tick="0.01")},
                                  channel=live_exit.CH_SLEEVE_TIMEOUT, now_utc=NOW,
                                  channel_obj=_channel(t_d, audit=audit))
    assert r_d["deferred"] is True and r_d["reason"] == "price_not_on_tick", r_d
    assert t_d.orders() == [] and len(t_d.sent("SELL")) == 1, t_d.calls
    # (e) 延期记录可被下一轮重试：同一腿第二轮买盘回来了 ⇒ 真实卖出
    t_e = ExitTransport(client=_client(matched="25", size="25", price="0.60"))
    pos_e = _pos(leg_d)
    r_e = live_exit.live_exit_leg(cfg=_live_cfg(), state=_state(), leg=leg_d, pos=pos_e,
                                  books={"Y31": _book("0.60")}, channel=live_exit.CH_SLEEVE_TIMEOUT,
                                  now_utc=NOW, channel_obj=_channel(t_e, audit=audit))
    assert r_e["sold"] is True and len(t_e.sent("SELL")) == 1, r_e
    # (f) 延期腿仍是"未 settle" ⇒ 落在既有 settle 兜底范围内
    assert leg_b.get("settled") is not True
    rows = [r for r in _rows(audit) if r["action"] == "submit"]
    assert rows and rows[0]["reason"] == "fak_no_match", rows[:2]
    print("PASS check_defer_keeps_position_open: 被拒/no_bid/无深度/离价 ⇒ exit_deferred + "
          "仓位 open + 无 liquidated + 账本未动；买盘回来后同腿重试成功且走真实 FAK")


# ===========================================================================================
# 5) 三闸门：卖出仍需全过；入场型上限不得阻挡卖出
# ===========================================================================================
def check_three_gates_and_no_entry_caps():
    cfg = _live_cfg()
    transport = ExitTransport(client=_client(matched="25", price="0.60"))
    # (a) 逐个缺闸门 ⇒ get_channel 拒绝（reason 前缀 gate_）→ live_exit_leg 延期 gate_*
    for missing, want in (("YES2RE_LIVE_ENABLE_SUBMIT", submit.GATE_FLAG),
                          ("LIVE_SUBMIT_ENABLED", submit.GATE_ENV)):
        env = _live_env(**{missing: ""})
        port_mod.reset_cache()
        try:
            live_exit.get_channel(cfg, env=env, transport=transport)
        except live_exit.ExitRefused as exc:
            assert exc.reason == f"gate_{want}", (missing, exc.reason)
        else:
            raise AssertionError(f"{missing} missing must refuse the exit channel")
        leg = _leg()
        leg = _leg()
        state, pos = _state(), _pos(leg)
        port_mod.reset_cache()
        r = live_exit.live_exit_leg(cfg=cfg, state=state, leg=leg, pos=pos,
                                    books={"Y31": _book("0.60")},
                                    channel=live_exit.CH_EARLY_STOP, now_utc=NOW,
                                    env=env, transport=ExitTransport(client=_client()))
        assert r["deferred"] is True and r["reason"] == f"gate_{want}", (missing, r)
        assert leg.get("settled") is not True and pos.get("liquidated") is not True
    env_phrase = _live_env(YES2RE_LIVE_CONFIRM="SMOKE-1970-01-01")
    port_mod.reset_cache()
    try:
        live_exit.get_channel(cfg, env=env_phrase, transport=transport)
    except live_exit.ExitRefused as exc:
        assert exc.reason.startswith("gate_"), exc.reason
    else:
        raise AssertionError("a stale confirm phrase must refuse the exit channel")
    port_mod.reset_cache()
    leg_p = _leg()
    st_p, pos_p = _state(), _pos(leg_p)
    r_p = live_exit.live_exit_leg(cfg=cfg, state=st_p, leg=leg_p, pos=pos_p,
                                  books={"Y31": _book("0.60")},
                                  channel=live_exit.CH_EARLY_STOP, now_utc=NOW,
                                  env=env_phrase,
                                  transport=ExitTransport(client=_client()))
    assert r_p["deferred"] is True and r_p["reason"].startswith("gate_"), r_p
    # (b) execute_leg 自身仍要求三闸门全过（不可绕过）；全过才发单
    audit = _audit("gates")
    for forged in (None, {}, {"ok": True}, {"ok": True, "checks": {"cli_flag": True}},
                   {"ok": True, "checks": {"cli_flag": True, "env_flag": True,
                                           "confirm_phrase": False}}):
        c = _client(matched="25")
        with tp._stub_v2_lib():
            try:
                v2_transport.execute_leg(c, token_id="Y31", side="SELL", price="0.60", size="25",
                                         book=_book("0.60"), gates=forged, taker=True,
                                         floor="0.05", audit_path=audit)
            except PermissionError:
                pass
            else:
                raise AssertionError(f"execute_leg must refuse with gates={forged!r}")
        assert not [x for x in c.calls if x[0] == "post_order"], forged
    okt = ExitTransport(client=_client(matched="25"))
    out_ok = okt.execute_leg(okt.client, token_id="Y31", side="SELL", price="0.60", size="25",
                             book=_book("0.60"), gates=GATES_OK, taker=True, floor="0.05",
                             poll_attempts=2, sleep=lambda _s: None,
                             audit_path=_audit("gates-ok"))
    assert out_ok["ok"] is True and len(okt.sent("SELL")) == 1, out_ok
    # (c) 入场型上限（持仓数满 / 资金上限 / 预算）不得阻挡卖出
    limited = port_mod.LivePort(
        transport, env=_live_env(), gates=GATES_OK,
        limits={"fire_budget_usdc": "1", "max_open_positions": "0", "max_capital_usdc": "1"},
        audit_path=audit)
    limited._client = transport.client
    limited._credentials = {"funder_address": _live_env()["POLY_FUNDER_ADDRESS"]}
    blocked = limited.preflight(fire={"budget_usdc": "12"},
                                cfg={"fire_budget_usdc": 12})
    assert blocked["ok"] is False and blocked["reason"].startswith(
        ("risk_gate:", "limits:")), blocked
    # 出场：同一套 limits/账户，走 ensure_client（不跑 risk_gate/check_limits）⇒ 照常卖
    import live.risk_gate as risk_gate
    import live.submit as submit_mod
    orig_gate, orig_limits = risk_gate.evaluate, submit_mod.check_limits
    risk_gate.evaluate = lambda **kw: (_ for _ in ()).throw(AssertionError("entry risk_gate called"))
    submit_mod.check_limits = lambda **kw: (_ for _ in ()).throw(AssertionError("entry limits called"))
    try:
        chan_limited = _channel(ExitTransport(client=_client(matched="25", price="0.60")),
                                gates=GATES_OK, limits={"fire_budget_usdc": "1",
                                                        "max_open_positions": "0",
                                                        "max_capital_usdc": "1"}, audit=audit)
        leg = _leg()
        state, pos = _state(), _pos(leg)
        res = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=leg, pos=pos,
                                      books={"Y31": _book("0.60")},
                                      channel=live_exit.CH_BREACH_RC, now_utc=NOW,
                                      channel_obj=chan_limited)
    finally:
        risk_gate.evaluate, submit_mod.check_limits = orig_gate, orig_limits
    assert res["sold"] is True, res
    assert leg["settled"] is True, leg
    print("PASS check_three_gates_and_no_entry_caps: 三闸门任一缺失 ⇒ exit_deferred:gate_*；"
          "execute_leg 仍要求全过；持仓数满/资金上限/预算（preflight 会拒）**不阻挡**卖出")


# ===========================================================================================
# 6) 撤单先于卖单（含撤单失败的 fail-closed）
# ===========================================================================================
def check_cancel_precedes_sell():
    audit = _audit("cancel-order")
    client = _client(matched="25", price="0.60")
    transport = ExitTransport(client=client,
                              open_orders=[{"id": "OLD-1", "asset_id": "Y31", "status": "live"},
                                           {"id": "OLD-2", "asset_id": "ZZZ", "status": "live"}])
    leg = _leg()
    state, pos = _state(), _pos(leg)
    res = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=leg, pos=pos,
                                  books={"Y31": _book("0.60")}, channel=live_exit.CH_REFIRE_LIQ,
                                  now_utc=NOW, channel_obj=_channel(transport, audit=audit))
    assert res["sold"] is True, res
    order = [name for name, *_ in transport.calls if name in ("cancel_with_retry", "execute_leg",
                                                              "list_open_orders")]
    assert order == ["list_open_orders", "cancel_with_retry", "execute_leg"], order
    assert [c for c in transport.calls if c[0] == "cancel_with_retry"] == \
        [("cancel_with_retry", "OLD-1")], transport.calls       # 只撤本仓 token 的挂单
    assert res["cancel"]["count"] == 1 and res["cancel"]["ok"] is True, res["cancel"]
    # 撤单失败 ⇒ 绝不发卖单（先撤后卖是硬顺序）
    t2 = ExitTransport(client=_client(matched="25", price="0.60"), cancel_ok=False,
                       open_orders=[{"id": "OLD-1", "asset_id": "Y31"}])
    leg2 = _leg()
    state2, pos2 = _state(), _pos(leg2)
    res2 = live_exit.live_exit_leg(cfg=_live_cfg(), state=state2, leg=leg2, pos=pos2,
                                   books={"Y31": _book("0.60")}, channel=live_exit.CH_REFIRE_LIQ,
                                   now_utc=NOW, channel_obj=_channel(t2, audit=audit))
    assert res2["deferred"] is True and res2["reason"] == "cant_cancel_first", res2
    assert t2.sent(side="SELL") == [], t2.calls
    assert leg2.get("settled") is not True and pos2.get("liquidated") is not True
    print("PASS check_cancel_precedes_sell: list_open_orders → cancel_with_retry → execute_leg "
          "严格顺序，只撤本仓 token；撤单失败 ⇒ 零发单 + exit_deferred")


# ===========================================================================================
# 7) taker 2% 费与净回收记账
# ===========================================================================================
def check_fee_and_net_proceeds():
    audit = _audit("fee")
    transport = ExitTransport(client=_client(matched="20", size="20", price="0.55"))
    chan = _channel(transport, audit=audit)
    leg = _leg(shares="20")
    state, pos = _state(debit="11.0"), _pos(leg)
    res = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=leg, pos=pos,
                                  books={"Y31": _book("0.55")}, channel=live_exit.CH_BREACH_RC,
                                  now_utc=NOW, channel_obj=chan)
    gross, fee, net = (Decimal(res["gross_usdc"]), Decimal(res["fee_usdc"]),
                       Decimal(res["proceeds_usdc"]))
    assert res["sold"] is True and gross == Decimal("11.0000"), res
    assert fee == Decimal("0.2200") and net == Decimal("10.7800"), res      # 2% of 11.00
    assert leg["exit_fee_usdc"] == "0.2200" and leg["exit_gross_usdc"] == "11.0000", leg
    assert leg["payout_credit_usdc"] == "10.7800", leg
    assert Decimal(str(state["paper_total_debit_usdc"])) == Decimal("0.2200"), state
    # 费率来自 cfg["base_fee_rate"]（可配），且不可为负/≥1
    cfg3 = _live_cfg(base_fee_rate="0.05")
    st3, lg3 = _state(debit="11.0"), _leg(shares="20")
    live_exit.live_exit_leg(cfg=cfg3, state=st3, leg=lg3, pos=_pos(lg3),
                            books={"Y31": _book("0.55")}, channel=live_exit.CH_BREACH_RC,
                            now_utc=NOW,
                            channel_obj=_channel(ExitTransport(
                                client=_client(matched="20", size="20", price="0.55")), audit=audit))
    assert lg3["exit_fee_usdc"] == "0.5500", lg3            # 5% of 11.00
    assert live_exit.exit_settings({"base_fee_rate": "2"})["fee_rate"] == Decimal("0.02"), \
        "an out-of-range fee rate must fall back to the 2% default"
    print("PASS check_fee_and_net_proceeds: 2% taker 费显式记账（gross 11.0000 / fee 0.2200 / "
          "net 10.7800），净回收入现金池，费率可配且越界回退 2%")


# ===========================================================================================
# 8) 负风险签名域：沿用既有解析（book 缺失 ⇒ 重取；取不到 ⇒ 拒单）
# ===========================================================================================
def check_neg_risk_resolution():
    audit = _audit("neg-risk")
    # (a) book 缺 neg_risk ⇒ 用 live 客户端重取一次 ⇒ 按取到的域签名（审计记 neg_risk_source）
    client = _client(matched="25", price="0.60")
    transport = ExitTransport(client=client, refetch={"Y31": _book("0.60", neg_risk=True)})
    chan = _channel(transport, audit=audit)
    state, leg, pos = _state(), _leg(), _pos(_leg())
    res = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=leg, pos=pos,
                                  books={"Y31": _book("0.60", neg_risk=None)},
                                  channel=live_exit.CH_SLEEVE_TIMEOUT, now_utc=NOW,
                                  channel_obj=chan)
    assert res["sold"] is True, res
    assert [c for c in transport.calls if c[0] == "refetch_book"] == [("refetch_book", "Y31")], \
        transport.calls
    kw = transport.sent("SELL")[0]
    assert kw["neg_risk"] is True and kw["neg_risk_source"] == "refetch", kw
    # (b) 同一 token 第二次不再重取（缓存）
    transport.calls.clear()
    _, leg2, pos2 = _state(), _leg(), _pos(_leg())
    live_exit.live_exit_leg(cfg=_live_cfg(), state=_state(), leg=leg2, pos=pos2,
                            books={"Y31": _book("0.60", neg_risk=None)},
                            channel=live_exit.CH_SLEEVE_TIMEOUT, now_utc=NOW, channel_obj=chan)
    assert [c for c in transport.calls if c[0] == "refetch_book"] == [], transport.calls
    # (c) 重取仍未知 ⇒ 拒单（零发单）
    t3 = ExitTransport(client=_client(matched="25", price="0.60"),
                       refetch={"Y31": {"tick_size": "0.001"}})
    state3, leg3, pos3 = _state(), _leg(), _pos(_leg())
    res3 = live_exit.live_exit_leg(cfg=_live_cfg(), state=state3, leg=leg3, pos=pos3,
                                   books={"Y31": _book("0.60", neg_risk=None)},
                                   channel=live_exit.CH_SLEEVE_TIMEOUT, now_utc=NOW,
                                   channel_obj=_channel(t3, audit=audit))
    assert res3["deferred"] is True and res3["reason"] == "neg_risk_unknown", res3
    assert t3.sent(side="SELL") == [], t3.calls
    assert leg3.get("settled") is not True
    print("PASS check_neg_risk_resolution: book 缺 neg_risk ⇒ 重取一次并按其签名域发单"
          "（neg_risk_source=refetch）；缓存复用；仍未知 ⇒ 拒单零发单")


# ===========================================================================================
# 9) 审计：side=SELL / exit_channel / sell_floor / leg_window / 成交明细，且不可被 audit_extra 伪造
# ===========================================================================================
def check_audit_fields_unforgeable():
    audit = _audit("audit")
    client = _client(matched="25", price="0.60")
    transport = ExitTransport(client=client)
    leg = _leg()
    state, pos = _state(), _pos(leg)
    events: list[dict] = []
    res = live_exit.live_exit_leg(cfg=_live_cfg(), state=state, leg=leg, pos=pos,
                                  books={"Y31": _book("0.60")}, channel=live_exit.CH_EARLY_STOP,
                                  now_utc=NOW, leg_window="(0.05, 0.60]",
                                  channel_obj=_channel(transport, audit=audit),
                                  log=events.append)
    assert res["sold"] is True
    rows = _rows(audit)
    acts = [r["action"] for r in rows]
    assert acts == ["intent", "submit"], rows
    for row in rows:
        p = row["params"]
        assert p["side"] == "SELL", p
        assert p["exit_channel"] == "early_stop" and p["sell_floor"] == "0.05", p
        assert p["leg_window"] == "(0.05, 0.60]", p
        assert p["order_api"] == "market" and p["amount_unit"] == "shares", p
        assert p["amount"] == "25", p               # SELL amount = 股数（≤4 dp 是上限）
        assert "entry_channel" not in p, p          # 卖单不属于入场通道
    assert rows[1]["order_id"] == "ORD-1", rows[1]
    # 伪造尝试：audit_extra 里的 side/exit_channel/sell_floor/leg_window 一律被剔除
    forged = {"side": "BUY", "exit_channel": "breach_rc", "sell_floor": "0.01",
              "leg_window": "(0.0, 0.0]", "amount": "999", "amount_unit": "USDC"}
    log = _audit("audit-forged")
    with tp._stub_v2_lib():
        out = v2_transport.execute_leg(_client(matched="25", price="0.60"), token_id="Y31",
                                       side="SELL", price="0.60", size="25", book=_book("0.60"),
                                       gates=GATES_OK, taker=True, floor="0.05",
                                       exit_channel=live_exit.CH_BREACH_RC,
                                       leg_window="(0.05, 0.60]", audit_extra=dict(forged),
                                       poll_attempts=2, sleep=lambda _s: None, audit_path=log)
    assert out["ok"] is True, out
    for row in _rows(log):
        p = row["params"]
        assert p["side"] == "SELL" and p["exit_channel"] == "breach_rc", p
        assert p["sell_floor"] == "0.05" and p["leg_window"] == "(0.05, 0.60]", p
        assert p["amount"] == "25" and p["amount_unit"] == "shares", p
    # 卖后真实账户入审计 + 成交明细（规则⑪）：引擎事件行
    ev = [r for r in events if r.get("type") == "live_exit"]
    assert ev, events
    last = ev[-1]
    assert last["account"] is not None and last["account"]["usdc_balance"] == 51.0, last
    assert last["side"] == "SELL" and last["exit_channel"] == "early_stop", last
    assert last["filled_shares"] == "25" and last["remaining_shares"] == "0.0000", last
    assert last["avg_price"] == "0.600000" and last["bid"] == "0.60", last
    assert last["fee_usdc"] == "0.3000" and last["net_usdc"] == "14.7000", last
    assert last["gross_usdc"] == "15.0000", last
    assert last["sell_floor"] == "0.05" and last["leg_window"] == "(0.05, 0.60]", last
    assert last["order_id"] == "ORD-1" and last["leg_settled"] is True, last
    # 延期路径的事件也带通道/地板（可审计）
    later: list[dict] = []
    _chan_d = _channel(ExitTransport(client=_client(matched="0", post_error=_NoMatch())),
                       audit=audit)
    live_exit.live_exit_leg(cfg=_live_cfg(), state=_state(), leg=_leg(), pos=_pos(_leg()),
                            books={"Y31": _book("0.60")}, channel=live_exit.CH_BREACH_RC,
                            now_utc=NOW, channel_obj=_chan_d, log=later.append)
    assert later and later[-1]["type"] == "exit_deferred", later
    assert later[-1]["exit_channel"] == "breach_rc" and later[-1]["sell_floor"] == "0.05", later
    print("PASS check_audit_fields_unforgeable: 审计行带 side=SELL/exit_channel/sell_floor/"
          "leg_window/amount(shares)；audit_extra 伪造被剔除；卖后真实账户入事件审计")


# ===========================================================================================
# 10) paper 逐字不变：4 个调用点的 paper 分支源码 + paper 行为 golden + sim sha
# ===========================================================================================
#: 从 HEAD（aaf6bfe）逐字抄下的 4 个 paper 分支（缩进归一化后必须仍是当前源码的子串）
PAPER_BRANCH_SNIPPETS = {
    "sleeve_timeout": [
        "# Shared paper close (best_bid sell / write-off at 0 when no bid) —",
        "# the same helper the 追火 old-leg liquidation uses (2026-09-09).",
        "close = close_leg_at_best_bid(",
        "state, leg, books=cache, closed_by=\"sleeve_timeout\",",
        "settled_at_utc=re_execution.iso_utc(now_utc),",
        ")",
        "if close is None:",
        "rec[\"status\"] = \"expired_no_leg\"",
        "continue",
    ],
    "refire_liq": [
        "close = close_leg_at_best_bid(",
        "state, old_yes, books=close_books, closed_by=\"refire_liquidation\",",
        "settled_at_utc=re_execution.iso_utc(now_utc),",
        ")",
    ],
    "early_stop": [
        "for leg in pos.get(\"legs\", []):",
        "if leg.get(\"outcome\") == \"YES\" and not leg.get(\"settled\"):",
        "res = close_leg_at_best_bid(",
        "state, leg, rule_books,",
        "closed_by=\"early_stop_loss\",",
        "settled_at_utc=re_execution.iso_utc(now),",
        ")",
        "liq_res[leg.get(\"token_id\")] = res",
        "pos[\"settled\"] = True",
        "pos[\"liquidated\"] = True",
        "pos[\"liquidation_type\"] = early_stop.get(\"reason\")",
    ],
    "breach_rc": [
        "for leg in pos.get(\"legs\", []):",
        "if leg.get(\"outcome\") == \"YES\" and not leg.get(\"settled\"):",
        "close_leg_at_best_bid(",
        "state, leg, rule_books,",
        "closed_by=\"metar_breach\",",
        "settled_at_utc=re_execution.iso_utc(now)",
        ")",
        "pos[\"settled\"] = True",
        "pos[\"liquidated\"] = True",
        "pos[\"liquidation_type\"] = \"METAR_BREACH\"",
    ],
}

#: HEAD 上跑同一套 paper 探针得到的 golden（sha256，含 4 条通道的 state+events）
PAPER_GOLDEN_SHA = "0b75662d02a2f990852321178397cb27f82677a94241a7a4e2060d9f97ed50c3"
#: ``paper_reversal_sim.py --scenarios-only`` 输出（stdout）在 HEAD 上的 sha256
SIM_STDOUT_SHA = "3d16632c458bb969dd9dd379fd8e3d03df767e9d1827e1c05ba20e03945b0c2c"


def check_paper_branch_source_unchanged():
    src = (ROOT / "_r_cycle.py").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in src.splitlines()]
    joined = "\n".join(lines)
    for name, snippet in PAPER_BRANCH_SNIPPETS.items():
        block = "\n".join(snippet)
        assert block in joined, f"paper branch for {name} is no longer verbatim:\n{block}"
    # 4 个调用点都必须是 ``if <live> ... : ... else:`` 的 else 分支（AST 不变式）
    import ast
    tree = ast.parse(src)
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "close_leg_at_best_bid":
            sites.append(node)
    assert len(sites) in (4, 5), f"expected 4 or 5 close_leg_at_best_bid call sites, got {len(sites)}"
    live_guards = {}
    for ifn in [n for n in ast.walk(tree) if isinstance(n, ast.If)]:
        test_src = ast.unparse(ifn.test)
        livey = ("_cycle_is_live" in test_src or "_CYCLE_MODE" in test_src
                 or ("mode" in test_src and "live" in test_src))
        if not livey:
            continue
        for sub in ifn.orelse:
            for n in ast.walk(sub):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                        and n.func.id == "close_leg_at_best_bid":
                    live_guards[id(n)] = ast.unparse(ifn.test)
    assert len(live_guards) == len(sites), (
        "every close_leg_at_best_bid call must sit in the ELSE branch of a live guard; "
        f"guarded={len(live_guards)} of {len(sites)}")
    print("PASS check_paper_branch_source_unchanged: 4 个调用点：paper 分支源码逐字保留 + "
          "AST 证明它们全部位于 live 分支的 else 里")


def _norm(obj):
    ts_keys = ("ts_utc", "settled_at_utc", "closed_at_utc", "at_utc", "entered_at_utc",
               "fires_at_utc", "validated_at_utc", "expired_at_utc", "refire_at_utc",
               "created_at", "last_obs_time")
    if isinstance(obj, dict):
        return {k: ("<ts>" if k in ts_keys and isinstance(v, str) else _norm(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm(v) for v in obj]
    return obj


def check_paper_behaviour_golden():
    """4 条通道的 **paper 行为**与 HEAD golden 逐字节相同（真 run_cycle / 真函数）。"""
    out = _norm({"sleeve": _paper_probe_sleeve(), "refire": _paper_probe_refire(),
                 "early_stop": _rc_scenario("early_stop", mode="paper"),
                 "breach": _rc_scenario("breach", mode="paper")})
    text = json.dumps(out, indent=1, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == PAPER_GOLDEN_SHA, (
        "paper behaviour changed vs HEAD golden\n"
        f"got      {digest}\nwant     {PAPER_GOLDEN_SHA}\n{text[:1500]}")
    # 可读化的关键值（digest 不匹配时上面会打印全文）
    assert out["sleeve"]["events"][0]["proceeds_usdc"] == "13.7500"
    assert out["refire"]["events"][0]["proceeds_usdc"] == "13.0000"
    assert out["early_stop"]["rows"][0]["type"] == "early_stop_loss"
    assert out["breach"]["rows"][0]["type"] == "breach_risk_control"
    # paper_reversal_sim.py --scenarios-only 的 stdout sha 与 HEAD 基线一致
    done = subprocess.run([sys.executable, "paper_reversal_sim.py", "--scenarios-only"],
                          cwd=str(ROOT), capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr[-400:]
    sim = hashlib.sha256(done.stdout).hexdigest()
    assert sim == SIM_STDOUT_SHA, f"paper sim output changed: {sim} != {SIM_STDOUT_SHA}"
    print(f"PASS check_paper_behaviour_golden: 4 条通道 paper 行为 golden sha {digest[:16]}… 一致；"
          f"paper_reversal_sim.py --scenarios-only stdout sha {sim[:16]}… 一致")


def _paper_probe_sleeve():
    log_path = str(TMP / "probe_sleeve.jsonl")
    cfg = {"mode": "paper", "strategy": {"sleeve_timeout_s": 60}, "log_path": log_path}
    sess = "paris|2026-09-13|high"
    cache = book_cache()
    cache.clear()
    cache["Y31"] = {"best_bid": "0.55", "best_ask": "0.60", "tick_size": "0.01",
                    "bids": [{"price": "0.55", "size": "100"}],
                    "asks": [{"price": "0.60", "size": "100"}]}
    state = {
        "paper_initial_capital_usdc": 700.0, "paper_total_debit_usdc": 15.0,
        "positions": {f"{sess}#sleeve": {
            "key": f"{sess}#sleeve", "kind": "sleeve", "settled": False,
            "legs": [{"leg": "buy_yes_sleeve", "token_id": "Y31", "side": "BUY", "outcome": "YES",
                      "shares": "25.0", "cost_usdc": "13.75", "avg_price": "0.55",
                      "bucket_id": "b31", "settled": False}]}},
        "weatherbotyes2re": {"sleeves": {sess: {"status": "open",
                                                "entered_at_utc": (NOW - timedelta(seconds=600)).isoformat(),
                                                "position_key": f"{sess}#sleeve"}},
                             "fired": {}}}
    _r_cycle.set_cycle_mode("paper", log_path)
    _r_cycle._expire_stale_sleeves(cfg, state, NOW)
    return {"state": state, "events": _rows(Path(log_path))}


def _paper_probe_refire():
    log_path = str(TMP / "probe_refire.jsonl")
    cfg = {"mode": "paper", "log_path": log_path}
    sess = "paris|2026-09-13|high"
    cache = book_cache()
    cache.clear()
    cache["Y31"] = {"best_bid": "0.52", "best_ask": "0.58", "tick_size": "0.01",
                    "neg_risk": True,
                    "bids": [{"price": "0.52", "size": "100"}],
                    "asks": [{"price": "0.58", "size": "100"}],
                    "fetched_at_epoch": NOW.timestamp()}
    state = {
        "paper_initial_capital_usdc": 700.0, "paper_total_debit_usdc": 15.0,
        "positions": {sess: {
            "key": sess, "kind": "consensus_lock", "city_id": "paris", "direction": "high",
            "settled": False, "fires_at_utc": NOW.isoformat(),
            "legs": [{"leg": "buy_yes_lock", "token_id": "Y31", "side": "BUY", "outcome": "YES",
                      "shares": "25.0", "cost_usdc": "15.0", "avg_price": "0.60",
                      "bucket_id": "b31", "settled": False}]}},
        "weatherbotyes2re": {"fired": {}, "armed": {}, "running_extremes": {},
                             "last_obs_time": {}}}
    fire = {"key": sess, "kind": "consensus_lock", "city_id": "paris", "icao": "LFPB",
            "market_local_date": "2026-09-13", "direction": "high", "jump": 1,
            "ref_source": "taf", "fire_no": 2, "entry_channel": None, "next_entry_window": None}
    new_pos = {"key": sess, "legs": [
        {"leg": "buy_no_broken", "token_id": "N32", "side": "BUY", "outcome": "NO",
         "shares": "10.0", "cost_usdc": "7.5", "avg_price": "0.75", "bucket_id": "b32",
         "settled": False},
        {"leg": "buy_yes_new", "token_id": "Y32", "side": "BUY", "outcome": "YES",
         "shares": "10.0", "cost_usdc": "3.0", "avg_price": "0.30", "bucket_id": "b33",
         "settled": False}]}
    _r_cycle.set_cycle_mode("paper", log_path)
    _r_cycle.record_refire(cfg, state, fire, new_pos, [], NOW, fire_path="core")
    return {"state": state, "events": _rows(Path(log_path))}


# ===========================================================================================
# 11) 4 条通道在 live 下**全部**走真实卖单（桩 transport 计数 == 4）
# ===========================================================================================
RC_CITY = {"city_id": "paris", "icao": "LFPB", "timezone": "Europe/Paris", "market_unit": "C",
           "name": "Paris", "offset": "+0200"}

#: 最近一次 ``_rc_scenario`` 的策略侧状态（规则⑧：止损/破位后当日禁止再开仓）。
#: 刻意**不**放进返回值 —— 返回值参与 paper golden 的 sha，任何加法都会破坏逐字比对。
RC_STRAT_STATE: dict = {}


def _rc_scenario(kind, *, mode="paper", channel=None, transport=None, entry_age_seconds=7200,
                 log_tag=None, metar_temp=32.5, cycles=1):
    """真 ``run_cycle`` 驱动 早停 / 破位 两条退场通道。

    ``kind='early_stop'``：持仓桶 best_bid 崩到 0.40（< 0.45）⇒ 抢先止损。
    ``kind='breach'``：METAR 32.5 把 b31 顶到 b32 ⇒ 破位风控清算。
    ``mode='live'`` 时必须给 ``channel``（``live.exit.LiveExitChannel``）。

    ``entry_age_seconds``（② 开仓冷静期，2026-09-13）：默认 7200（2h）⇒ 早停正常执行；传 0 可
    构造"刚入场"的仓位以验证冷静期抑制路径。
    ``metar_temp``：默认 32.5（能触发破位风控）；只想验早停一条路径时传 31.2（b31 内 ⇒ 无破位）。
    """
    from zoneinfo import ZoneInfo
    now = NOW
    local_date = now.astimezone(ZoneInfo(RC_CITY["timezone"])).date().isoformat()
    rule_key = f"{RC_CITY['city_id']}|{local_date}|high"
    buckets = [
        {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "Y30", "no_token_id": "N30"},
        {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "Y31", "no_token_id": "N31"},
        {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "Y32", "no_token_id": "N32"},
    ]
    rule = {"city_id": RC_CITY["city_id"], "market_local_date": local_date, "direction": "high",
            "buckets": buckets, "enabled": True}
    cache = book_cache()
    cache.clear()
    y31_bid = "0.40" if kind == "early_stop" else "0.55"
    for tok, bid, ask in (("Y30", "0.01", "0.02"), ("N30", "0.90", "0.95"),
                          ("Y31", y31_bid, "0.60"), ("N31", "0.75", "0.80"),
                          ("Y32", "0.28", "0.30"), ("N32", "0.68", "0.72")):
        # ``neg_risk`` 只在 live 通道被解析（paper 平仓不读它）⇒ 对 paper golden 无影响
        cache[tok] = {"best_bid": bid, "best_ask": ask, "tick_size": "0.01", "neg_risk": True,
                      "bids": [{"price": bid, "size": "500"}],
                      "asks": [{"price": ask, "size": "500"}]}
    metar = {RC_CITY["icao"]: {"temp_c": metar_temp, "obs_time": now - timedelta(seconds=60),
                               "source": "test", "raw": "TEST 111200Z 32005KT 32/20"}}
    state = {
        "paper_initial_capital_usdc": 700.0, "paper_total_debit_usdc": 15.0,
        "positions": {rule_key: {
            "key": rule_key, "kind": "consensus_lock", "city_id": RC_CITY["city_id"],
            "direction": "high", "settled": False, "liquidated": False,
            # ② 开仓冷静期（2026-09-13）：入场时间默认放在冷静期之外（2h 前）——本场景验的是"门限破了就
            # 真出场"，而 fresh 仓会被抑制（由策略层用例与 check_early_stop_grace_suppression 覆盖）。
            # ``fires_at_utc`` 在 golden 里被 ``_norm`` 归一成 ``<ts>``，因此这里改数值**不**改变
            # paper 行为 golden 的 sha。
            "fires_at_utc": (now - timedelta(seconds=entry_age_seconds)).isoformat(),
            "legs": [{"leg": "buy_yes_lock", "token_id": "Y31", "side": "BUY", "outcome": "YES",
                      "shares": "25.0", "cost_usdc": "15.0", "avg_price": "0.60",
                      "bucket_id": "b31", "settled": False}]}},
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}}}
    log_path = str(TMP / f"rc_{kind}_{mode}_{log_tag if log_tag else id(channel)}.jsonl")
    cfg = {"mode": mode, "strategy_mode": "consensus_lock", "fire_budget_usdc": 10.0,
           "log_path": log_path, "market_ws_enabled": False, "ws_triggered_metar_enabled": False,
           "rules_refresh_interval_seconds": 10 ** 9, "idle_book_interval_seconds": 10 ** 9,
           "idle_metar_interval_seconds": 10 ** 9, "arm_metar_interval_seconds": 10 ** 9,
           "taf_refresh_interval_seconds": 10 ** 9, "settle_poll_seconds": 10 ** 9,
           "consensus_lock": {"filter_fast_stations_only": False, "early_stop_enabled": True,
                              "early_stop_bid_floor": "0.45",
                              "early_stop_next_bucket_surge": "0.35",
                              "max_fires_per_session": 2, "risk_control_no_cap": "0.85",
                              "risk_control_yes_cap": "0.75", "live_sell_enabled": True,
                              "live_sell_floor": "0.05",
                              "live_sell_max_attempts_per_cycle": 1}}
    _r_cycle._CONSENSUS_LOCK_STRAT = None
    saved = {}
    for attr, value in (("load_active_cities", lambda _cfg: [dict(RC_CITY)]),
                        ("target_dates_by_icao", lambda *a, **k: {}),
                        ("refresh_rules", lambda *a, **k: None),
                        ("_load_rule_cache", lambda: ({rule_key: dict(rule)}, {})),
                        ("prune_stale_sessions", lambda *a, **k: 0),
                        ("refresh_books", lambda *a, **k: {}),
                        ("_fetch_metar", lambda *a, **k: dict(metar)),
                        ("_fetch_taf", lambda *a, **k: {}),
                        ("consensus_entry_fire", lambda *a, **k: None),
                        ("_paper_fire", lambda *a, **k: (None, []))):
        saved[attr] = getattr(_r_cycle, attr)
        setattr(_r_cycle, attr, value)

    class _WS:
        running = False

        def start(self, *a, **k):
            pass

        def ensure_tokens(self, *a, **k):
            pass

        def telemetry(self):
            return {"running": False}

    saved["ws_bridge"] = _r_cycle.ws_bridge
    _r_cycle.ws_bridge = lambda: _WS()
    saved["get_channel"] = live_exit.get_channel
    if channel is not None:
        live_exit.get_channel = lambda cfg, **kw: channel
    _r_cycle._LAST_GOOD_METAR.clear()
    _r_cycle._LAST_GOOD_TAF.clear()
    try:
        for _c in range(cycles):
            _r_cycle.run_cycle(cfg, state, now, force_metar=True, force_books=True, force_rules=True)
        strat_obj = _r_cycle._CONSENSUS_LOCK_STRAT
        RC_STRAT_STATE.clear()
        if strat_obj is not None:
            RC_STRAT_STATE.update({
                "stopped_out": sorted(strat_obj.state.stopped_out_sessions),
                "breached": sorted(strat_obj.state.breached_sessions)})
    finally:
        for attr, value in saved.items():
            if attr == "get_channel":
                live_exit.get_channel = value
            else:
                setattr(_r_cycle, attr, value)
        _r_cycle._CONSENSUS_LOCK_STRAT = None
    keys = ("early_stop_loss", "early_stop_suppressed", "breach_risk_control", "close_old_yes",
            "sleeve_timeout", "fire", "exit_deferred", "live_exit")
    rows = [r for r in _rows(Path(log_path)) if r.get("type") in keys]
    return {"state": {k: v for k, v in state.items() if k != "paper_initial_capital_usdc"},
            "rows": rows}


def check_all_four_channels_route_to_real_sell():
    """4 条通道在 live 下**每一条**都走真实 FAK 卖（桩 transport 计数累计恰 4）。"""
    audit = _audit("four-channels")
    sent_total = 0
    channels_seen = []

    # (1) sleeve 超时
    t1 = ExitTransport(client=_client(matched="25", size="25", price="0.55"))
    chan1 = _channel(t1, audit=audit)
    log_path = str(TMP / "four_sleeve.jsonl")
    # 显式开启真实卖出（代码默认已改 fail-safe=False）；本用例测的是 4 条通道**开启后**都走真实卖
    cfg = {"mode": "live", "strategy": {"sleeve_timeout_s": 60}, "log_path": log_path,
           "consensus_lock": {"live_sell_enabled": True, "live_sell_floor": "0.05",
                              "live_sell_max_attempts_per_cycle": 1}}
    sess = "paris|2026-09-13|high"
    cache = book_cache()
    cache.clear()
    cache["Y31"] = {"best_bid": "0.55", "best_ask": "0.60", "tick_size": "0.01",
                    "neg_risk": True,
                    "bids": [{"price": "0.55", "size": "100"}],
                    "asks": [{"price": "0.60", "size": "100"}]}
    state = {"paper_initial_capital_usdc": 700.0, "paper_total_debit_usdc": 15.0,
             "positions": {f"{sess}#sleeve": {
                 "key": f"{sess}#sleeve", "kind": "sleeve", "settled": False,
                 "legs": [{"leg": "buy_yes_sleeve", "token_id": "Y31", "side": "BUY",
                           "outcome": "YES", "shares": "25.0", "cost_usdc": "13.75",
                           "avg_price": "0.55", "bucket_id": "b31", "settled": False}]}},
             "weatherbotyes2re": {"sleeves": {sess: {"status": "open",
                                                     "entered_at_utc": (NOW - timedelta(seconds=600)).isoformat(),
                                                     "position_key": f"{sess}#sleeve"}},
                                  "fired": {}}}
    saved_get = live_exit.get_channel
    live_exit.get_channel = lambda cfg, **kw: chan1
    try:
        _r_cycle.set_cycle_mode("live", log_path)
        _r_cycle._expire_stale_sleeves(cfg, state, NOW)
    finally:
        live_exit.get_channel = saved_get
    assert len(t1.sent("SELL")) == 1, t1.calls
    sent_total += len(t1.sent("SELL"))
    channels_seen.append(t1.sent("SELL")[0]["exit_channel"])
    assert t1.sent("SELL")[0]["floor"] == Decimal("0.05"), t1.sent("SELL")[0]
    pos1 = state["positions"][f"{sess}#sleeve"]
    assert pos1["legs"][0]["settled"] is True and pos1["settled"] is True, pos1

    # (2) 追火旧桶清算
    t2 = ExitTransport(client=_client(matched="25", size="25", price="0.52"))
    chan2 = _channel(t2, audit=audit)
    log2 = str(TMP / "four_refire.jsonl")
    cfg2 = {"mode": "live", "log_path": log2,
            "consensus_lock": {"live_sell_enabled": True, "live_sell_floor": "0.05",
                               "live_sell_max_attempts_per_cycle": 1}}
    cache.clear()
    cache["Y31"] = {"best_bid": "0.52", "best_ask": "0.58", "tick_size": "0.01",
                    "neg_risk": True,
                    "asks": [{"price": "0.58", "size": "100"}],
                    "fetched_at_epoch": NOW.timestamp()}
    st2 = {"paper_initial_capital_usdc": 700.0, "paper_total_debit_usdc": 15.0,
           "positions": {sess: {"key": sess, "kind": "consensus_lock", "city_id": "paris",
                                "direction": "high", "settled": False,
                                "fires_at_utc": NOW.isoformat(),
                                "legs": [{"leg": "buy_yes_lock", "token_id": "Y31", "side": "BUY",
                                          "outcome": "YES", "shares": "25.0", "cost_usdc": "15.0",
                                          "avg_price": "0.60", "bucket_id": "b31",
                                          "settled": False}]}},
           "weatherbotyes2re": {"fired": {}, "armed": {}, "running_extremes": {},
                                "last_obs_time": {}}}
    fire = {"key": sess, "kind": "consensus_lock", "city_id": "paris", "icao": "LFPB",
            "market_local_date": "2026-09-13", "direction": "high", "jump": 1,
            "ref_source": "taf", "fire_no": 2, "entry_channel": None, "next_entry_window": None}
    new_pos = {"key": sess, "legs": [
        {"leg": "buy_no_broken", "token_id": "N32", "side": "BUY", "outcome": "NO",
         "shares": "10.0", "cost_usdc": "7.5", "avg_price": "0.75", "bucket_id": "b32",
         "settled": False},
        {"leg": "buy_yes_new", "token_id": "Y32", "side": "BUY", "outcome": "YES",
         "shares": "10.0", "cost_usdc": "3.0", "avg_price": "0.30", "bucket_id": "b33",
         "settled": False}]}
    live_exit.get_channel = lambda cfg, **kw: chan2
    try:
        _r_cycle.set_cycle_mode("live", log2)
        _r_cycle.record_refire(cfg2, st2, fire, new_pos, [], NOW, fire_path="core")
    finally:
        live_exit.get_channel = saved_get
    assert len(t2.sent("SELL")) == 1, t2.calls
    sent_total += len(t2.sent("SELL"))
    channels_seen.append(t2.sent("SELL")[0]["exit_channel"])
    assert st2["positions"][sess]["legs"][0]["settled"] is True, st2["positions"][sess]["legs"][0]

    # (3)+(4) 早停 / 破位（真 run_cycle）
    for kind, ch_name in (("early_stop", "early_stop"), ("breach", "breach_rc")):
        t = ExitTransport(client=_client(matched="25", size="25", price="0.60"))
        chan = _channel(t, audit=audit)
        out = _rc_scenario(kind, mode="live", channel=chan)
        sells = t.sent("SELL")
        assert len(sells) == 1, (kind, t.calls)
        assert sells[0]["exit_channel"] == ch_name, (kind, sells[0])
        assert sells[0]["taker"] is True and sells[0]["clamp"] is False, sells
        sent_total += 1
        channels_seen.append(ch_name)
        pos = list(out["state"]["positions"].values())[0]
        assert pos["liquidated"] is True, (kind, pos)
        assert pos["legs"][0]["settled"] is True, (kind, pos["legs"][0])
        # 规则⑧：止损/破位后当日禁止再开仓 —— 沿用既有 session 熔断标记（逐字不变的行为）
        key = list(out["state"]["positions"].keys())[0]
        assert key in RC_STRAT_STATE.get("breached", []), RC_STRAT_STATE
        if kind == "early_stop":
            assert key in RC_STRAT_STATE.get("stopped_out", []), RC_STRAT_STATE
        if kind == "early_stop":
            assert pos.get("settled") is not True, "live 不得把仓位标 settled（余下 NO 腿交给 settle）"
            assert pos["liquidation_type"].startswith("bid_floor_broken"), pos
            assert any(r["type"] == "early_stop_loss" for r in out["rows"]), out["rows"]
        else:
            assert pos["liquidation_type"] == "METAR_BREACH", pos
            assert any(r["type"] == "breach_risk_control" for r in out["rows"]), out["rows"]
    assert sent_total == 4, sent_total
    assert sorted(channels_seen) == ["breach_rc", "early_stop", "refire_liq", "sleeve_timeout"], \
        channels_seen
    print("PASS check_all_four_channels_route_to_real_sell: 4 条通道（early_stop/refire_liq/"
          "sleeve_timeout/breach_rc）在 live 下各发 1 笔真实 FAK SELL（合计恰 4），"
          "腿 settled + 仓 liquidated，无 settled 误标，止损/破位后当日仍禁止再开仓（规则⑧）")


# ===========================================================================================
# 12) 不变式：live 下 close_leg_at_best_bid 不可达（运行时哨兵 + 续卖扫）
# ===========================================================================================
def check_virtual_close_guard():
    log_path = str(TMP / "guard_events.jsonl")
    if Path(log_path).exists():
        Path(log_path).unlink()
    _r_cycle.set_cycle_mode("live", log_path)
    state = _state()
    leg = _leg()
    try:
        _r_cycle.close_leg_at_best_bid(state, leg, {"Y31": _book("0.60")},
                                       closed_by="early_stop_loss")
    except _r_cycle.LiveVirtualCloseRefused as exc:
        assert "live" in str(exc), exc
    else:
        raise AssertionError("live 下对真实持仓的虚拟平仓必须被拒绝")
    assert leg.get("settled") is not True and leg["shares"] == "25", leg
    assert Decimal(str(state["paper_total_debit_usdc"])) == Decimal("15.0"), state
    rows = _rows(Path(log_path))
    assert rows and rows[-1]["type"] == "virtual_close_refused", rows
    # paper 模式：同一调用照常工作（守卫不改变既有行为）
    _r_cycle.set_cycle_mode("paper", log_path)
    st2, lg2 = _state(), _leg()
    got = _r_cycle.close_leg_at_best_bid(st2, lg2, {"Y31": _book("0.60")},
                                         closed_by="early_stop_loss")
    assert got and got["proceeds_usdc"] == "15.0000" and lg2["settled"] is True, (got, lg2)
    assert Decimal(str(st2["paper_total_debit_usdc"])) == Decimal("0.0"), st2
    # 已 settled / 零股数的腿在 live 下不会被哨兵误伤（语义：只有真实持仓才拦）
    _r_cycle.set_cycle_mode("live", log_path)
    dead = _leg(shares="0", settled=False)
    assert _r_cycle.close_leg_at_best_bid(_state(), dead, {}) is None
    already = _leg(settled=True)
    assert _r_cycle.close_leg_at_best_bid(_state(), already, {}) is None
    # 续卖扫：pending_exit 的余量在下一轮被真实卖出（且不是虚拟平仓）
    _r_cycle.set_cycle_mode("live", log_path)
    t = ExitTransport(client=_client(matched="10", size="10", price="0.58"))
    chan = _channel(t, audit=_audit("retry"))
    saved = live_exit.get_channel
    live_exit.get_channel = lambda cfg, **kw: chan
    cache = book_cache()
    cache.clear()
    cache["Y31"] = {"best_bid": "0.58", "best_ask": "0.62", "tick_size": "0.001",
                    "neg_risk": True,
                    "bids": [{"price": "0.58", "size": "100"}],
                    "asks": [{"price": "0.62", "size": "100"}]}
    yes = _leg(shares="10.0000")
    pos = _pos(yes)
    pos["pending_exit"] = {"Y31": {"token_id": "Y31", "leg": "buy_yes_lock",
                                   "exit_channel": "refire_liq", "shares": "10.0000"}}
    st3 = _state(debit="15.0")
    st3["positions"] = {pos["key"]: pos}
    try:
        _r_cycle._retry_pending_exits(_live_cfg(), st3, NOW)
    finally:
        live_exit.get_channel = saved
    assert len(t.sent("SELL")) == 1 and t.sent("SELL")[0]["exit_channel"] == "refire_liq", t.calls
    assert yes["settled"] is True and pos["liquidated"] is True, (yes, pos)
    assert "pending_exit" not in pos, pos
    # paper 模式：续卖扫整段不动作（paper 逐字不变）
    t_p = ExitTransport(client=_client(matched="10", price="0.58"))
    live_exit.get_channel = lambda cfg, **kw: _channel(t_p, audit=_audit("retry-paper"))
    yes_p = _leg(shares="10.0000")
    pos_p = _pos(yes_p)
    pos_p["pending_exit"] = {"Y31": {"token_id": "Y31", "exit_channel": "refire_liq"}}
    st_p = _state(debit="15.0")
    st_p["positions"] = {pos_p["key"]: pos_p}
    try:
        _r_cycle._retry_pending_exits({"mode": "paper", "log_path": log_path}, st_p, NOW)
    finally:
        live_exit.get_channel = saved
    assert t_p.calls == [] and yes_p["shares"] == "10.0000", (t_p.calls, yes_p)
    _r_cycle.set_cycle_mode("paper", log_path)
    print("PASS check_virtual_close_guard: live 下虚拟平仓被拒（记 virtual_close_refused，"
          "腿不 settled、账本不动）；paper 下逐字照常；续卖扫在 live 续卖、paper 不动作")


# ===========================================================================================
# 13) config + GOLDEN + 零真实订单
# ===========================================================================================
def check_config_keys_and_zero_real_orders():
    raw = json.loads((ROOT / "config" / "yes2re_reversal.json").read_text(encoding="utf-8"))
    block = raw["consensus_lock"]
    assert block["live_sell_enabled"] is False, block
    assert block["live_sell_floor"] == "0.05", block
    assert block["live_sell_max_attempts_per_cycle"] == 1, block
    cfg = _r_state.load_config(ROOT / "config" / "yes2re_reversal.json")
    assert cfg["consensus_lock"]["live_sell_floor"] == "0.05", cfg["consensus_lock"]
    import strategy_consensus_lock as scl
    for key, want in (("live_sell_enabled", False), ("live_sell_floor", Decimal("0.05")),
                      ("live_sell_max_attempts_per_cycle", 1)):
        assert scl.DEFAULT_CONFIG[key] == want, (key, scl.DEFAULT_CONFIG[key])
    # live/exit.py 的代码默认必须与 DEFAULT_CONFIG 一致（防配置漂移）
    assert live_exit.DEFAULT_LIVE_SELL_ENABLED is scl.DEFAULT_CONFIG["live_sell_enabled"]
    assert live_exit.parse_floor(live_exit.DEFAULT_LIVE_SELL_FLOOR) == \
        scl.DEFAULT_CONFIG["live_sell_floor"]
    assert live_exit.DEFAULT_LIVE_SELL_MAX_ATTEMPTS_PER_CYCLE == \
        scl.DEFAULT_CONFIG["live_sell_max_attempts_per_cycle"]
    assert live_exit.DEFAULT_TAKER_FEE_RATE == cfg["base_fee_rate"] == "0.02"
    st = live_exit.exit_settings({})
    assert st["enabled"] is False and st["floor"] == Decimal("0.05") and st["max_attempts"] == 1, st
    # ① 事故回归守卫（2026-09-13）：**缺键必须等价于关闭（fail-safe）**，绝不能回落成"开"。
    # 背景：r94 覆盖 config 时删掉了 live_sell_* 三键，当时代码默认是 True ⇒ 重启即启用真实卖出。
    plan_missing = live_exit.plan_live_exit(mode="live", settings=st, best_bid="0.60",
                                            gates=GATES_OK, attempts=0, shares=Decimal("25"))
    assert plan_missing["action"] == "defer" and plan_missing["reason"] == live_exit.REASON_DISABLED, \
        plan_missing
    assert json.loads((ROOT / "config" / "yes2re_reversal.json").read_text(
        encoding="utf-8"))["consensus_lock"]["live_sell_enabled"] is False, "config 也必须显式关闭"
    # 关闭开关 ⇒ 只延期（fail-closed），绝不虚拟平仓
    st_off = live_exit.exit_settings({"consensus_lock": {"live_sell_enabled": False}})
    plan_off = live_exit.plan_live_exit(mode="live", settings=st_off, best_bid="0.60",
                                        gates=GATES_OK, attempts=0, shares=Decimal("25"))
    assert plan_off["action"] == "defer" and plan_off["reason"] == live_exit.REASON_DISABLED, plan_off
    st_att = live_exit.exit_settings({"consensus_lock": {"live_sell_enabled": True,
                                                         "live_sell_max_attempts_per_cycle": 1}})
    assert live_exit.plan_live_exit(mode="live", settings=st_att, best_bid="0.60",
                                    gates=GATES_OK, attempts=1,
                                    shares=Decimal("25"))["reason"] == \
        live_exit.REASON_ATTEMPTS
    # 零真实订单：本测试的每一次下单都在桩上，绝不向真实环境发单
    pass
    print("PASS check_config_keys_and_zero_real_orders: config/DEFAULT_CONFIG/代码默认三处一致"
          "（enabled=false（fail-safe）/ floor=0.05 / attempts=1 / fee 0.02）；缺键⇒关闭；关闭开关只延期；零真实订单")


# ===========================================================================================
def check_max_attempts_per_cycle_is_real():
    """``live_sell_max_attempts_per_cycle``（默认 1）是**真**上限：同一轮同一腿第二次不发单。

    计数只认"真实下单"，且只在 live 下写状态（``live_exit_cycle`` / ``live_exit_attempts``）
    ⇒ paper 的 state/golden 一个键都不多（paper golden 检查在本文件里已单独把关）。
    """
    audit = _audit("attempts")
    cfg = _live_cfg()
    leg = _leg(shares="25")
    state, pos = _state(), _pos(leg)
    # 第一轮：第 1 次尝试 ⇒ 发单
    t1 = ExitTransport(client=_client(matched="10", size="25", price="0.60"))
    live_exit.bump_cycle(state)
    r1 = live_exit.live_exit_leg(cfg=cfg, state=state, leg=leg, pos=pos,
                                 books={"Y31": _book("0.60")}, channel=live_exit.CH_BREACH_RC,
                                 now_utc=NOW, channel_obj=_channel(t1, audit=audit))
    assert r1["sold"] is True and len(t1.orders()) == 1, r1
    assert live_exit.attempts_this_cycle(state, "Y31") == 1, state
    # 同一轮再来一次（例如 续卖扫 + sleeve 超时 同轮都命中）⇒ 被上限挡住，零发单
    t2 = ExitTransport(client=_client(matched="15", size="15", price="0.60"))
    r2 = live_exit.live_exit_leg(cfg=cfg, state=state, leg=leg, pos=pos,
                                 books={"Y31": _book("0.60")}, channel=live_exit.CH_SLEEVE_TIMEOUT,
                                 now_utc=NOW, channel_obj=_channel(t2, audit=audit))
    assert r2["deferred"] is True and r2["reason"] == live_exit.REASON_ATTEMPTS, r2
    assert t2.orders() == [] and t2.sent("SELL") == [], t2.calls
    assert leg["shares"] == "15.0000", leg          # 第一轮的成交已按真实股数落账
    # 下一轮（run_cycle 会 bump）⇒ 计数归零、可以继续卖余量
    t3 = ExitTransport(client=_client(matched="15", size="15", price="0.58"))
    live_exit.bump_cycle(state)
    r3 = live_exit.live_exit_leg(cfg=cfg, state=state, leg=leg, pos=pos,
                                 books={"Y31": _book("0.58")}, channel=live_exit.CH_BREACH_RC,
                                 now_utc=NOW, channel_obj=_channel(t3, audit=audit))
    assert r3["sold"] is True and len(t3.orders()) == 1, r3
    assert leg["settled"] is True and pos["liquidated"] is True, (leg, pos)
    # 上限可配（2 ⇒ 同轮两次都发）
    st2 = _state()
    leg2 = _leg(shares="25")
    pos2 = _pos(leg2)
    cfg2 = _live_cfg(consensus_lock={"live_sell_enabled": True, "live_sell_floor": "0.05",
                                     "live_sell_max_attempts_per_cycle": 2})
    live_exit.bump_cycle(st2)
    for _ in range(2):
        r = live_exit.live_exit_leg(cfg=cfg2, state=st2, leg=leg2, pos=pos2,
                                    books={"Y31": _book("0.60")}, channel=live_exit.CH_BREACH_RC,
                                    now_utc=NOW,
                                    channel_obj=_channel(ExitTransport(
                                        client=_client(matched="10", size="25", price="0.60")),
                                        audit=audit))
        assert r["sold"] is True, r
    assert live_exit.attempts_this_cycle(st2, "Y31") == 2, st2
    # paper 模式不写这两个键（paper golden 逐字不变的机制保证）
    assert live_exit.attempts_this_cycle(_state(), "Y31") == 0
    print("PASS check_max_attempts_per_cycle_is_real: 默认 1 ⇒ 同轮第二次 defer:attempts_exhausted"
          "（零发单），下一轮恢复；可配 2 ⇒ 同轮两次都发；计数键只在 live 写")


def check_early_stop_grace_suppression():
    """② 引擎级：fresh 仓 + 买盘破位 ⇒ **不执行**止损（不卖、不平仓、不熔断），但必须落审计。

    与 ``check_all_four_channels_route_to_real_sell`` 相对照：那里的入场时间在冷静期之外 ⇒ 正常真卖；
    这里把 ``entry_age_seconds=0`` ⇒ 引擎收到策略的 ``early_stop_suppressed`` 事件后**只记审计**。
    """
    # (a) paper：被抑制 ⇒ 无 early_stop_loss 行、有 early_stop_suppressed 行、仓**未被早停**平掉。
    #     ``metar_temp=31.2``（b31 内）⇒ 只走早停一条路径，破位风控不干扰本用例。
    fresh = _rc_scenario("early_stop", mode="paper", entry_age_seconds=0, log_tag="grace-fresh",
                         metar_temp=31.2)
    rows = fresh["rows"]
    assert not [r for r in rows if r.get("type") == "early_stop_loss"], rows
    sup = [r for r in rows if r.get("type") == "early_stop_suppressed"]
    assert len(sup) == 1, rows
    body = sup[0]["suppressed"]
    assert body["action"] == "early_stop_suppressed", body
    assert "bid_floor_broken" in body["would_be_reason"], body
    assert body["session_key"] and body["entry_channel"] in ("target_bucket", "next_bucket"), body
    assert body["bid"] and body["floor"] and body["remaining_grace_seconds"] == 1200, body
    pos = list(fresh["state"]["positions"].values())[0]
    assert pos.get("settled") is not True and pos.get("liquidated") is not True, pos
    assert not RC_STRAT_STATE.get("stopped_out"), RC_STRAT_STATE

    # (b) live：被抑制 ⇒ 零 SELL（不卖真币），仓仍 open
    audit = _audit("grace-suppress")
    t = ExitTransport(client=_client(matched="25", size="25", price="0.40"))
    chan = _channel(t, audit=audit)
    live_fresh = _rc_scenario("early_stop", mode="live", channel=chan, entry_age_seconds=0,
                              log_tag="grace-fresh-live", metar_temp=31.2)
    assert t.sent("SELL") == [], t.calls
    assert [r for r in live_fresh["rows"] if r.get("type") == "early_stop_suppressed"], live_fresh["rows"]
    assert not [r for r in live_fresh["rows"] if r.get("type") == "early_stop_loss"], live_fresh["rows"]
    lpos = list(live_fresh["state"]["positions"].values())[0]
    assert lpos.get("liquidated") is not True and lpos.get("settled") is not True, lpos

    # (c) 对照：同场景但入场在冷静期之外 ⇒ 真的早停出场（行为与 (a)(b) 形成闭环）
    aged = _rc_scenario("early_stop", mode="paper", entry_age_seconds=10 ** 6, log_tag="grace-aged",
                        metar_temp=31.2)
    assert [r for r in aged["rows"] if r.get("type") == "early_stop_loss"], aged["rows"]
    assert not [r for r in aged["rows"] if r.get("type") == "early_stop_suppressed"], aged["rows"]
    apos = list(aged["state"]["positions"].values())[0]
    assert apos.get("settled") is True, apos
    assert apos["legs"][0].get("closed_by") == "early_stop_loss", apos["legs"][0]
    print("PASS check_early_stop_grace_suppression: fresh 仓破位 ⇒ 引擎只记 early_stop_suppressed"
          "（零卖单/未平仓/未熔断，含 would-be reason + 剩余冷静期）；入场在冷静期外 ⇒ 正常早停出场")


def check_grace_dedupe_across_cycles():
    """② 引擎级去重（独立审计 M2）：同一冷静期窗口内**多轮只记一行**，且第 2 轮绝不误触发平仓。

    没有这条用例时 ``_r_cycle`` 会每轮无条件写 ``early_stop_suppressed`` —— 1200s × ~20s/轮
    会产生数十行噪声，把审计信号本身掩掉（且让策略的 ``duplicate`` 标志沦为死代码）。
    """
    two = _rc_scenario("early_stop", mode="paper", entry_age_seconds=0, log_tag="grace-dedupe",
                       metar_temp=31.2, cycles=2)
    rows = two["rows"]
    sup = [r for r in rows if r.get("type") == "early_stop_suppressed"]
    assert len(sup) == 1, f"两轮（同一冷静期）应只记 1 行，实得 {len(sup)} 行"
    assert not [r for r in rows if r.get("type") == "early_stop_loss"], \
        "去重只应抑制日志，绝不能让第 2 轮掉进平仓分支"
    pos = list(two["state"]["positions"].values())[0]
    assert pos.get("liquidated") is not True and pos.get("settled") is not True, pos
    assert not RC_STRAT_STATE.get("stopped_out"), RC_STRAT_STATE
    print("PASS check_grace_dedupe_across_cycles: 两轮冷静期内只记 1 行 early_stop_suppressed"
          "（第 2 轮 duplicate=True 被去重）；未误触发平仓/熔断")


def check_below_venue_min_order_size():
    """股数 < venue ``min_order_size`` ⇒ **本地弃单**（零签名 / 零真单 / 零尝试计数），仓位保持 open。

    背景（2026-09-13 r104 实盘实测）：``buenos-aires|2026-09-13|high`` 早停部分成交后，账面余量 =
    腿股数 15.814063 − venue 回报成交 15.81 = **0.0041 股**，而该盘口 ``min_order_size`` = 5
    ⇒ 每轮真实提交、每轮被交易所 400 ``invalid maker amount`` 拒（30 次 / 11 min，永不停）——
    与入场侧 F-D 同一根因（计划股数 < venue 最小量 ⇒ 注定被拒），故同一 reason 字面量、同一 fail-open 口径。
    """
    # (1) 解析器：缺失 / 非法 / <= 0 / 非盘口 ⇒ None（= 不知道最小量 ⇒ 守卫不生效）
    assert live_exit.book_min_order_size({"min_order_size": "5"}) == Decimal("5")
    for book in ({}, {"min_order_size": None}, {"min_order_size": ""}, {"min_order_size": "0"},
                 {"min_order_size": "abc"}, {"min_order_size": "-5"}, None, "not-a-book"):
        assert live_exit.book_min_order_size(book) is None, book
    settings = live_exit.exit_settings({"consensus_lock": {"live_sell_enabled": True,
                                                           "live_sell_floor": "0.05"}})

    def _plan(shares, minimum):
        return live_exit.plan_live_exit(mode="live", settings=settings, best_bid="0.07",
                                        gates=GATES_OK, attempts=0,
                                        shares=Decimal(shares) if shares is not None else None,
                                        min_order_size=minimum)

    # (2) 纯决策边界：< min ⇒ 弃；== min ⇒ 卖（下界含）；> min ⇒ 卖；min 未知 ⇒ 维持原行为（卖）
    low = _plan("0.0041", "5")
    assert low["action"] == live_exit.ACTION_DEFER and low["reason"] == live_exit.REASON_BELOW_MIN, low
    assert low["min_order_size"] == Decimal("5"), low
    assert _plan("4.9999", "5")["action"] == live_exit.ACTION_DEFER, _plan("4.9999", "5")
    for shares in ("5", "5.0001", "25"):
        ok = _plan(shares, "5")
        assert ok["action"] == live_exit.ACTION_SELL, (shares, ok)
    unknown = _plan("0.0041", None)
    assert unknown["action"] == live_exit.ACTION_SELL, unknown
    assert _plan("0.0041", "abc")["action"] == live_exit.ACTION_SELL, _plan("0.0041", "abc")
    # 顺序：仍受地板/闸门约束（弃单理由是 below_min_order_size 而不是别的）
    assert live_exit.plan_live_exit(mode="live", settings=settings, best_bid="0.07",
                                    gates=GATES_OK, attempts=0, shares=Decimal("0"),
                                    min_order_size="5")["reason"] == live_exit.REASON_NO_SHARES

    # (3) 端到端（真 execute_leg + 桩交易所）：余量 < min ⇒ 连下单口都不进、零真单、仓仍 open
    audit = _audit("below-min")
    t_low = ExitTransport(client=_client(matched="0.0041", size="0.0041", price="0.07"))
    leg_low = _leg(shares="0.0041", token="Y31", cost="5.0605")
    state_low, pos_low = _state(), _pos(leg_low)
    pos_low["pending_exit"] = {"Y31": {"token_id": "Y31", "leg": leg_low["leg"],
                                       "exit_channel": live_exit.CH_EARLY_STOP,
                                       "shares": "0.0041", "sell_floor": "0.05",
                                       "last_reason": live_exit.REASON_DEPTH}}
    book_low = {**_book("0.07"), "min_order_size": "5"}
    events_low: list[dict] = []
    r_low = live_exit.live_exit_leg(cfg=_live_cfg(), state=state_low, leg=leg_low, pos=pos_low,
                                    books={"Y31": book_low}, channel=live_exit.CH_EARLY_STOP,
                                    now_utc=NOW, channel_obj=_channel(t_low, audit=audit),
                                    log=events_low.append)
    assert r_low["deferred"] is True and r_low["sold"] is False, r_low
    assert r_low["reason"] == live_exit.REASON_BELOW_MIN, r_low
    assert t_low.calls == [] and t_low.orders() == [], t_low.calls   # 零 execute_leg、零 post_order
    assert live_exit.attempts_this_cycle(state_low, "Y31") == 0, state_low
    assert leg_low["shares"] == "0.0041" and leg_low.get("settled") is not True, leg_low
    assert pos_low.get("liquidated") is not True and "exit_fills" not in leg_low, (pos_low, leg_low)
    assert Decimal(str(state_low["paper_total_debit_usdc"])) == Decimal("15.0"), state_low
    # 余量一分未丢：pending_exit 仍在队列（交给 settle 兜底，绝不虚拟平仓、绝不静默丢弃）
    assert pos_low["pending_exit"]["Y31"]["shares"] == "0.0041", pos_low["pending_exit"]
    # 审计面：弃单必须留痕（机器可读 reason；不是静默跳过）
    deferred = [e for e in events_low if e.get("type") == "exit_deferred"]
    assert [e["reason"] for e in deferred] == [live_exit.REASON_BELOW_MIN], events_low
    assert deferred[0]["shares"] == "0.0041" and deferred[0]["sell_floor"] == "0.05", deferred[0]
    assert _rows(audit) == [], _rows(audit)   # 零发单 ⇒ 审计面没有 submit/deny 行

    # (4) 对照组：>= min 的正常余量照旧真实卖出（守卫不得挡住真实退场）
    t_ok = ExitTransport(client=_client(matched="25", size="25", price="0.60"))
    leg_ok = _leg(shares="25")
    r_ok = live_exit.live_exit_leg(cfg=_live_cfg(), state=_state(), leg=leg_ok, pos=_pos(leg_ok),
                                   books={"Y31": {**_book("0.60"), "min_order_size": "5"}},
                                   channel=live_exit.CH_EARLY_STOP, now_utc=NOW,
                                   channel_obj=_channel(t_ok, audit=audit))
    assert r_ok["sold"] is True and len(t_ok.orders()) == 1, (r_ok, t_ok.calls)

    # (5) 下单层防御纵深：直接调 sell_leg 也被同一条边界挡住（零签名、零发单）
    t_deep = ExitTransport(client=_client(matched="0.0041", size="0.0041", price="0.07"))
    deep = _channel(t_deep, audit=audit).sell_leg(leg=leg_low, book=book_low,
                                                  floor=Decimal("0.05"), shares=Decimal("0.0041"),
                                                  channel=live_exit.CH_EARLY_STOP)
    assert deep["ok"] is False and deep["status"] == live_exit.REASON_BELOW_MIN, deep
    assert t_deep.calls == [] and t_deep.orders() == [], t_deep.calls
    print("PASS check_below_venue_min_order_size: 余量 0.0041 股 < min_order_size 5 ⇒ 零发单"
          "（零 execute_leg/零 post_order/零尝试计数），仓保持 open 且余量留在 pending_exit；"
          "边界 == min 仍卖、min 未知守卫不生效、对照组 25 股照常卖出")


CHECKS = [
    ("floor matrix 0.049/0.050/0.051 (defer/sell/sell)", check_floor_matrix),
    ("floor missing/0/negative/1.5 ⇒ refuse, zero orders", check_floor_missing_or_invalid),
    ("partial fill ⇒ keep remainder, second round clears", check_partial_fill_two_rounds),
    ("refusal/no-bid/no-depth ⇒ exit_deferred, position stays open", check_defer_keeps_position_open),
    ("three gates required; entry caps never block a sell", check_three_gates_and_no_entry_caps),
    ("cancel precedes sell (order asserted)", check_cancel_precedes_sell),
    ("2% taker fee + net proceeds ledger", check_fee_and_net_proceeds),
    ("neg-risk resolution reused (refetch, else refuse)", check_neg_risk_resolution),
    ("audit side/exit_channel/sell_floor/leg_window unforgeable", check_audit_fields_unforgeable),
    ("paper branches verbatim + AST invariant", check_paper_branch_source_unchanged),
    ("paper behaviour golden + sim sha unchanged", check_paper_behaviour_golden),
    ("all 4 channels route to a real SELL (count == 4)", check_all_four_channels_route_to_real_sell),
    ("live virtual close unreachable (guard + retry sweep)", check_virtual_close_guard),
    ("config keys/GOLDEN consistent + zero real orders", check_config_keys_and_zero_real_orders),
    ("max attempts per cycle is a real cap", check_max_attempts_per_cycle_is_real),
    ("early stop grace period suppresses fresh-position stop (②)", check_early_stop_grace_suppression),
    ("grace dedupe: two cycles ⇒ one audit row, no false stop (②/M2)", check_grace_dedupe_across_cycles),
    ("below venue min_order_size ⇒ local refuse, zero orders (r104)",
     check_below_venue_min_order_size),
]


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — the runner reports everything
            failed += 1
            import traceback
            print(f"FAIL {name}: {type(exc).__name__}: {str(exc)[:600]}")
            traceback.print_exc(limit=3)
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
