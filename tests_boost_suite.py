import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

from strategy_consensus_lock import (
    ConsensusLockStrategy,
    DEFAULT_CONFIG,
    CHANNEL_NEXT,
    CHANNEL_TARGET,
)
from consensus_tracker import ConsensusTracker
from live.exit import (
    CH_TAKE_PROFIT,
    CH_ABORTION_RC,
    CH_BREACH_RC,
    LiveExitChannel,
    exit_settings,
    apply_live_exit_fill,
)
from live.v2_transport import EXIT_CHANNELS


class TestBoostSuite(unittest.TestCase):

    def setUp(self):
        self.prod_cfg = {
            "filter_fast_stations_only": False,
            "high_local_start": 12.5,
            "high_local_end": 18,
            "low_local_start": 0,
            "low_local_end": 9,
            "next_bucket_max_twap": Decimal("0.26"),
            "next_bucket_twap_window_s": 3600,
            "next_bucket_max_instant_ask": Decimal("0.30"),
            "next_bucket_max_instant_bid": Decimal("0.15"),
            "min_dwell_seconds_if_rising": 1800,
            "entry_mode": "capped_taker",
            "yes_min_ask": Decimal("0.42"),
            "yes_max_ask": Decimal("0.86"),
            "next_entry_enabled": True,
            "next_entry_min_ask": Decimal("0.27"),
            "next_entry_max_ask": Decimal("0.40"),
            "next_entry_min_bid": Decimal("0.18"),
            "next_entry_max_spread": Decimal("0.12"),
            "next_entry_high_cutoff_hour": 16,
            "take_profit_enabled": True,
            "take_profit_multiplier": Decimal("1.50"),
            "weather_abortion_enabled": True,
            "weather_abortion_temp_drop": 2.5,
            "weather_abortion_cutoff_hour": 15,
            "order_budget_usdc": Decimal("10.0"),
            "fire_budget_usdc": Decimal("10.0"),
            "next_entry_budget_pct": Decimal("0.5"),
        }

    def _get_strat(self):
        return ConsensusLockStrategy(self.prod_cfg)

    def _make_paris_market(self):
        city = {"city_id": "paris", "icao": "LFPB", "timezone": "Europe/Paris", "market_unit": "C"}
        bks = [
            {"bucket_id": "b29", "lo": 29.0, "hi": 30.0, "yes_token_id": "Y29", "no_token_id": "N29"},
            {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "Y30", "no_token_id": "N30"},
            {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "Y31", "no_token_id": "N31"},
            {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "Y32", "no_token_id": "N32"},
            {"bucket_id": "b33", "lo": 33.0, "hi": 34.0, "yes_token_id": "Y33", "no_token_id": "N33"},
        ]
        return city, bks

    def _setup_tracker_and_books(self, city, bks, now_utc, target_ask="0.60", next_ask="0.30", next_bid="0.22"):
        tracker = ConsensusTracker()
        for step in range(10):
            ts = now_utc - timedelta(minutes=20 - step)
            tracker.record_books("paris", "2026-09-10", "high", bks, {
                "Y31": {"best_bid": "0.58", "best_ask": "0.60"},
                "Y32": {"best_bid": "0.10", "best_ask": "0.15"},
            }, ts)
        books = {
            "Y31": {"best_bid": "0.58", "best_ask": target_ask, "tick_size": "0.01"},
            "Y32": {"best_bid": next_bid, "best_ask": next_ask, "tick_size": "0.01"},
        }
        return tracker, books

    # =========================================================================
    # 模块 ①: 规避假突破
    # =========================================================================
    def test_module1_solar_cutoff_hour(self):
        """16:00 后禁开突破单 (Solar Cutoff)."""
        city, bks = self._make_paris_market()
        obs = {"temp_c": 31.2, "obs_age_s": 60.0}

        # 13:50 UTC = 15:50 Paris (pass)
        now_before = datetime(2026, 9, 10, 13, 50, 0, tzinfo=timezone.utc)
        tracker_before, books_before = self._setup_tracker_and_books(city, bks, now_before, target_ask="0.90", next_ask="0.30")
        strat_before = self._get_strat()
        res_before = strat_before.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_before, tracker_before, now_before)
        self.assertEqual(res_before["action"], "execute_taker_fire")
        self.assertEqual(res_before["entry_channel"], CHANNEL_NEXT)

        # 14:05 UTC = 16:05 Paris (blocked by cutoff)
        now_after = datetime(2026, 9, 10, 14, 5, 0, tzinfo=timezone.utc)
        tracker_after, books_after = self._setup_tracker_and_books(city, bks, now_after, target_ask="0.90", next_ask="0.30")
        strat_after = self._get_strat()
        res_after = strat_after.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_after, tracker_after, now_after)
        self.assertEqual(res_after["action"], "skip")
        self.assertIn("next_entry_after_solar_cutoff", strat_after.last_next_entry_skip)

    def test_module1_weather_veto_convective(self):
        """METAR 遇 CB/TS/RA 一票否决."""
        city, bks = self._make_paris_market()
        now = datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)  # 15:00 Paris
        tracker, books = self._setup_tracker_and_books(city, bks, now, target_ask="0.90", next_ask="0.30")

        # Case A: rawOb contains TSRA
        obs_tsra = {"temp_c": 31.2, "rawOb": "LFPB 101300Z 24012KT 4000 TSRA SCT020CB 31/22 Q1013"}
        strat_a = self._get_strat()
        res_tsra = strat_a.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs_tsra, books, tracker, now)
        self.assertEqual(res_tsra["action"], "skip")
        self.assertIn("weather_veto_convective", strat_a.last_next_entry_skip)

        # Case B: wxString contains +RA
        obs_ra = {"temp_c": 31.2, "wxString": "+RA"}
        strat_b = self._get_strat()
        res_ra = strat_b.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs_ra, books, tracker, now)
        self.assertEqual(res_ra["action"], "skip")
        self.assertIn("weather_veto_convective", strat_b.last_next_entry_skip)

        # Case C: clouds contain CB
        obs_cb = {"temp_c": 31.2, "clouds": "FEW025CB"}
        strat_c = self._get_strat()
        res_cb = strat_c.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs_cb, books, tracker, now)
        self.assertEqual(res_cb["action"], "skip")
        self.assertIn("weather_veto_convective", strat_c.last_next_entry_skip)

    def test_module1_price_band_and_quality(self):
        """价格死守 [0.27, 0.40] 且要求 Bid >= 0.18 与 Spread <= 0.12."""
        city, bks = self._make_paris_market()
        now = datetime(2026, 9, 10, 13, 0, 0, tzinfo=timezone.utc)
        obs = {"temp_c": 31.2, "obs_age_s": 60.0}

        # 1. Ask < 0.27 拦截
        tracker, books_low = self._setup_tracker_and_books(city, bks, now, target_ask="0.90", next_ask="0.269", next_bid="0.20")
        strat_low = self._get_strat()
        strat_low.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_low, tracker, now)
        self.assertIn("next_entry_ask_below_window", strat_low.last_next_entry_skip)

        # 2. Ask > 0.40 拦截
        tracker, books_hi = self._setup_tracker_and_books(city, bks, now, target_ask="0.90", next_ask="0.401", next_bid="0.32")
        strat_hi = self._get_strat()
        strat_hi.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_hi, tracker, now)
        self.assertIn("next_entry_ask_above_window", strat_hi.last_next_entry_skip)

        # 3. Bid < 0.18 拦截
        tracker, books_low_bid = self._setup_tracker_and_books(city, bks, now, target_ask="0.90", next_ask="0.30", next_bid="0.17")
        strat_low_bid = self._get_strat()
        strat_low_bid.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_low_bid, tracker, now)
        self.assertIn("next_entry_bid_too_low", strat_low_bid.last_next_entry_skip)

        # 4. Spread > 0.12 拦截 (0.35 - 0.20 = 0.15 > 0.12)
        tracker, books_wide = self._setup_tracker_and_books(city, bks, now, target_ask="0.90", next_ask="0.35", next_bid="0.20")
        strat_wide = self._get_strat()
        strat_wide.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_wide, tracker, now)
        self.assertIn("next_entry_spread_too_wide", strat_wide.last_next_entry_skip)

        # 5. 质量合格放行 (Ask=0.30, Bid=0.22, Spread=0.08 <= 0.12)
        tracker, books_ok = self._setup_tracker_and_books(city, bks, now, target_ask="0.90", next_ask="0.30", next_bid="0.22")
        strat_ok = self._get_strat()
        res_ok = strat_ok.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_ok, tracker, now)
        self.assertEqual(res_ok["action"], "execute_taker_fire")
        self.assertEqual(res_ok["entry_channel"], CHANNEL_NEXT)

    # =========================================================================
    # 模块 ②: 50% 动态阶梯止盈 (Take-Profit 50% Ladder)
    # =========================================================================
    def test_module2_take_profit_ladder(self):
        """Bid >= avg_price * 1.50 时自动以 FAK 卖出一半仓位收回本金，打上单次标记."""
        strat = self._get_strat()
        now = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
        session_key = "paris|2026-09-10|high"
        pos = {
            "key": session_key,
            "take_profit_done": False,
            "legs": [
                {
                    "outcome": "YES",
                    "token_id": "Y32",
                    "bucket_id": "b32",
                    "shares": "30.0",
                    "avg_price": "0.30",
                    "cost_usdc": "9.0",
                    "settled": False,
                }
            ],
        }

        # 场景 A: Bid = 0.40 (< 0.30 * 1.50 = 0.45) -> 不触发
        books_low = {"Y32": {"best_bid": "0.40", "best_ask": "0.44"}}
        res_a = strat.evaluate_take_profit(session_key, pos, books_low, now)
        self.assertIsNone(res_a)

        # 场景 B: Bid = 0.46 (>= 0.45) -> 触发止盈，卖出 15.0 股 (30.0 * 0.5)
        books_tp = {"Y32": {"best_bid": "0.46", "best_ask": "0.50"}}
        res_b = strat.evaluate_take_profit(session_key, pos, books_tp, now)
        self.assertIsNotNone(res_b)
        self.assertEqual(res_b["action"], "take_profit")
        self.assertEqual(res_b["shares_to_sell"], Decimal("15.0"))
        self.assertEqual(res_b["best_bid"], Decimal("0.46"))

        # 模拟执行止盈记账
        fill = {"ok": True, "status": "matched", "filled_shares": "15.0", "avg_price": "0.46"}
        state = {"cash_pool_usdc": "100.0", "positions": {session_key: pos}}
        ledger_res = apply_live_exit_fill(
            state=state, pos=pos, leg=pos["legs"][0], fill=fill, channel=CH_TAKE_PROFIT,
            floor=Decimal("0.05"), fee_rate=Decimal("0.02"), now_utc=now
        )
        self.assertTrue(ledger_res["sold"])
        self.assertFalse(ledger_res["pos_liquidated"])  # 仓位保持 open!
        self.assertEqual(pos["legs"][0]["shares"], "15.0000")  # 剩余 15 股!
        self.assertNotIn("pending_exit", pos)  # 止盈绝不误登记 pending_exit!
        pos["take_profit_done"] = True

        # 场景 C: 单次标记生效，后续即使 Bid 暴涨到 0.80 也不重复触发
        books_high = {"Y32": {"best_bid": "0.80", "best_ask": "0.85"}}
        res_c = strat.evaluate_take_profit(session_key, pos, books_high, now)
        self.assertIsNone(res_c)

    # =========================================================================
    # 模块 ③: 通道 A 吃单概率优化
    # =========================================================================
    def test_module3_channel_a_window_and_price_band(self):
        """时间提前至 12:30，价格带放宽至 [0.42, 0.86]，下一档门限放宽至 0.30."""
        city, bks = self._make_paris_market()
        obs = {"temp_c": 31.0, "obs_age_s": 60.0}

        # 1. 时间窗 12:30 (10:30 UTC = 12:30 Paris -> 放行)
        strat_time = self._get_strat()
        now_1230 = datetime(2026, 9, 10, 10, 30, 0, tzinfo=timezone.utc)
        in_win, hr = strat_time.is_in_time_window(now_1230, city["timezone"], "high")
        self.assertTrue(in_win)
        self.assertEqual(hr, 12)

        # 12:20 (10:20 UTC = 12:20 Paris -> 拦截)
        now_1220 = datetime(2026, 9, 10, 10, 20, 0, tzinfo=timezone.utc)
        in_win_early, _ = strat_time.is_in_time_window(now_1220, city["timezone"], "high")
        self.assertFalse(in_win_early)

        # 2. 目标桶价格带 [0.42, 0.86]
        # 0.41 弃单 (低于 0.42)
        strat_41 = self._get_strat()
        tracker_41, books_41 = self._setup_tracker_and_books(city, bks, now_1230, target_ask="0.41", next_ask="0.10", next_bid="0.08")
        res_41 = strat_41.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_41, tracker_41, now_1230)
        self.assertIn("ask_below_confirmation_floor", res_41["reason"])

        # 0.42 吃单放行
        strat_42 = self._get_strat()
        tracker_42, books_42 = self._setup_tracker_and_books(city, bks, now_1230, target_ask="0.42", next_ask="0.10", next_bid="0.08")
        res_42 = strat_42.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_42, tracker_42, now_1230)
        self.assertEqual(res_42["action"], "execute_taker_fire")
        self.assertEqual(res_42["entry_channel"], CHANNEL_TARGET)

        # 0.86 吃单放行
        strat_86 = self._get_strat()
        tracker_86, books_86 = self._setup_tracker_and_books(city, bks, now_1230, target_ask="0.86", next_ask="0.10", next_bid="0.08")
        res_86 = strat_86.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_86, tracker_86, now_1230)
        self.assertEqual(res_86["action"], "execute_taker_fire")
        self.assertEqual(res_86["entry_channel"], CHANNEL_TARGET)

        # 0.87 弃单
        strat_87 = self._get_strat()
        tracker_87, books_87 = self._setup_tracker_and_books(city, bks, now_1230, target_ask="0.87", next_ask="0.10", next_bid="0.08")
        res_87 = strat_87.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_87, tracker_87, now_1230)
        self.assertIn("ask_above_safety_cap", res_87["reason"])

        # 3. 下一档瞬时 Ask 门限放宽至 0.30
        # next_ask = 0.28 (<= 0.30) -> 放行
        strat_next28 = self._get_strat()
        tracker_next28, books_next28 = self._setup_tracker_and_books(city, bks, now_1230, target_ask="0.65", next_ask="0.28", next_bid="0.08")
        res_next28 = strat_next28.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_next28, tracker_next28, now_1230)
        self.assertEqual(res_next28["action"], "execute_taker_fire")

        # next_ask = 0.31 (> 0.30) -> 拦截
        strat_next31 = self._get_strat()
        tracker_next31, books_next31 = self._setup_tracker_and_books(city, bks, now_1230, target_ask="0.65", next_ask="0.31", next_bid="0.08")
        res_next31 = strat_next31.evaluate_entry(city, "2026-09-10", "high", bks, 31.0, obs, books_next31, tracker_next31, now_1230)
        self.assertIn("next_bucket_instant_ask_too_high", res_next31["reason"])

    # =========================================================================
    # 模块 ④: 敏锐证伪与极速滑点逃生
    # =========================================================================
    def test_module4_weather_abortion_and_slippage_escape(self):
        """气温不可逆夭折证伪 (高点暴跌 >= 2.5C 且 METAR 遇 CB/TS/RA) + 滑点 0.02 深度吃单."""
        city, _ = self._make_paris_market()
        session_key = "paris|2026-09-10|high"
        strat = self._get_strat()

        # 模拟 14:00 气温达到日内峰值 34.0°C
        t1 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
        strat.record_metar_observation(session_key, 34.0, {"temp_c": 34.0}, t1)

        # 模拟 15:30 遭遇强雷雨，气温暴跌至 31.0°C (暴跌 3.0°C >= 2.5°C 阈值)
        t2 = datetime(2026, 9, 10, 13, 30, 0, tzinfo=timezone.utc)  # 15:30 Paris (>= 15:00 cutoff)
        obs_storm = {
            "temp_c": 31.0,
            "rawOb": "LFPB 101330Z 28025G40KT 2000 +TSRA BKN015CB 31/20 Q1012",
        }
        strat.record_metar_observation(session_key, 31.0, obs_storm, t2)

        # 判定气温不可逆夭折
        abortion = strat.handle_weather_abortion_risk_control(
            session_key, city, "high", 31.0, obs_storm, t2
        )
        self.assertIsNotNone(abortion)
        self.assertEqual(abortion["action"], "weather_abortion")
        self.assertEqual(abortion["peak_temp"], 34.0)
        self.assertEqual(abortion["temp_drop"], 3.0)

        # 验证极速滑点深度吃单: sell_leg 在 channel in (breach_rc, abortion_rc) 时挂 price = max(floor, best_bid - 0.02)
        mock_transport = MagicMock()
        mock_transport.execute_leg.return_value = {"ok": True, "status": "matched", "filled_shares": Decimal("20")}
        mock_transport.cancel_with_retry.return_value = {"ok": True, "canceled": []}

        mock_port = MagicMock()
        mock_port.transport = mock_transport
        mock_port.ensure_client.return_value = (MagicMock(), "mock")
        mock_port.resolve_neg_risk.return_value = (False, "mock")
        mock_port.gates = {"auth": True, "balance": True, "circuit": True}

        channel_obj = LiveExitChannel(mock_port)

        # 场景 A: best_bid = 0.20 -> 挂 0.18 (低于买一价 0.02)
        leg = {"token_id": "Y32", "leg": "YES", "shares": "20.0"}
        book_20 = {"best_bid": "0.20", "best_ask": "0.22", "neg_risk": False}
        channel_obj.sell_leg(leg=leg, book=book_20, floor=Decimal("0.05"), shares=Decimal("20.0"), channel=CH_ABORTION_RC)
        call_kwargs_a = mock_transport.execute_leg.call_args[1]
        self.assertEqual(str(call_kwargs_a["price"]), "0.18")

        # 场景 B: 同理验证 breach_rc 也扣减 0.02
        book_30 = {"best_bid": "0.30", "best_ask": "0.32", "neg_risk": False}
        channel_obj.sell_leg(leg=leg, book=book_30, floor=Decimal("0.05"), shares=Decimal("20.0"), channel=CH_BREACH_RC)
        call_kwargs_b = mock_transport.execute_leg.call_args[1]
        self.assertEqual(str(call_kwargs_b["price"]), "0.28")

        # 场景 C: 地板价守卫: best_bid = 0.06 -> max(0.05, 0.06 - 0.02) = 0.05
        book_06 = {"best_bid": "0.06", "best_ask": "0.08", "neg_risk": False}
        channel_obj.sell_leg(leg=leg, book=book_06, floor=Decimal("0.05"), shares=Decimal("20.0"), channel=CH_ABORTION_RC)
        call_kwargs_c = mock_transport.execute_leg.call_args[1]
        self.assertEqual(str(call_kwargs_c["price"]), "0.05")

    def test_fahrenheit_unit_conversion(self):
        """验证美洲 Fahrenheit 城市正确的单位换算，不出现摄氏度直比华氏度的假跳单."""
        from strategy_consensus_lock import ConsensusLockStrategy, ConsensusTracker
        cfg = {"fast_metar_stations": ["KAUS"]}
        strat = ConsensusLockStrategy(cfg)
        city = {"city_id": "austin", "timezone": "America/Chicago", "icao": "KAUS", "market_unit": "F"}
        buckets = [
            {"bucket_id": "b_lo", "lo": 80, "hi": 89, "yes_token_id": "tok_lo"},
            {"bucket_id": "b_mid", "lo": 90, "hi": 99, "yes_token_id": "tok_mid"},
            {"bucket_id": "b_hi", "lo": 100, "hi": 109, "yes_token_id": "tok_hi"}
        ]
        # 34.4°C 换算成华氏度应为 94°F (落在 b_mid [90, 99])
        metar_obs = {"temp_c": 34.4, "obs_age_s": 60, "rawOb": "METAR KAUS 141700Z 34/22"}
        tracker = ConsensusTracker()
        now_utc = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc) # 13:00 CDT
        res = strat.evaluate_entry(
            city=city, market_local_date="2026-09-14", direction="high",
            buckets=buckets, expected_extreme_temp=95.0, # 95.0°F 落在 b_mid
            metar_obs=metar_obs, books_by_token={}, tracker=tracker, now_utc=now_utc
        )
        # 此时 94°F 与 95°F 落在同一个 bucket (b_mid)，绝不会因 34.4 < 95.0 被错误判定为 not_reached_expected_high
        self.assertNotIn("34.4 < expected 95.0", res.get("reason", ""))


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestBoostSuite)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("\nALL BOOST SUITE OPTIMIZATION TESTS PASSED 100%!")
