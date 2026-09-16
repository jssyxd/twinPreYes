"""tests_cycle_consensus_lock.py — Integration test for consensus_lock execution in _r_cycle.py"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, ".")
import _r_cycle
from paper_capital import initial_capital_usdc, remaining_capital_usdc

def test_paper_fire_buy_yes_lock():
    cfg = {
        "mode": "paper",
        "strategy_mode": "consensus_lock",
        "fire_budget_usdc": 15.0,
        "log_path": "data/test_events.jsonl",
    }
    state = {
        "paper_initial_capital_usdc": 700.0,
        "positions": {},
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
    }
    now = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    
    # Mock orderbook in cache
    tok_id = "YES-PARIS-31"
    _r_cycle.book_cache()[tok_id] = {
        "best_ask": "0.60",
        "best_bid": "0.58",
        "tick_size": "0.01",
        "asks": [{"price": "0.60", "size": "100"}],
        "bids": [{"price": "0.58", "size": "100"}],
    }

    fire = {
        "key": "paris|2026-09-10|high",
        "kind": "consensus_lock",
        "city_id": "paris",
        "icao": "LFPB",
        "market_local_date": "2026-09-10",
        "direction": "high",
        "ref_extreme": 31.0,
        "ref_source": "market_rank1",
        "running_extreme": 31.2,
        "jump": 0,
        "target_bucket_id": "b31",
        "local_fire_time": "2026-09-10T16:00:00+02:00",
        "market_unit": "C",
        "fire_no": 1,
        "budget_usdc": "15.0",
        "legs": [
            {
                "leg": "buy_yes_lock",
                "token_id": tok_id,
                "side": "BUY",
                "outcome": "YES",
                "cap": "0.75",
                "floor": "0.45",
                "notional_pct": "1.0",
                "bucket_id": "b31",
                "bucket_lo": 31.0,
                "bucket_hi": 32.0,
                "bucket_label": "31°C",
            }
        ],
        "action_type": "re_fire",
    }

    pos, ladlog = _r_cycle._paper_fire(cfg, state, fire, now)
    assert pos is not None, "Position should be filled"
    assert pos["kind"] == "consensus_lock"
    assert len(pos["legs"]) == 1
    leg = pos["legs"][0]
    assert leg["leg"] == "buy_yes_lock"
    assert leg["token_id"] == tok_id
    assert leg["bucket_id"] == "b31"
    assert Decimal(leg["shares"]) > 0
    assert Decimal(leg["cost_usdc"]) > 0
    
    # Record fire event into state
    _r_cycle._record_fire_event(cfg, state, fire, pos, ladlog, now)
    assert "paris|2026-09-10|high" in state["positions"]
    print("PASS: test_paper_fire_buy_yes_lock")


def test_early_stop_loss_liquidation():
    cfg = {
        "mode": "paper",
        "strategy_mode": "consensus_lock",
        "fire_budget_usdc": 15.0,
        "log_path": "data/test_events.jsonl",
        "consensus_lock": {
            "early_stop_enabled": True,
            "early_stop_next_bucket_surge": "0.35",
            "early_stop_bid_floor": "0.45",
        }
    }
    state = {
        "paper_initial_capital_usdc": 700.0,
        "paper_total_debit_usdc": 15.0,
        "positions": {
            "paris|2026-09-10|high": {
                "key": "paris|2026-09-10|high",
                "kind": "consensus_lock",
                "city_id": "paris",
                "direction": "high",
                "settled": False,
                "legs": [
                    {
                        "leg": "buy_yes_lock",
                        "token_id": "YES-PARIS-31",
                        "side": "BUY",
                        "outcome": "YES",
                        "shares": "25.0",
                        "cost_usdc": "15.0",
                        "avg_price": "0.60",
                        "bucket_id": "b31",
                        "settled": False,
                    }
                ]
            }
        },
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
    }
    
    # Simulated books where next bucket surges to 0.40 (>0.35 threshold)
    books = {
        "YES-PARIS-31": {"best_bid": "0.50", "best_ask": "0.55"},
        "YES-PARIS-32": {"best_bid": "0.38", "best_ask": "0.40"},
    }
    buckets = [
        {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "YES-PARIS-30"},
        {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "YES-PARIS-31"},
        {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "YES-PARIS-32"},
    ]
    
    strat = _r_cycle._get_consensus_lock_strat(cfg)
    now = datetime(2026, 9, 10, 14, 5, 0, tzinfo=timezone.utc)
    
    # Sync strat state
    strat.state.open_positions["paris|2026-09-10|high"] = _r_cycle.PositionRecord(
        session_key="paris|2026-09-10|high",
        bucket_id="b31",
        yes_token_id="YES-PARIS-31",
        shares=Decimal("25.0"),
        cost_usdc=Decimal("15.0"),
        avg_price=Decimal("0.60"),
        entry_ts_utc=now.isoformat(),
        liquidated=False,
    )
    
    early_stop = strat.evaluate_early_stop_loss(
        "paris|2026-09-10|high", "high", buckets, books, now
    )
    assert early_stop is not None
    assert "next_bucket_surge" in early_stop["reason"]

    # Close leg via close_leg_at_best_bid
    pos = state["positions"]["paris|2026-09-10|high"]
    leg = pos["legs"][0]
    res = _r_cycle.close_leg_at_best_bid(state, leg, books, closed_by="early_stop_loss")
    assert res is not None
    assert Decimal(res["proceeds_usdc"]) == Decimal("12.5000")  # 25 shares * 0.50 bid = 12.5 USDC
    # debit reduced from 15.0 to 2.5 (12.5 credited back)
    assert round(float(state["paper_total_debit_usdc"]), 4) == 2.5
    print("PASS: test_early_stop_loss_liquidation (12.5 / 15.0 = 83.3% capital preserved)")


def test_paper_fire_next_entry_channel_budget_and_audit():
    """并行通道"下一档桶廉价入场"：腿级窗口贯通到 FAK 梯子 + 预算隔离 + 审计字段（引擎层）。"""
    import json as _json
    import os as _os
    import tempfile
    log_path = _os.path.join(tempfile.mkdtemp(prefix="nb-cycle-"), "events.jsonl")
    cfg = {
        "mode": "paper",
        "strategy_mode": "consensus_lock",
        "fire_budget_usdc": 15.0,
        "log_path": log_path,
    }
    state: dict = {
        "paper_initial_capital_usdc": 700.0,
        "paper_total_debit_usdc": 0.0,
        "positions": {},
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
    }
    now = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    next_tok = "YES-PARIS-32"
    _r_cycle.book_cache()[next_tok] = {
        "best_ask": "0.25", "best_bid": "0.10", "tick_size": "0.01",
        "asks": [{"price": "0.25", "size": "100"}], "bids": [{"price": "0.10", "size": "100"}],
    }
    # 引擎对新通道 fire 的构造方式：budget_usdc = next_entry_budget_pct × fire 预算 = 7.5
    fire = {
        "key": "paris|2026-09-10|high",
        "kind": "consensus_lock",
        "city_id": "paris", "icao": "LFPB",
        "market_local_date": "2026-09-10", "direction": "high",
        "ref_extreme": 31.0, "ref_source": "market_rank1", "running_extreme": 31.2, "jump": 0,
        "target_bucket_id": "b32", "bucket_id": "b32",
        "local_fire_time": "2026-09-10T16:00:00+02:00", "market_unit": "C", "fire_no": 1,
        "budget_usdc": "7.50", "entry_channel": "next_bucket", "next_entry_window": "[0.20, 0.32]",
        "legs": [{
            "leg": "buy_yes_next", "token_id": next_tok, "side": "BUY", "outcome": "YES",
            "cap": "0.32", "floor": "0.20", "notional_pct": "1.0", "bucket_id": "b32",
            "bucket_lo": 32.0, "bucket_hi": 33.0, "bucket_label": "32°C",
            "entry_channel": "next_bucket",
        }],
        "action_type": "re_fire",
    }
    pos, ladlog = _r_cycle._paper_fire(cfg, state, fire, now)
    assert pos is not None, "新通道 fire 应成交"
    assert pos["entry_channel"] == "next_bucket" and pos["next_entry_window"] == "[0.20, 0.32]"
    assert pos["budget_usdc"] == "7.50", pos["budget_usdc"]      # 预算隔离：只用 0.5 × fire 预算
    leg = pos["legs"][0]
    assert leg["leg"] == "buy_yes_next" and leg["bucket_id"] == "b32"
    # 引擎按既有 size_legs 约定 sizing：notional / leg.cap = 7.5 / 0.32 = 23.44 份
    assert Decimal(leg["shares"]) == Decimal("23.44"), leg
    assert Decimal(leg["cost_usdc"]) == Decimal("5.86")          # 23.44 @ 0.25
    assert Decimal(leg["cost_usdc"]) <= Decimal("15")            # ≤ fire 预算
    assert round(float(state["paper_total_debit_usdc"]), 4) == 5.86, state["paper_total_debit_usdc"]
    # 腿级窗口贯通到 FAK 梯子：intent 自带 floor/cap/entry_channel
    intents = [i for i in ladlog if i.get("status") == "send_fak" and i.get("leg") == "buy_yes_next"]
    assert intents and all(i["cap"] == "0.32" and i["floor"] == "0.20"
                           and i["entry_channel"] == "next_bucket"
                           and Decimal(i["limit_price"]) <= Decimal("0.32") for i in intents), ladlog
    # 审计行（events.jsonl）带通道来源
    _r_cycle._record_fire_event(cfg, state, fire, pos, ladlog, now)
    rows = [_json.loads(l) for l in open(log_path, encoding="utf-8").read().splitlines() if l.strip()]
    fires = [r for r in rows if r.get("type") == "fire"]
    assert fires and fires[-1]["entry_channel"] == "next_bucket", fires
    assert fires[-1]["next_entry_window"] == "[0.20, 0.32]", fires
    assert state["positions"]["paris|2026-09-10|high"]["entry_channel"] == "next_bucket"

    # 既有通道（无 entry_channel / budget_usdc=15）行为逐字不变：仍吃满 fire 预算
    legacy_tok = "YES-PARIS-31"
    _r_cycle.book_cache()[legacy_tok] = {
        "best_ask": "0.60", "best_bid": "0.58", "tick_size": "0.01",
        "asks": [{"price": "0.60", "size": "100"}], "bids": [{"price": "0.58", "size": "100"}],
    }
    legacy_fire = {
        **{k: v for k, v in fire.items() if k not in ("entry_channel", "next_entry_window", "budget_usdc")},
        "key": "london|2026-09-10|high", "city_id": "london", "icao": "EGLC",
        "target_bucket_id": "b31", "bucket_id": "b31", "budget_usdc": "15.0",
        "legs": [{
            "leg": "buy_yes_lock", "token_id": legacy_tok, "side": "BUY", "outcome": "YES",
            "cap": "0.75", "floor": "0.45", "notional_pct": "1.0", "bucket_id": "b31",
            "entry_channel": "target_bucket",
        }],
    }
    state2 = dict(state, positions={}, paper_total_debit_usdc=0.0)
    pos2, ladlog2 = _r_cycle._paper_fire(cfg, state2, legacy_fire, now)
    assert pos2 is not None and pos2["budget_usdc"] == "15.0", pos2
    assert Decimal(pos2["legs"][0]["shares"]) == Decimal("20.00"), pos2["legs"]   # 15 / 0.75
    assert Decimal(pos2["legs"][0]["cost_usdc"]) == Decimal("12.00"), pos2["legs"]  # 20 @ 0.60
    assert round(float(state2["paper_total_debit_usdc"]), 4) == 12.0

    # 零降级（引擎层）：book 在窗口上方 ⇒ 无任何成交（绝不按窗口外价格成交，也不挂被动单）
    state3 = dict(state, positions={}, paper_total_debit_usdc=0.0)
    _r_cycle.book_cache()[next_tok] = {
        "best_ask": "0.33", "best_bid": "0.10", "tick_size": "0.01",
        "asks": [{"price": "0.33", "size": "100"}], "bids": [{"price": "0.10", "size": "100"}],
    }
    pos3, ladlog3 = _r_cycle._paper_fire(cfg, state3, fire, now)
    assert pos3 is not None and Decimal(pos3["legs"][0]["shares"]) == Decimal("0")
    assert {i["status"] for i in ladlog3} == {"abort_above_cap"}, ladlog3
    assert round(float(state3["paper_total_debit_usdc"]), 4) == 0.0
    print("PASS: test_paper_fire_next_entry_channel_budget_and_audit "
          "(7.5/15 预算隔离 + 腿级窗口贯通 + 审计字段 + 带外零成交)")


def test_consensus_entry_fire_channel_specs():
    """生产分支 `_r_cycle.consensus_entry_fire`：两通道的腿/窗口/预算/审计字段。

    顺带覆盖 2026-09-12 发现的绑定缺陷：HEAD 的入口 fire 分支引用 `ZoneInfo`，而它只由 TAF 块里的
    一处 local import 绑定 ⇒ 有 TAF 时正常、**无 TAF（market_rank1 回退）时 UnboundLocalError 并中断
    整轮**。提取后本函数自行绑定 ZoneInfo，故下面的 `local_fire_time`（+02:00）一定能算出。
    """
    from datetime import datetime as _dt
    from decimal import Decimal as _D
    cfg = {"mode": "paper", "fire_budget_usdc": 15.0, "log_path": "/tmp/nb-entry-fire.jsonl"}
    _r_cycle._CONSENSUS_LOCK_STRAT = None            # 隔离全局缓存
    strat = _r_cycle._get_consensus_lock_strat({
        "consensus_lock": {"next_entry_enabled": True, "next_entry_min_ask": "0.20",
                           "next_entry_max_ask": "0.32", "next_entry_budget_pct": "0.5"},
        "strategy": {"yes_min_ask": "0.45", "yes_max_ask": "0.75"},
    })
    city = {"city_id": "paris", "timezone": "Europe/Paris", "icao": "LFPB", "market_unit": "C"}
    rule = {"market_local_date": "2026-09-10", "direction": "high", "buckets": []}
    now = _dt(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)
    res_next = {
        "action": "execute_taker_fire", "entry_channel": "next_bucket", "bucket_id": "b32",
        "token_id": "Y32", "fill_price": "0.25", "shares": "30", "cost_usdc": "7.50",
        "cap": "0.32", "floor": "0.20", "budget_pct": "0.5", "budget_usdc": "7.50",
        "bucket_lo": 32.0, "bucket_hi": 33.0, "bucket_label": "32°C",
    }
    fire = _r_cycle.consensus_entry_fire(
        cfg, strat, rule_key="paris|2026-09-10|high", city=city, rule=rule,
        expected_ref=31.0, taf_extreme_market=31.0, temp=31.2, market_unit="C",
        entry_res=res_next, now=now)
    assert fire["entry_channel"] == "next_bucket" and fire["budget_usdc"] == "7.50", fire
    assert fire["next_entry_window"] == "[0.20, 0.32]", fire
    leg = fire["legs"][0]
    assert leg["leg"] == "buy_yes_next" and leg["entry_channel"] == "next_bucket", leg
    assert leg["cap"] == "0.32" and leg["floor"] == "0.20" and leg["bucket_id"] == "b32", leg
    assert leg["notional_pct"] == "1.0", leg
    # ZoneInfo 路径真的执行了（+02:00 = Europe/Paris 夏令时）
    assert fire["local_fire_time"].endswith("+02:00"), fire["local_fire_time"]
    assert fire["target_bucket_id"] == "b32", fire
    assert Decimal(fire["budget_usdc"]) == Decimal("7.50")

    # 既有目标桶通道：窗口/腿一字未改；预算 = fire 预算（本会话新通道未占用）
    strat.state.session_next_entry_used.pop("london|2026-09-10|high", None)
    res_target = {"action": "execute_taker_fire", "entry_channel": "target_bucket",
                  "bucket_id": "b31", "token_id": "Y31", "fill_price": "0.60",
                  "shares": "25", "cost_usdc": "15.00", "cap": "0.75"}
    fire2 = _r_cycle.consensus_entry_fire(
        cfg, strat, rule_key="london|2026-09-10|high", city=city, rule=rule,
        expected_ref=31.0, taf_extreme_market=None, temp=31.2, market_unit="C",
        entry_res=res_target, now=now)
    assert fire2["entry_channel"] == "target_bucket" and fire2["budget_usdc"] == "15.0", fire2
    leg2 = fire2["legs"][0]
    assert leg2["leg"] == "buy_yes_lock" and leg2["cap"] == "0.75" and leg2["floor"] == "0.45", leg2
    assert leg2["entry_channel"] == "target_bucket", leg2

    # 预算隔离：同一会话里新通道已占用 7.5 ⇒ 既有通道只剩 7.5
    strat.state.session_next_entry_used["london|2026-09-10|high"] = _D("7.50")
    fire3 = _r_cycle.consensus_entry_fire(
        cfg, strat, rule_key="london|2026-09-10|high", city=city, rule=rule,
        expected_ref=31.0, taf_extreme_market=None, temp=31.2, market_unit="C",
        entry_res=res_target, now=now)
    assert fire3["budget_usdc"] == "7.50", fire3
    _r_cycle._CONSENSUS_LOCK_STRAT = None
    print("PASS: test_consensus_entry_fire_channel_specs "
          "(两通道 fire 规格 + 7.5/15 预算隔离 + ZoneInfo 入口可用)")


def test_engine_budget_base_injected_from_effective_fire_budget():
    """F-A（引擎侧）：`_get_consensus_lock_strat` 注入**生效 fire 预算**；两通道 fire 预算合计 == fire。

    取数必须与 `_paper_fire` 逐字同源（`cfg.get("fire_budget_usdc", DEFAULTS[...])`）：本用例把
    `consensus_lock.order_budget_usdc` 故意设成 15.0 而 fire 预算设成别的值，证明唯一的 sizing 基数
    来自 fire 预算（旧实现会在此处算错，审计 F-A）。
    """
    from decimal import Decimal as _D
    city = {"city_id": "paris", "timezone": "Europe/Paris", "icao": "LFPB", "market_unit": "C"}
    rule = {"market_local_date": "2026-09-10", "direction": "high", "buckets": []}
    now = datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)
    res_next = {"action": "execute_taker_fire", "entry_channel": "next_bucket", "bucket_id": "b32",
                "token_id": "Y32", "fill_price": "0.25", "shares": "30", "cost_usdc": "7.50",
                "cap": "0.32", "floor": "0.20", "bucket_lo": 32.0, "bucket_hi": 33.0}
    res_target = {"action": "execute_taker_fire", "entry_channel": "target_bucket", "bucket_id": "b31",
                  "token_id": "Y31", "fill_price": "0.60", "shares": "25", "cost_usdc": "15.00",
                  "cap": "0.75"}
    key = "paris|2026-09-10|high"
    for fire in ("12", "15", "30"):
        for pct in ("0.5", "1.0"):
            _r_cycle._CONSENSUS_LOCK_STRAT = None
            cfg = {"mode": "paper", "fire_budget_usdc": fire, "log_path": "/tmp/nb-fa.jsonl"}
            # run_cycle 传入的是**完整引擎 cfg**（顶层 fire_budget_usdc + 两个子 dict）
            strat = _r_cycle._get_consensus_lock_strat({
                **cfg,
                "consensus_lock": {"next_entry_enabled": True, "next_entry_budget_pct": pct,
                                   "order_budget_usdc": "15.0"},     # 故意与 fire 预算不等
                "strategy": {"yes_min_ask": "0.45", "yes_max_ask": "0.75"},
            })
            engine_base = _D(str(cfg.get("fire_budget_usdc")))        # _paper_fire 的同一表达式
            assert strat.cfg["fire_budget_usdc"] == fire, strat.cfg["fire_budget_usdc"]
            assert strat.order_budget() == engine_base, (fire, strat.order_budget())
            # 策略侧记账（`evaluate_next_bucket_entry` 的 `(基数 × pct).quantize(0.01)`）== 引擎侧预算
            booked = (strat.order_budget() * _D(pct)).quantize(_D("0.01"))
            fire_next = _r_cycle.consensus_entry_fire(
                cfg, strat, rule_key=key, city=city, rule=rule, expected_ref=31.0,
                taf_extreme_market=31.0, temp=31.2, market_unit="C",
                entry_res={**res_next, "budget_pct": pct}, now=now)
            new_budget = _D(fire_next["budget_usdc"])
            assert new_budget == (engine_base * _D(pct)).quantize(_D("0.01")), (fire, pct, new_budget)
            assert new_budget == booked, (fire, pct, new_budget, booked, "策略/引擎必须同基数")
            # 同会话既有通道：引擎用 target_channel_budget(key, fire 预算) = fire − 已占用
            strat.state.session_next_entry_used[key] = booked
            fire_target = _r_cycle.consensus_entry_fire(
                cfg, strat, rule_key=key, city=city, rule=rule, expected_ref=31.0,
                taf_extreme_market=None, temp=31.2, market_unit="C",
                entry_res=res_target, now=now)
            target_budget = _D(fire_target["budget_usdc"])
            assert target_budget == engine_base - booked, (fire, pct, target_budget)
            assert target_budget >= _D("0")
            # 不变量：新通道 + 既有通道 == fire（两基数统一后逐字成立）
            assert new_budget + target_budget == engine_base, (fire, pct, new_budget, target_budget)
    _r_cycle._CONSENSUS_LOCK_STRAT = None
    print("PASS: test_engine_budget_base_injected_from_effective_fire_budget "
          "(注入生效 fire 预算；3 组 fire × 2 组 pct 下两通道预算合计 == fire)")


def test_leg_to_bucket_mapping_contract():
    """F-F（LOW）：`_paper_fire` 的"腿 → 桶"映射契约（把既有行为固定下来）。

    既有行为（`_r_cycle._paper_fire` 的两段映射）：
      ① ``buy_no_broken``  → ``fire["broken_bucket_id"]``（存在时覆盖腿自带 bucket）
      ② ``buy_yes_lock``   → ``fire["target_bucket_id"]``
      ③ ``buy_yes_new`` / ``buy_yes_sleeve`` / ``buy_yes_next`` → ``fire["new_bucket_id"] or 腿自带 bucket_id``
         —— **这就是 2026-09-12 的加宽点**：`new_bucket_id` 为空串/None 时不再记成 ``""``，而是退回腿自己的桶
         （生产链路上新通道 fire 不带 `new_bucket_id` ⇒ 一定走腿自带 bucket；审计回归实证 ``'' → 'B2'``）
      ④ 其它腿名 → 腿自带 ``bucket_id``（缺失 ⇒ 保持 ``None``/``""``，不抛异常）
    """
    log_path = "/tmp/nb-leg-bucket.jsonl"
    cfg = {"mode": "paper", "fire_budget_usdc": 20.0, "log_path": log_path}
    now = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)

    def _book(tok):
        _r_cycle.book_cache()[tok] = {
            "best_ask": "0.40", "best_bid": "0.38", "tick_size": "0.01",
            "asks": [{"price": "0.40", "size": "500"}], "bids": [{"price": "0.38", "size": "500"}],
        }

    def _leg(name, tok, bucket, **extra):
        leg = {"leg": name, "token_id": tok, "side": "BUY", "outcome": "YES",
               "cap": "0.50", "notional_pct": "0.25"}
        if bucket is not None:
            leg["bucket_id"] = bucket
        leg.update(extra)
        return leg

    def _run(key, legs, **fire_extra):
        for leg in legs:
            _book(str(leg["token_id"]))
        fire = {"key": key, "kind": "consensus_lock", "city_id": "paris", "icao": "LFPB",
                "market_local_date": "2026-09-10", "direction": "high", "ref_extreme": 31.0,
                "ref_source": "market_rank1", "running_extreme": 31.2, "jump": 0,
                "local_fire_time": "2026-09-10T16:00:00+02:00", "market_unit": "C", "fire_no": 1,
                "legs": legs, "action_type": "re_fire", **fire_extra}
        state = {"paper_initial_capital_usdc": 700.0, "positions": {},
                 "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}}}
        pos, _ = _r_cycle._paper_fire(cfg, state, fire, now)
        assert pos is not None, key
        return {str(lg["leg"]): lg for lg in pos["legs"]}, state

    # ① fire 侧键存在 ⇒ 覆盖腿自带 bucket（既有 fire #1 / 追火 的形态）
    legs, _ = _run("k1|2026-09-10|high",
                   [_leg("buy_no_broken", "N1", "LEG-NO", outcome="NO", cap="0.65"),
                    _leg("buy_yes_lock", "Y1", "LEG-LOCK"),
                    _leg("buy_yes_new", "Y2", "LEG-NEW")],
                   broken_bucket_id="B1", target_bucket_id="B3", new_bucket_id="B2")
    assert legs["buy_no_broken"]["bucket_id"] == "B1", legs
    assert legs["buy_yes_lock"]["bucket_id"] == "B3", legs
    assert legs["buy_yes_new"]["bucket_id"] == "B2", legs

    # ② new_bucket_id 为空串 ⇒ 退回腿自带 bucket（F-F：此前记成 ""）
    legs, _ = _run("k2|2026-09-10|high",
                   [_leg("buy_yes_new", "Y3", "B2")], broken_bucket_id="B1", new_bucket_id="")
    assert legs["buy_yes_new"]["bucket_id"] == "B2", legs

    # ③ new_bucket_id 缺失（生产新通道 fire 的形态：不带该键）⇒ 腿自带 bucket
    legs, _ = _run("k3|2026-09-10|high",
                   [_leg("buy_yes_next", "Y4", "B2", floor="0.20", cap="0.32",
                         entry_channel="next_bucket")],
                   target_bucket_id="B2")
    assert legs["buy_yes_next"]["bucket_id"] == "B2", legs

    # ④ 其它腿名 ⇒ 腿自带 bucket；腿无 bucket 且无 fire 侧键 ⇒ 不抛异常（保持原值）
    legs, _ = _run("k4|2026-09-10|high",
                   [_leg("buy_yes_sleeve", "Y5", "B7"), _leg("unknown_leg", "Y6", None)])
    assert legs["buy_yes_sleeve"]["bucket_id"] == "B7", legs
    assert legs["unknown_leg"]["bucket_id"] in (None, ""), legs
    print("PASS: test_leg_to_bucket_mapping_contract "
          "(fire 侧键优先 / new_bucket_id 空 ⇒ 腿自带 bucket / 其它腿名保持原值)")


if __name__ == "__main__":
    test_paper_fire_buy_yes_lock()
    test_early_stop_loss_liquidation()
    test_paper_fire_next_entry_channel_budget_and_audit()
    test_consensus_entry_fire_channel_specs()
    test_engine_budget_base_injected_from_effective_fire_budget()
    test_leg_to_bucket_mapping_contract()
    print("ALL INTEGRATION TESTS PASSED!")
