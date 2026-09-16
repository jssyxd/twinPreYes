#!/usr/bin/env python3
"""tests_consensus_lock.py — 针对优化版策略的完整测试套件 (8大微观结构场景全覆盖)"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN

from consensus_tracker import ConsensusTracker
from strategy_consensus_lock import (
    ConsensusLockStrategy,
    PositionRecord,
    RestingOrder,
)


def make_city(city_id: str = "paris", tz: str = "Europe/Paris", icao: str = "LFPB") -> dict:
    return {"city_id": city_id, "timezone": tz, "icao": icao, "market_unit": "C"}


def make_buckets() -> list[dict]:
    return [
        {"bucket_id": "b29", "lo": 29.0, "hi": 30.0, "yes_token_id": "Y29", "no_token_id": "N29"},
        {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "Y30", "no_token_id": "N30"},
        {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "Y31", "no_token_id": "N31"},
        {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "Y32", "no_token_id": "N32"},
        {"bucket_id": "b33", "lo": 33.0, "hi": 34.0, "yes_token_id": "Y33", "no_token_id": "N33"},
    ]


def test_station_filter():
    # ① 过滤器关闭（当前生产配置：操作者 2026-09-12 决定"放开全部 49 站"）⇒ 任何站都被允许
    off = ConsensusLockStrategy()
    assert off.cfg.get("filter_fast_stations_only") is False, "生产配置应为放开全部站点"
    assert off.is_fast_station("paris") is True
    assert off.is_fast_station("tokyo") is True
    assert off.is_fast_station("miami") is True       # 60 min 站在过滤器关闭后同样被允许
    assert off.is_fast_station("chicago") is True
    # ② 过滤器打开（保留语义）⇒ 仅高频站白名单通过
    on = ConsensusLockStrategy(cfg={"filter_fast_stations_only": True})
    assert on.is_fast_station("paris") is True
    assert on.is_fast_station("tokyo") is True
    assert on.is_fast_station("miami") is False       # 60 min
    assert on.is_fast_station("chicago") is False
    print("PASS: 1. test_station_filter")


def test_time_window():
    strat = ConsensusLockStrategy()
    # 14:00 UTC for Paris (UTC+2) -> 16:00 local (in 12-18 window)
    dt_in = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    in_win, hr = strat.is_in_time_window(dt_in, "Europe/Paris", "high")
    assert in_win is True

    # 06:00 UTC for Paris -> 08:00 local (outside 12-18 window)
    dt_out = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    in_win2, hr2 = strat.is_in_time_window(dt_out, "Europe/Paris", "high")
    assert in_win2 is False
    print("PASS: 2. test_time_window")


def test_capped_taker_entry():
    """验证 Capped Taker 执行：避免挂单 0 成交陷阱，在合理区间直接吃单锁定胜率。"""
    strat = ConsensusLockStrategy({"entry_mode": "capped_taker", "yes_max_ask": Decimal("0.75")})
    city = make_city("paris")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    now_utc = datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)

    tracker = ConsensusTracker()
    for t_step in range(10):
        t_sample = datetime(2026, 9, 10, 12, 10 + t_step, 0, tzinfo=timezone.utc)
        books_sample = {
            "Y31": {"best_bid": "0.65", "best_ask": "0.68", "tick_size": "0.01"},
            "Y32": {"best_bid": "0.08", "best_ask": "0.12", "tick_size": "0.01"},
        }
        tracker.record_books("paris", date_str, dir_str, bks, books_sample, t_sample)

    # 场景 A: Ask 在 0.68 (<= 0.75)，主动吃单入场，立即生成仓位
    books_now = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.68", "tick_size": "0.01"},
        "Y32": {"best_bid": "0.08", "best_ask": "0.12", "tick_size": "0.01"},
    }
    obs_ok = {"temp_c": 31.2}
    res = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs_ok, books_now, tracker, now_utc)
    assert res["action"] == "execute_taker_fire"
    assert res["fill_price"] == "0.68"
    assert res["shares"] == "22"  # 15 / 0.68 = 22.05 -> 22
    assert res["fire_no"] == 1
    assert "paris|2026-09-10|high" in strat.state.open_positions

    # 场景 B: 若 Ask 涨至 0.82 (> 0.75 安全上限)，坚决不追高
    strat2 = ConsensusLockStrategy({"entry_mode": "capped_taker", "yes_max_ask": Decimal("0.75")})
    books_expensive = {
        "Y31": {"best_bid": "0.78", "best_ask": "0.82", "tick_size": "0.01"},
        "Y32": {"best_bid": "0.08", "best_ask": "0.12", "tick_size": "0.01"},
    }
    res2 = strat2.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs_ok, books_expensive, tracker, now_utc)
    assert res2["action"] == "skip"
    assert "ask_above_safety_cap" in res2["reason"]
    print("PASS: 3. test_capped_taker_entry")


def test_next_bucket_barrier_reject():
    strat = ConsensusLockStrategy()
    city = make_city("tokyo", tz="Asia/Tokyo", icao="RJTT")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    now_utc = datetime(2026, 9, 10, 5, 0, 0, tzinfo=timezone.utc)

    tracker = ConsensusTracker()
    for t_step in range(10):
        t_sample = datetime(2026, 9, 10, 4, 10 + t_step, 0, tzinfo=timezone.utc)
        # 下一档 Y32 达 0.35 (>= 0.20)，说明市场预警很可能突破
        books_sample = {
            "Y31": {"best_bid": "0.55", "best_ask": "0.60"},
            "Y32": {"best_bid": "0.32", "best_ask": "0.38"},
        }
        tracker.record_books("tokyo", date_str, dir_str, bks, books_sample, t_sample)

    books_now = {
        "Y31": {"best_bid": "0.55", "best_ask": "0.60"},
        "Y32": {"best_bid": "0.32", "best_ask": "0.38"},
    }
    obs_ok = {"temp_c": 31.4}
    res = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs_ok, books_now, tracker, now_utc)
    assert res["action"] == "skip"
    assert "next_bucket_twap_too_high" in res["reason"]
    print("PASS: 4. test_next_bucket_barrier_reject")


def test_pre_metar_early_stop_next_bucket_surge():
    """验证风控亮点：在 METAR 落地前，若监测到下一档被抢买至 0.38 (>=0.35)，提前抢跑止损！"""
    strat = ConsensusLockStrategy({"early_stop_next_bucket_surge": Decimal("0.35")})
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 15, 0, tzinfo=timezone.utc)

    # 假设我们持有 31 度 YES (买入均价 0.65，投入 14.30 USDC)
    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("22"), cost_usdc=Decimal("14.30"), avg_price=Decimal("0.65"),
        entry_ts_utc=now_utc.isoformat(),
    )

    # 盘口异动发生：32 度 YES 突然被推高到 0.38 (散户或大户抢跑买32度)
    # 此时 31 度的买盘还在 0.52 (还没彻底崩盘)
    books_surge = {
        "Y31": {"best_bid": "0.52", "best_ask": "0.58"},
        "Y32": {"best_bid": "0.34", "best_ask": "0.38"},
    }

    res = strat.evaluate_early_stop_loss(key, "high", bks, books_surge, now_utc)
    assert res is not None
    assert res["action"] == "early_stop_loss_executed"
    assert "next_bucket_surge" in res["reason"]
    # 以 0.52 成功抢跑割肉，回收 22 * 0.52 = 11.44 USDC (回收率 80%!)
    assert Decimal(res["recovered_usdc"]) == Decimal("11.44")
    assert Decimal(res["loss_usdc"]) == Decimal("2.86")  # 仅损失 2.86 而非 14.30 全亏！
    assert strat.state.open_positions[key].liquidated is True
    print("PASS: 5. test_pre_metar_early_stop_next_bucket_surge (成功保全 80% 本金)")


def test_pre_metar_early_stop_bid_floor():
    """验证风控亮点：持仓桶买盘撤单跌破 0.45，立即自动割肉保命。

    ②（2026-09-13）：开仓冷静期只对**新仓**生效；本用例验的是"门限破了就割"，因此把入场时间
    设在冷静期之外（1 小时前）—— fresh 仓被抑制的行为由 ``test_early_stop_grace_period`` 单独覆盖。
    """
    strat = ConsensusLockStrategy({"early_stop_bid_floor": Decimal("0.45")})
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 20, 0, tzinfo=timezone.utc)

    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("22"), cost_usdc=Decimal("14.30"), avg_price=Decimal("0.65"),
        entry_ts_utc=(now_utc - timedelta(hours=1)).isoformat(),
    )

    # 买盘突然从 0.65 溃退到 0.40
    books_collapse = {
        "Y31": {"best_bid": "0.40", "best_ask": "0.48"},
        "Y32": {"best_bid": "0.20", "best_ask": "0.25"},
    }
    res = strat.evaluate_early_stop_loss(key, "high", bks, books_collapse, now_utc)
    assert res is not None
    assert "bid_floor_broken" in res["reason"]
    # 目标桶通道：reason 串与门限**逐字不变**（0.45，既有格式）
    assert res["reason"] == "bid_floor_broken (bid=0.40 < 0.45)", res["reason"]
    assert res["entry_channel"] == "target_bucket", res
    assert Decimal(res["recovered_usdc"]) == Decimal("8.80")
    print("PASS: 6. test_pre_metar_early_stop_bid_floor")


def test_breach_risk_control_and_no_defence():
    """验证实测硬破位风控：微观结构 NO 扫空防御机制。"""
    strat = ConsensusLockStrategy({"risk_control_no_cap": Decimal("0.85")})
    city = make_city("paris")
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 30, 0, tzinfo=timezone.utc)

    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("22"), cost_usdc=Decimal("14.30"), avg_price=Decimal("0.65"),
        entry_ts_utc=now_utc.isoformat(),
    )
    strat.state.session_fires_count[key] = 1

    # 官方 METAR 证实跳到 32.5 度 (破位!)
    # 实盘微观结构：N31 ask 已被扫至 0.99，Y32 ask 为 0.70
    books_swept = {
        "Y31": {"best_bid": "0.01", "best_ask": "0.05"},
        "N31": {"best_bid": "0.95", "best_ask": "0.99"},  # NO 昂贵超限
        "Y32": {"best_bid": "0.65", "best_ask": "0.70"},
    }

    res = strat.handle_breach_risk_control(key, city, "high", bks, 32.5, books_swept, now_utc)
    assert res["action"] == "breach_reverse_executed"
    # 验证防御：N31 超过 0.85 被安全跳过，只保留有安全边际的 Y32
    legs = {l["leg"]: l for l in res["hedge_legs"]}
    assert "buy_no_broken" not in legs, "NO leg above 0.85 must be skipped"
    assert "buy_yes_new" in legs and legs["buy_yes_new"]["token_id"] == "Y32"
    assert strat.state.session_fires_count[key] == 2
    print("PASS: 7. test_breach_risk_control_and_no_defence")


def test_max_fires_cap_enforcement():
    """验证单日双火硬顶：同一 Session 最多 2 次开仓/反手，杜绝 Warsaw 式连环双杀。"""
    strat = ConsensusLockStrategy({"max_fires_per_session": 2})
    city = make_city("warsaw", tz="Europe/Warsaw", icao="EPWA")
    bks = make_buckets()
    key = "warsaw|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone.utc)

    # 已经发生过 2 次开仓
    strat.state.session_fires_count[key] = 2
    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b32", yes_token_id="Y32",
        shares=Decimal("20"), cost_usdc=Decimal("14.0"), avg_price=Decimal("0.70"),
        entry_ts_utc=now_utc.isoformat(),
    )

    # 温度再次突破到 33.5 度 (第三次破位)
    books = {"Y32": {"best_bid": "0.01"}, "Y33": {"best_bid": "0.70"}}
    res = strat.handle_breach_risk_control(key, city, "high", bks, 33.5, books, now_utc)
    assert res["action"] == "breach_risk_control_closed_only"
    assert "max_fires_reached" in res["reason"]
    # 绝不再追买第三手，只清空持仓止损，锁死账户单日最大回撤！
    print("PASS: 8. test_max_fires_cap_enforcement")


def test_post_stop_loss_cooldown():
    """防线 1: 验证止损后单向熔断冷却，彻底杜绝赫尔辛基式同一分钟连续开仓。"""
    strat = ConsensusLockStrategy({"early_stop_next_bucket_surge": Decimal("0.35")})
    city = make_city("helsinki", tz="Europe/Helsinki", icao="EFHK")
    bks = make_buckets()
    key = "helsinki|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 12, 40, 27, tzinfo=timezone.utc)

    # 1. 模拟初次开仓并持仓
    strat.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31",
        shares=Decimal("18.75"), cost_usdc=Decimal("14.43"), avg_price=Decimal("0.77"),
        entry_ts_utc=now_utc.isoformat(),
    )
    strat.state.session_fires_count[key] = 1

    # 2. 下一档暴涨至 0.49 触发提前止损
    books_surge = {
        "Y31": {"best_bid": "0.50", "best_ask": "0.60"},
        "Y32": {"best_bid": "0.45", "best_ask": "0.49"},
    }
    stop_res = strat.evaluate_early_stop_loss(key, "high", bks, books_surge, now_utc)
    assert stop_res is not None
    assert key in strat.state.stopped_out_sessions
    assert key in strat.state.breached_sessions

    # 3. 验证紧接着的下一个 tick 即使 session_fires_count=1 < max_fires=2，也坚决拒绝二次入场！
    tracker = ConsensusTracker()
    tracker.record_books("helsinki", "2026-09-10", "high", bks, books_surge, now_utc)
    obs = {"temp_c": 31.0, "obs_age_s": 0}
    entry_res = strat.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_surge, tracker, now_utc)
    assert entry_res["action"] == "skip"
    assert entry_res["reason"] == "session_already_stopped_out"
    print("PASS: 9. test_post_stop_loss_cooldown (止损后单向熔断成功阻断二次入场)")


def test_temperature_velocity_stalling_filter():
    """防线 2: 验证气温变化率停滞过滤器 (冲顶动量拦截与见顶企稳放行)。"""
    strat = ConsensusLockStrategy({"min_dwell_seconds_if_rising": 1800})
    city = make_city("helsinki", tz="Europe/Helsinki", icao="EFHK")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    t0 = datetime(2026, 9, 10, 12, 10, 0, tzinfo=timezone.utc)

    tracker = ConsensusTracker()
    books = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
        "Y32": {"best_bid": "0.05", "best_ask": "0.10"},
    }
    tracker.record_books("helsinki", date_str, dir_str, bks, books, t0)

    # 步骤 1: 上一份报文为 30.0 度
    obs1 = {"temp_c": 30.0, "obs_age_s": 600}
    res1 = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs1, books, tracker, t0)
    assert res1["action"] == "skip"
    assert "not_reached_expected_high" in res1["reason"]

    # 步骤 2: 下一份报文跳升到 31.0 度 (刚跳字，升温斜率冲顶，停留仅 60 秒)
    t1 = t0 + timedelta(minutes=10)
    obs2 = {"temp_c": 31.0, "obs_age_s": 60}
    res2 = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs2, books, tracker, t1)
    assert res2["action"] == "skip"
    assert "temperature_rising_velocity_active" in res2["reason"]
    print("PASS: 10a. test_temperature_velocity_stalling_filter (刚跳升未停滞成功拦截)")

    # 步骤 3: 维持该温度超过 1800 秒 (见顶企稳，斜率归零)
    t2 = t1 + timedelta(seconds=1900)
    obs3 = {"temp_c": 31.0, "obs_age_s": 1900}
    res3 = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs3, books, tracker, t2)
    assert res3["action"] == "execute_taker_fire"
    print("PASS: 10b. test_temperature_velocity_stalling_filter (充分停滞后正常放行开仓)")


def test_next_bucket_instantaneous_book_checks():
    """防线 3: 验证下一档瞬时盘口与买单防御 (防范快钱突袭与 TWAP 滞后)。"""
    strat = ConsensusLockStrategy({
        "next_bucket_max_twap": Decimal("0.26"),
        "next_bucket_max_instant_ask": Decimal("0.25"),
        "next_bucket_max_instant_bid": Decimal("0.15"),
    })
    city = make_city("tokyo", tz="Asia/Tokyo", icao="RJTT")
    bks = make_buckets()
    date_str = "2026-09-10"
    dir_str = "high"
    now_utc = datetime(2026, 9, 10, 5, 0, 0, tzinfo=timezone.utc)

    # 过去 1 小时 TWAP 看起来很低 (0.15 < 0.26)
    tracker = ConsensusTracker()
    for t_step in range(10):
        t_sample = datetime(2026, 9, 10, 4, 10 + t_step, 0, tzinfo=timezone.utc)
        books_sample = {
            "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
            "Y32": {"best_bid": "0.10", "best_ask": "0.15"},
        }
        tracker.record_books("tokyo", date_str, dir_str, bks, books_sample, t_sample)

    # 场景 A: 瞬时 Ask 突增至 0.28 (>= 0.25)
    books_instant_ask_high = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
        "Y32": {"best_bid": "0.10", "best_ask": "0.28"},
    }
    obs = {"temp_c": 31.0, "obs_age_s": 2000}
    res_a = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs, books_instant_ask_high, tracker, now_utc)
    assert res_a["action"] == "skip"
    assert "next_bucket_instant_ask_too_high" in res_a["reason"]

    # 场景 B: 瞬时 Bid 潜伏至 0.18 (>= 0.15)
    books_instant_bid_high = {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"},
        "Y32": {"best_bid": "0.18", "best_ask": "0.22"},
    }
    res_b = strat.evaluate_entry(city, date_str, dir_str, bks, 31.0, obs, books_instant_bid_high, tracker, now_utc)
    assert res_b["action"] == "skip"
    assert "next_bucket_instant_bid_too_high" in res_b["reason"]
    print("PASS: 11. test_next_bucket_instantaneous_book_checks (瞬时盘口双向校验生效)")


# --------------------------------------------------------------------------- #
# 并行通道：下一档桶廉价入场 (buy_yes_next)  —— 2026-09-12
# 依据 preyes_param_sim_20260912.md §5/§6：既有通道的非价格门全通过时目标桶 ask 已被
# 定价到 0.81–0.99（cap 0.75 挡死 ⇒ 8h 零成交）⇒ 改入场对象为"下一档桶"，窗口自有独立。
# --------------------------------------------------------------------------- #
NEXT_CFG = {
    "next_entry_enabled": True,
    "next_entry_min_ask": "0.20",
    "next_entry_max_ask": "0.32",
    "next_entry_budget_pct": "0.5",
    "order_budget_usdc": "15.0",
}


def _next_entry_case(ask_next: str, *, ask_target: str = "0.90", cfg_extra: dict | None = None,
                     enabled: bool = True, samples: bool = True, now_utc=None, city=None,
                     temp: float = 31.2, obs_age_s: float = 2000.0, expected: float | None = 31.0,
                     date_str: str = "2026-09-10", dir_str: str = "high",
                     next_sample_ask: str = "0.12"):
    """一个"非价格门全通过 + 给定下一档桶 ask"的场景（新通道测试用）。

    `ask_target` 默认 0.90 = 实盘观测到的目标桶报价（被既有 cap 0.75 挡死）；`ask_next` 是新通道
    唯一的变量；`next_sample_ask` 决定 tracker 里下一档桶的历史 TWAP（0.12 ⇒ ~0.10，低于既有
    0.26 门）。返回 (strat, res, tracker, now, city, books)。
    """
    cfg = dict(NEXT_CFG) if enabled else dict(NEXT_CFG, next_entry_enabled=False)
    cfg.update(cfg_extra or {})
    strat = ConsensusLockStrategy(cfg)
    city = city or make_city("paris")
    bks = make_buckets()                     # b29..b33 (Y29..Y33)；target=b31，next=b32
    tracker = ConsensusTracker()
    if samples:
        next_bid = str(Decimal(next_sample_ask) - Decimal("0.02"))
        for step in range(10):
            ts = datetime(2026, 9, 10, 12, 10 + step, 0, tzinfo=timezone.utc)
            tracker.record_books("paris", date_str, dir_str, bks, {
                "Y31": {"best_bid": "0.65", "best_ask": "0.68"},
                "Y32": {"best_bid": next_bid, "best_ask": next_sample_ask},
            }, ts)
    now = now_utc or datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)   # Paris 15:00 本地
    books = {
        "Y31": {"best_bid": "0.80", "best_ask": ask_target, "tick_size": "0.01"},
        "Y32": {"best_bid": "0.10", "best_ask": ask_next, "tick_size": "0.01"},
    }
    obs = {"temp_c": temp, "obs_age_s": obs_age_s}
    res = strat.evaluate_entry(city, date_str, dir_str, bks, expected, obs, books, tracker, now)
    return strat, res, tracker, now, city, books


def test_next_entry_channel_default_off_and_config_gate():
    """① 代码默认关闭（保守）；② 部署 config 显式开启；③ 非法窗口 fail-closed 不回退。"""
    default = ConsensusLockStrategy()
    assert default.cfg["next_entry_enabled"] is False, "代码默认必须是关闭"
    win = default.next_entry_window()
    assert win["ok"] is True and str(win["lo"]) == "0.20" and str(win["hi"]) == "0.32"
    assert win["label"] == "[0.20, 0.32]"

    # 关闭态：即使下一档桶 ask=0.25 在窗口内，也必须走既有通道（被既有共识/顶价门挡下）
    strat_off, res_off, *_ = _next_entry_case("0.25", enabled=False)
    assert res_off["action"] == "skip", res_off
    assert ("ask_above_safety_cap" in res_off["reason"]
            or "next_bucket_instant_ask_too_high" in res_off["reason"]), res_off
    assert strat_off.last_next_entry_skip is None, "未评估的新通道不留审计噪声"
    assert "paris|2026-09-10|high" not in strat_off.state.open_positions

    # 开启态：同一场景由新通道成交（证明 window 是唯一的开关差异）
    strat_on, res_on, *_ = _next_entry_case("0.25", enabled=True)
    assert res_on["action"] == "execute_taker_fire"
    assert res_on["entry_channel"] == "next_bucket"
    assert res_on["bucket_id"] == "b32" and res_on["token_id"] == "Y32"

    # 明确的语义替代（有意为之，报告里如实记录）：下一档桶的 twap/instant **价格子门**
    # （0.26/0.25/0.15）是既有目标桶通道的过滤器；新通道用**自有窗口** (0.20, 0.32] 替代它，
    # 因此 twap>=0.26 时新通道仍可入场 —— 但 ask 必须落在窗口内（见边界矩阵：0.321 仍弃单）。
    strat_sub, res_sub, *_ = _next_entry_case("0.25", next_sample_ask="0.41")
    assert float(res_sub["consensus_meta"]["next_bucket_twap"]) >= 0.26, res_sub
    assert res_sub["action"] == "execute_taker_fire" and res_sub["entry_channel"] == "next_bucket", res_sub
    # 同一 fixture、通道关闭 ⇒ 既有通道被 twap 价格子门挡死（证明这里确实是"替代"而非"绕过"）
    _, res_sub_off, *_ = _next_entry_case("0.25", next_sample_ask="0.41", enabled=False)
    assert res_sub_off["action"] == "skip" and "next_bucket_twap_too_high" in res_sub_off["reason"], res_sub_off

    # 非法窗口（lo >= hi）⇒ 整通道弃单，绝不回退既有 (0.45, 0.75]：
    # 下一档 0.20 让既有通道的共识门通过 ⇒ 若新通道错误回退窗口，成交来源就会变成 next_bucket
    strat_bad, res_bad, *_ = _next_entry_case(
        "0.20", ask_target="0.60", cfg_extra={"next_entry_min_ask": "0.40", "next_entry_max_ask": "0.32"})
    assert strat_bad.last_next_entry_skip and "next_entry_window_invalid" in strat_bad.last_next_entry_skip
    assert res_bad["action"] == "execute_taker_fire", res_bad
    assert res_bad["entry_channel"] == "target_bucket" and res_bad["bucket_id"] == "b31", \
        "非法窗口时只能是既有通道按自己的窗口成交（绝不回退成新通道窗口）"
    for bad_lo, bad_hi in (("abc", "0.32"), ("0.20", "NaN"), ("-0.1", "0.32"), ("0.20", "1.5"), ("0.3", "0.3")):
        w = ConsensusLockStrategy({"next_entry_min_ask": bad_lo, "next_entry_max_ask": bad_hi}).next_entry_window()
        assert w["ok"] is False, (bad_lo, bad_hi, w)
    print("PASS: 12. test_next_entry_channel_default_off_and_config_gate "
          "(默认关闭 / config 开启 / 非法窗口 fail-closed 不回退)")


def test_next_entry_window_boundary_matrix():
    """窗口边界矩阵 **[0.20, 0.32]**（B 项闭区间，2026-09-13）：0.199 ⇒ 弃、0.200 ⇒ **入**、
    0.201/0.319/0.320 ⇒ 入、0.321 ⇒ 弃。下界含等号正是用户口径（0.27 <= ask <= 0.32）。"""
    probes = [("0.199", "skip"), ("0.200", "fire"), ("0.201", "fire"),
              ("0.319", "fire"), ("0.320", "fire"), ("0.321", "skip")]
    print("  [next-entry window boundary matrix] window = [0.20, 0.32]  (closed)")
    for raw, want in probes:
        strat, res, *_ = _next_entry_case(raw)
        got = "fire" if res.get("action") == "execute_taker_fire" else "skip"
        skip_reason = strat.last_next_entry_skip
        print(f"    next_ask={raw:>6} -> {got:4}  channel={res.get('entry_channel') or 'n/a':>12}"
              f"  next_entry_skip={skip_reason}")
        assert got == want, (raw, want, res)
        if want == "fire":
            assert res["entry_channel"] == "next_bucket" and res["bucket_id"] == "b32", res
            assert res["floor"] == "0.20" and res["cap"] == "0.32", res
            assert res["window"] == "[0.20, 0.32]" and res["next_entry_window"] == "[0.20, 0.32]", res
            assert Decimal(res["fill_price"]) == Decimal(raw), res
            assert Decimal(res["shares"]) * Decimal(res["fill_price"]) <= Decimal("7.50"), res
            assert Decimal(res["shares"]) == (Decimal("7.50") / Decimal(raw)).to_integral_value(
                rounding=ROUND_DOWN), res
        else:
            expect = "next_entry_ask_below_window" if Decimal(raw) < Decimal("0.20") \
                else "next_entry_ask_above_window"
            assert skip_reason and expect in skip_reason, (raw, skip_reason)
            # 弃单 ⇒ 新通道没有任何仓位/额度占用
            assert strat.state.session_next_entry_used.get("paris|2026-09-10|high") is None
    # 闭区间端点的精确复核（B 项）：0.20 本身**入场**、0.32 本身入场、0.1999 弃单
    s1, r1, *_ = _next_entry_case("0.2")
    assert r1["action"] == "execute_taker_fire" and r1["entry_channel"] == "next_bucket", r1
    s1b, r1b, *_ = _next_entry_case("0.1999")
    assert r1b["action"] == "skip" and "next_entry_ask_below_window" in (s1b.last_next_entry_skip or "")
    s2, r2, *_ = _next_entry_case("0.32")
    assert r2["action"] == "execute_taker_fire", r2
    # 窗口是自有且独立的：改窗口 ⇒ 边界随之移动（新窗口下界仍含等号）
    s3, r3, *_ = _next_entry_case("0.26", cfg_extra={"next_entry_min_ask": "0.28"})
    assert r3["action"] == "skip" and "next_entry_ask_below_window" in (s3.last_next_entry_skip or "")
    s4, r4, *_ = _next_entry_case("0.28", cfg_extra={"next_entry_min_ask": "0.28"})
    assert r4["action"] == "execute_taker_fire", r4
    assert r4["window"] == "[0.28, 0.32]", r4
    print("PASS: 13. test_next_entry_window_boundary_matrix (闭区间 6 组边界 + 端点复核全部符合预期)")


def test_next_entry_priority_and_budget_isolation():
    """通道优先级 + 预算隔离：新通道先评估；命中则既有通道不下单；两者预算永不重叠。"""
    key = "paris|2026-09-10|high"
    # (a) 两个通道同时可成交（目标桶 0.60 ∈ (0.45,0.75]；下一档 0.25 ∈ (0.20,0.32]）
    strat, res, *_ = _next_entry_case("0.25", ask_target="0.60")
    assert res["action"] == "execute_taker_fire", res
    assert res["entry_channel"] == "next_bucket", "新通道必须优先，既有通道本次不得下单"
    assert res["bucket_id"] == "b32" and res["token_id"] == "Y32", res
    assert Decimal(res["shares"]) == Decimal("30")            # floor(7.5 / 0.25)
    assert Decimal(res["cost_usdc"]) == Decimal("7.50")
    assert res["budget_usdc"] == "7.50" and res["budget_pct"] == "0.5"
    assert strat.state.session_next_entry_used[key] == Decimal("7.50")
    # 既有通道只剩剩余额度（7.5 已被新通道占用 ⇒ 15 − 7.5 = 7.5）
    assert strat.target_channel_budget(key, Decimal("15")) == Decimal("7.50")
    assert Decimal(res["cost_usdc"]) <= Decimal("15")         # 总名义额 ≤ fire 预算
    # 会话上限沿用既有语义：同会话已有持仓 ⇒ 两个通道都不再 fire
    res_again = strat.evaluate_entry(
        make_city("paris"), "2026-09-10", "high", make_buckets(), 31.0,
        {"temp_c": 31.2, "obs_age_s": 2000}, {
            "Y31": {"best_bid": "0.80", "best_ask": "0.60"},
            "Y32": {"best_bid": "0.10", "best_ask": "0.25"}}, ConsensusTracker(),
        datetime(2026, 9, 10, 13, 5, 0, tzinfo=timezone.utc))
    assert res_again["action"] == "skip" and res_again["reason"] == "session_already_has_open_position"

    # (b) 新通道弃单（下一档 0.19 < 闭区间下界 0.20）⇒ 既有通道按**原窗口**独立判定并成交
    strat2, res2, *_ = _next_entry_case("0.19", ask_target="0.60")
    assert strat2.last_next_entry_skip and "next_entry_ask_below_window" in strat2.last_next_entry_skip
    assert res2["action"] == "execute_taker_fire", res2
    assert res2["entry_channel"] == "target_bucket" and res2["bucket_id"] == "b31", res2
    assert res2["cap"] == "0.75", res2                       # 既有窗口一字未改
    assert Decimal(res2["shares"]) == Decimal("25")          # floor(15 / 0.60) ⇒ 吃满 fire 预算
    assert strat2.target_channel_budget(key, Decimal("15")) == Decimal("15")
    assert strat2.state.session_next_entry_used.get(key) is None, "弃单不得占用任何额度"
    assert Decimal(res2["cost_usdc"]) <= Decimal("15")

    # (c) 下一档 ask 高于窗口（0.40）⇒ 新通道弃单，且任何成交都不可能是下一档桶
    strat3, res3, *_ = _next_entry_case("0.40", ask_target="0.60")
    assert strat3.last_next_entry_skip and "next_entry_ask_above_window" in strat3.last_next_entry_skip
    assert res3.get("entry_channel") != "next_bucket", res3
    assert strat3.state.session_next_entry_used.get(key) is None
    print("PASS: 14. test_next_entry_priority_and_budget_isolation "
          "(新通道优先 / 既有通道独立 / 15−7.5=7.5 剩余额度)")


def test_next_entry_non_price_gates_not_relaxed():
    """非价格门一个都不放松：站点/时间窗/预期极值/变率/共识 rank1 任一不过 ⇒ 新通道也不 fire。"""
    key = "paris|2026-09-10|high"
    # ① 站点频次门（filter_fast_stations_only=True 时非白名单站点被挡）
    strat, res, *_ = _next_entry_case("0.25", city=make_city("testville", "UTC", "TEST"),
                                      cfg_extra={"filter_fast_stations_only": True})
    assert res["action"] == "skip" and res["reason"] == "infrequent_metar_station", res
    assert res.get("entry_channel") != "next_bucket"

    # ② 时间窗门（Paris 06:00Z = 08:00 本地，不在 14–18）
    strat, res, *_ = _next_entry_case("0.25", now_utc=datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc))
    assert res["action"] == "skip" and "outside_time_window" in res["reason"], res

    # ③ 预期极值桶位门（气温还没到预期极值）
    strat, res, *_ = _next_entry_case("0.25", temp=30.2)
    assert res["action"] == "skip" and "not_reached_expected_high" in res["reason"], res

    # ④ 速度/变率门（刚跳升未停滞）
    strat = ConsensusLockStrategy(NEXT_CFG)
    city = make_city("helsinki", "Europe/Helsinki", "EFHK")
    bks = make_buckets()
    tracker = ConsensusTracker()
    tracker.record_books("helsinki", "2026-09-10", "high", bks, {
        "Y31": {"best_bid": "0.65", "best_ask": "0.70"}, "Y32": {"best_bid": "0.08", "best_ask": "0.12"}},
        datetime(2026, 9, 10, 12, 10, 0, tzinfo=timezone.utc))
    books = {"Y31": {"best_bid": "0.80", "best_ask": "0.90"},
             "Y32": {"best_bid": "0.10", "best_ask": "0.25"}}
    t0 = datetime(2026, 9, 10, 12, 10, 0, tzinfo=timezone.utc)
    strat.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, {"temp_c": 30.0, "obs_age_s": 600},
                         books, tracker, t0)
    t1 = t0 + timedelta(minutes=10)
    res = strat.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, {"temp_c": 31.0, "obs_age_s": 60},
                               books, tracker, t1)
    assert res["action"] == "skip" and "temperature_rising_velocity_active" in res["reason"], res
    assert res.get("entry_channel") != "next_bucket"

    # ⑤ 共识 rank1 门（rank1 是另一个桶 ⇒ 新通道同样拒绝，哪怕下一档 ask 在窗口内）
    strat = ConsensusLockStrategy(NEXT_CFG)
    tracker = ConsensusTracker()
    for step in range(10):
        ts = datetime(2026, 9, 10, 12, 10 + step, 0, tzinfo=timezone.utc)
        tracker.record_books("paris", "2026-09-10", "high", bks, {
            "Y31": {"best_bid": "0.10", "best_ask": "0.12"},       # target 便宜（非 rank1）
            "Y32": {"best_bid": "0.65", "best_ask": "0.70"},       # 别的桶才是 rank1
        }, ts)
    res = strat.evaluate_entry(make_city("paris"), "2026-09-10", "high", bks, 31.0,
                               {"temp_c": 31.2, "obs_age_s": 2000}, {
                                   "Y31": {"best_bid": "0.10", "best_ask": "0.60"},
                                   "Y32": {"best_bid": "0.10", "best_ask": "0.25"}},
                               tracker, datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc))
    assert res["action"] == "skip" and "target_is_not_rank1" in res["reason"], res
    assert res.get("entry_channel") != "next_bucket"
    assert strat.last_next_entry_skip and "target_is_not_rank1" in strat.last_next_entry_skip
    assert key not in strat.state.open_positions
    print("PASS: 15. test_next_entry_non_price_gates_not_relaxed "
          "(站点/时间窗/极值桶位/变率/共识 rank1 五门全部仍然拦截)")


def test_budget_base_unified_fire_budget():
    """F-A（审计 MEDIUM）：**唯一预算基数 = 生效 fire 预算** + 参数化不变量。

    修复前：策略用 ``order_budget_usdc``（config=15.0）算"新通道已占用"，引擎按 ``fire_budget_usdc``
    发单 ⇒ fire=12 时既有通道被少给 1.5 USDC；fire=30 时 15.0 + 22.5 = 37.5 > 30（不变量被打破）。
    现在两处同源（引擎把生效 fire 预算注入策略 cfg），于是对任意 fire / pct：
    ``新通道预算 + 既有通道剩余 ≤ fire`` 且 ``计划名义额合计 ≤ fire``。
    """
    key = "paris|2026-09-10|high"
    # (0) 解析器语义：fire_budget_usdc 优先；缺失/不可解析 ⇒ 回退**废弃**键 order_budget_usdc；皆无 ⇒ 15.0
    assert ConsensusLockStrategy({"fire_budget_usdc": "12", "order_budget_usdc": "15.0"}).order_budget() == Decimal("12")
    assert ConsensusLockStrategy({"fire_budget_usdc": 30, "order_budget_usdc": "15.0"}).order_budget() == Decimal("30")
    assert ConsensusLockStrategy({"fire_budget_usdc": None, "order_budget_usdc": "7.5"}).order_budget() == Decimal("7.5")
    assert ConsensusLockStrategy({"fire_budget_usdc": "abc", "order_budget_usdc": "7.5"}).order_budget() == Decimal("7.5")
    assert ConsensusLockStrategy({"order_budget_usdc": "15.0"}).order_budget() == Decimal("15.0")
    assert ConsensusLockStrategy({}).order_budget() == Decimal("15.0")

    print("  [F-A] 唯一基数 = 生效 fire 预算；参数化矩阵 fire × next_entry_budget_pct")
    for fire in ("5", "10", "12", "15", "30", "50"):
        for pct in ("0.5", "0.0", "1.0"):
            fire_d, pct_d = Decimal(fire), Decimal(pct)
            # 引擎注入后的策略 cfg 形态（`_r_cycle._get_consensus_lock_strat`）：
            # fire_budget_usdc = 生效 fire 预算；order_budget_usdc 故意留成 15.0（与 fire 不等）
            # ⇒ 当前代码若仍读旧键，下面的断言全部失败。
            strat, res, *_ = _next_entry_case(
                "0.22", ask_target="0.60",
                cfg_extra={"fire_budget_usdc": fire, "next_entry_budget_pct": pct})
            assert strat.order_budget() == fire_d, (fire, pct, strat.order_budget())
            want_next = (fire_d * pct_d).quantize(Decimal("0.01"))
            if pct_d <= Decimal("0"):
                # pct 非法（0.0）⇒ 新通道**弃单**（只记 last_next_entry_skip，既有语义），
                # 既有通道随后按自己的窗口独立成交，且用**统一基数**（= fire）sizing
                assert "next_entry_budget_pct_invalid" in (strat.last_next_entry_skip or ""), \
                    (fire, strat.last_next_entry_skip)
                assert strat.state.session_next_entry_used.get(key) is None, (fire, pct)
                assert res["action"] == "execute_taker_fire", (fire, pct, res)
                assert res["entry_channel"] == "target_bucket", (fire, pct, res)
                assert Decimal(res["cost_usdc"]) <= fire_d, (fire, pct, res)   # 吃满但不超过 fire
                new_budget, new_planned = Decimal("0"), Decimal("0")
            else:
                assert res["action"] == "execute_taker_fire", (fire, pct, res)
                assert res["entry_channel"] == "next_bucket", (fire, pct, res)
                assert res["budget_usdc"] == str(want_next), (fire, pct, res["budget_usdc"], want_next)
                new_budget = Decimal(res["budget_usdc"])
                new_planned = Decimal(res["cost_usdc"])           # shares × ask（真实计划名义额）
                assert strat.state.session_next_entry_used[key] == want_next, (fire, pct)
                # 策略侧预算 == 引擎侧预算（`_r_cycle.consensus_entry_fire` 的 (fire × pct).quantize）
                assert new_budget == (fire_d * pct_d).quantize(Decimal("0.01")), (fire, pct)
                assert new_planned <= new_budget, (fire, pct, new_planned, new_budget)
            existing_budget = strat.target_channel_budget(key, fire_d)
            # 不变量 ①：两通道额度均 ≥ 0；②：合计 ≤ fire；③：计划名义额合计 ≤ fire
            assert new_budget >= Decimal("0") and existing_budget >= Decimal("0"), (fire, pct)
            assert new_budget + existing_budget <= fire_d, (fire, pct, new_budget, existing_budget)
            # 既有通道在同一价格（0.60）下的计划名义额 = floor(剩余额度 / 0.60) × 0.60
            existing_planned = ((existing_budget / Decimal("0.60")).to_integral_value(rounding=ROUND_DOWN)
                                * Decimal("0.60"))
            assert existing_planned <= existing_budget, (fire, pct)
            assert new_planned + existing_planned <= fire_d, (
                fire, pct, new_planned, existing_planned, "计划名义额合计必须 ≤ fire 预算")
            # 回归证据（修复前的旧基数 = order_budget_usdc=15.0）：
            # 旧引擎侧新通道预算 = fire × pct，旧策略侧已占用 = 15 × pct ⇒ 旧既有通道剩余 = fire − 15×pct
            legacy_engine_new = (fire_d * pct_d).quantize(Decimal("0.01"))
            legacy_existing = max(Decimal("0"), fire_d - (Decimal("15.0") * pct_d).quantize(Decimal("0.01")))
            if pct_d > Decimal("0"):
                if fire_d > Decimal("15.0"):
                    # fire 高于旧基数 ⇒ 旧行为**必然**越界（审计实测 fire=30/pct=0.5 ⇒ 37.5 > 30）
                    assert legacy_engine_new + legacy_existing > fire_d, (fire, pct)
                elif fire_d < Decimal("15.0"):
                    # fire 低于旧基数 ⇒ 旧行为**静默少给**既有通道（审计实测 fire=12 ⇒ 4.5 而非 6.0）；
                    # 当新通道已吃满 fire（pct=1.0）时两者同为 0，故只在剩余 > 0 时要求严格更少。
                    assert legacy_existing <= existing_budget, (fire, pct, legacy_existing, existing_budget)
                    if existing_budget > Decimal("0"):
                        assert legacy_existing < existing_budget, (fire, pct, legacy_existing, existing_budget)
                else:
                    # fire == 旧基数 ⇒ 旧行为与新行为一致（对账：证明差异只源于基数不一致）
                    assert legacy_existing == existing_budget, (fire, pct, legacy_existing, existing_budget)
            print(f"    fire={fire:>3} pct={pct}  新通道预算={new_budget:>5}  "
                  f"既有通道剩余={existing_budget:>5}  合计={new_budget + existing_budget:>5} ≤ {fire_d}"
                  f"   计划名义额合计={new_planned + existing_planned} ≤ {fire_d}")
    print("PASS: 16. test_budget_base_unified_fire_budget "
          "(唯一基数=fire 预算；18 组 fire×pct 不变量全部成立；旧基数越界被实证)")


def test_next_entry_closed_lower_bound_deployed_window():
    """B（2026-09-13；同日上界复核后放宽）部署窗口 ``[0.27, 0.40]`` **闭区间**。

    ask 恰为 0.27 ⇒ 入场；0.269 ⇒ 弃单；ask 恰为 0.40 ⇒ 入场；0.401 ⇒ 弃单。
    用户口径原文为 ``0.27 <= ask <= 0.40``（两端含等号），旧代码是半开 ``(0.27, 0.32]``
    ⇒ ask == 0.27 被误弃。本用例把**部署值（0.27 / 0.40）**钉死。
    """
    win = ConsensusLockStrategy({"next_entry_min_ask": "0.27",
                                 "next_entry_max_ask": "0.40"}).next_entry_window()
    assert win["ok"] and win["label"] == "[0.27, 0.40]", win
    assert "closed" in win["detail"] and "0.27" in win["detail"], win
    s_in, r_in, *_ = _next_entry_case("0.27", cfg_extra={"next_entry_min_ask": "0.27",
                                                        "next_entry_max_ask": "0.40"})
    assert r_in["action"] == "execute_taker_fire" and r_in["entry_channel"] == "next_bucket", r_in
    assert r_in["window"] == "[0.27, 0.40]" and r_in["floor"] == "0.27", r_in
    s_low, r_low, *_ = _next_entry_case("0.269", cfg_extra={"next_entry_min_ask": "0.27",
                                                           "next_entry_max_ask": "0.40"})
    assert r_low["action"] == "skip", r_low
    assert "next_entry_ask_below_window" in (s_low.last_next_entry_skip or ""), s_low.last_next_entry_skip
    assert s_low.state.session_next_entry_used.get("paris|2026-09-10|high") is None
    s_hi, r_hi, *_ = _next_entry_case("0.40", cfg_extra={"next_entry_min_ask": "0.27",
                                                        "next_entry_max_ask": "0.40"})
    assert r_hi["action"] == "execute_taker_fire", r_hi
    s_over, r_over, *_ = _next_entry_case("0.401", cfg_extra={"next_entry_min_ask": "0.27",
                                                             "next_entry_max_ask": "0.40"})
    assert r_over["action"] == "skip", r_over
    assert "next_entry_ask_above_window" in (s_over.last_next_entry_skip or ""), \
        s_over.last_next_entry_skip
    print("PASS: 17. test_next_entry_closed_lower_bound_deployed_window "
          "(0.27 入场 / 0.269 弃单 next_entry_ask_below_window / 0.40 入场 / 0.401 弃单)")


def test_stop_loss_threshold_decoupled_by_channel():
    """① 止损门限按通道解耦：目标桶沿用 0.45；next_bucket = max(入场实际均价 × 0.50, 0.12)。"""
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 20, 0, tzinfo=timezone.utc)
    old = (now_utc - timedelta(hours=2)).isoformat()

    def _strat(avg_price: str, channel: str):
        s = ConsensusLockStrategy({"next_bucket_stop_loss_pct": Decimal("0.50"),
                                   "next_bucket_bid_floor": Decimal("0.12")})
        s.state.open_positions[key] = PositionRecord(
            session_key=key, bucket_id="b31", yes_token_id="Y31",
            shares=Decimal("25"), cost_usdc=Decimal("8.00"), avg_price=Decimal(avg_price),
            entry_ts_utc=old,
        )
        s.state.locked_sessions[key] = {"entry_channel": channel}
        return s

    def _books(bid: str):
        return {"Y31": {"best_bid": bid, "best_ask": "0.60"},
                "Y32": {"best_bid": "0.10", "best_ask": "0.12"}}

    # 触发线（纯函数）：0.32 入场 ⇒ max(0.16, 0.12) = 0.16；0.20 入场 ⇒ max(0.10, 0.12) = 0.12
    s = _strat("0.32", "next_bucket")
    assert s.stop_loss_bid_floor("next_bucket", Decimal("0.32")) == Decimal("0.1600")
    assert s.stop_loss_bid_floor("next_bucket", Decimal("0.20")) == Decimal("0.1200")
    assert s.stop_loss_bid_floor("target_bucket", Decimal("0.32")) == Decimal("0.45")
    # 零 0.45 残留：0.17 的 bid 在新通道下**不**触发（旧行为必触发）
    quiet = _strat("0.32", "next_bucket")
    assert quiet.evaluate_early_stop_loss(key, "high", bks, _books("0.17"), now_utc) is None
    assert quiet.state.open_positions[key].liquidated is False
    # bid 0.15 < 0.16 ⇒ 触发，reason 自证来源（chan / floor / entry × pct）
    hot = _strat("0.32", "next_bucket")
    res = hot.evaluate_early_stop_loss(key, "high", bks, _books("0.15"), now_utc)
    assert res and res["action"] == "early_stop_loss_executed", res
    assert res["entry_channel"] == "next_bucket", res
    assert res["reason"] == ("bid_floor_broken (chan=next_bucket, bid=0.15 < floor 0.16 "
                             "= entry 0.32 x 0.50)"), res["reason"]
    # 绝对地板兜底：0.20 入场 ⇒ 触发线 0.12（0.10 太低，被 0.12 托住）
    low = _strat("0.20", "next_bucket")
    assert low.evaluate_early_stop_loss(key, "high", bks, _books("0.13"), now_utc) is None
    low2 = _strat("0.20", "next_bucket")
    res_low = low2.evaluate_early_stop_loss(key, "high", bks, _books("0.11"), now_utc)
    assert res_low and "floor 0.12" in res_low["reason"], res_low
    # 目标桶通道：门限与 reason 串**逐字不变**（0.45）—— 同一个 0.17 的 bid 在目标桶下必触发
    tgt = _strat("0.32", "target_bucket")
    assert tgt.stop_loss_bid_floor("target_bucket", Decimal("0.32")) == Decimal("0.45")
    res_t17 = tgt.evaluate_early_stop_loss(key, "high", bks, _books("0.17"), now_utc)
    assert res_t17 and res_t17["reason"] == "bid_floor_broken (bid=0.17 < 0.45)", res_t17
    assert res_t17["entry_channel"] == "target_bucket", res_t17
    tgt2 = _strat("0.32", "target_bucket")
    res_t = tgt2.evaluate_early_stop_loss(key, "high", bks, _books("0.46"), now_utc)
    assert res_t is None, res_t
    # 无 locked_sessions（legacy/缺失）⇒ 视为目标桶通道（0.45）
    legacy = ConsensusLockStrategy()
    legacy.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31", shares=Decimal("25"),
        cost_usdc=Decimal("8.00"), avg_price=Decimal("0.32"), entry_ts_utc=old)
    assert legacy.stop_loss_channel(key) == "target_bucket"
    assert legacy.stop_loss_bid_floor("target_bucket", Decimal("0.32")) == Decimal("0.45")
    print("PASS: 18. test_stop_loss_threshold_decoupled_by_channel "
          "(next_bucket max(entry×0.50, 0.12)；目标桶逐字 0.45；reason 自证来源)")


def test_early_stop_grace_period():
    """② 开仓冷静期：距入场不足 1200s 时**仅条件 B** 不执行，且必须落审计（不静默）。"""
    bks = make_buckets()
    key = "paris|2026-09-10|high"
    now_utc = datetime(2026, 9, 10, 14, 20, 0, tzinfo=timezone.utc)
    collapse = {"Y31": {"best_bid": "0.10", "best_ask": "0.60"},
                "Y32": {"best_bid": "0.10", "best_ask": "0.12"}}
    surge = {"Y31": {"best_bid": "0.60", "best_ask": "0.65"},
             "Y32": {"best_bid": "0.36", "best_ask": "0.38"}}

    def _fresh(entry_age_s: float, channel: str = "next_bucket", avg: str = "0.32"):
        s = ConsensusLockStrategy({"early_stop_grace_seconds": 1200})
        s.state.open_positions[key] = PositionRecord(
            session_key=key, bucket_id="b31", yes_token_id="Y31", shares=Decimal("25"),
            cost_usdc=Decimal("8.00"), avg_price=Decimal(avg),
            entry_ts_utc=(now_utc - timedelta(seconds=entry_age_s)).isoformat())
        s.state.locked_sessions[key] = {"entry_channel": channel}
        return s

    # (a) 刚入场（0s）⇒ 条件 B 被抑制：只记审计、不平仓、不熔断
    s = _fresh(0)
    sup = s.evaluate_early_stop_loss(key, "high", bks, collapse, now_utc)
    assert sup and sup["action"] == "early_stop_suppressed", sup
    assert "bid_floor_broken" in sup["would_be_reason"], sup
    assert sup["session_key"] == key and sup["entry_channel"] == "next_bucket", sup
    assert sup["bid"] == "0.10" and sup["floor"] == "0.16", sup
    assert sup["grace_seconds"] == 1200 and sup["remaining_grace_seconds"] == 1200, sup
    assert sup["duplicate"] is False, sup
    assert s.state.open_positions[key].liquidated is False, "被抑制不得平仓"
    assert key not in s.state.stopped_out_sessions and key not in s.state.breached_sessions
    # 同一轮/同一形态再评估 ⇒ 去重（不刷屏）；仓位仍 open
    again = s.evaluate_early_stop_loss(key, "high", bks, collapse, now_utc)
    assert again and again["action"] == "early_stop_suppressed" and again["duplicate"] is True, again
    # (b) 冷静期过（1201s）⇒ 正常执行；门限按通道解耦（0.16）
    s2 = _fresh(1201)
    done = s2.evaluate_early_stop_loss(key, "high", bks, collapse, now_utc)
    assert done and done["action"] == "early_stop_loss_executed", done
    assert "chan=next_bucket" in done["reason"], done
    assert s2.state.open_positions[key].liquidated is True
    assert key in s2.state.stopped_out_sessions
    # (c) 条件 A（next_bucket_surge）**不受**冷静期影响：刚入场也照常止损
    s3 = _fresh(0)
    a_res = s3.evaluate_early_stop_loss(key, "high", bks, surge, now_utc)
    assert a_res and a_res["action"] == "early_stop_loss_executed", a_res
    assert "next_bucket_surge" in a_res["reason"], a_res
    # (d) 目标桶通道的新仓同样受冷静期保护（冷静期与通道无关）
    s4 = _fresh(0, channel="target_bucket", avg="0.65")
    t_res = s4.evaluate_early_stop_loss(key, "high", bks, collapse, now_utc)
    assert t_res and t_res["action"] == "early_stop_suppressed", t_res
    assert t_res["entry_channel"] == "target_bucket", t_res
    # (e) 时间戳缺失/不可解析 ⇒ 不抑制（fail-open 到风控，绝不因为读了坏时间戳而漏掉止损）
    s5 = _fresh(0)
    s5.state.open_positions[key].entry_ts_utc = "not-a-timestamp"
    e_res = s5.evaluate_early_stop_loss(key, "high", bks, collapse, now_utc)
    assert e_res and e_res["action"] == "early_stop_loss_executed", e_res
    # (f) grace=0（显式关闭）⇒ 不抑制
    s6 = ConsensusLockStrategy({"early_stop_grace_seconds": 0})
    s6.state.open_positions[key] = PositionRecord(
        session_key=key, bucket_id="b31", yes_token_id="Y31", shares=Decimal("25"),
        cost_usdc=Decimal("8.00"), avg_price=Decimal("0.32"), entry_ts_utc=now_utc.isoformat())
    s6.state.locked_sessions[key] = {"entry_channel": "next_bucket"}
    z_res = s6.evaluate_early_stop_loss(key, "high", bks, collapse, now_utc)
    assert z_res and z_res["action"] == "early_stop_loss_executed", z_res
    print("PASS: 19. test_early_stop_grace_period "
          "(条件 B 冷静期抑制+审计+去重；条件 A 不受影响；目标桶同样受保护；坏时间戳/grace=0 不抑制)")


def main():
    test_station_filter()
    test_time_window()
    test_capped_taker_entry()
    test_next_bucket_barrier_reject()
    test_pre_metar_early_stop_next_bucket_surge()
    test_pre_metar_early_stop_bid_floor()
    test_breach_risk_control_and_no_defence()
    test_max_fires_cap_enforcement()
    test_post_stop_loss_cooldown()
    test_temperature_velocity_stalling_filter()
    test_next_bucket_instantaneous_book_checks()
    test_next_entry_channel_default_off_and_config_gate()
    test_next_entry_window_boundary_matrix()
    test_next_entry_priority_and_budget_isolation()
    test_next_entry_non_price_gates_not_relaxed()
    test_budget_base_unified_fire_budget()
    test_next_entry_closed_lower_bound_deployed_window()
    test_stop_loss_threshold_decoupled_by_channel()
    test_early_stop_grace_period()
    print("\nALL 19 OPTIMIZATION UNIT TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
