#!/usr/bin/env python3
"""tests_breach_hedge.py — regression for the 破位反手/追火 (METAR hard-breach
hedge) branch of ``_r_cycle.run_cycle``.

Covers F2 (2026-09-12): the branch used ``ZoneInfo`` while the only binding in
that function lived *inside* the TAF block ⇒ with no TAF (the ``market_rank1``
fallback path) the name was unbound and the cycle died with
``UnboundLocalError`` at the exact moment the hedge fire was about to be
emitted. The fix binds ``ZoneInfo`` at the top of ``run_cycle``.

Assertions:
  1. no-TAF path: ``run_cycle`` does not raise, emits the hedge fire, and
     ``local_fire_time`` is iso-parseable with the city's real UTC offset;
  2. TAF path: the produced ``hedge_fire`` dict is byte-for-byte the frozen
     ccbd0c8 baseline golden (behaviour unchanged where the binding already
     existed);
  3. the two paths differ ONLY in ``ref_extreme`` (TAF TX 33.0 vs METAR 32.5);
  4. the fire must be audited (``fire_attempt`` row) and actually reach
     ``_paper_fire``.
  5. (2026-09-12) **audit fields** for the no-TAF exposure: the entry lane and the
     hedge lane are each run with and without TAF, and every
     ``fire`` / ``fire_attempt`` / ``breach_risk_control`` row must carry
     ``taf_present`` / ``taf_source`` / ``fire_path`` / ``fire_key`` with the right
     values (an entry-lane harness drives ``run_cycle`` through the real
     ``evaluate_entry``/``consensus_entry_fire``/``_record_fire_event`` with a
     seeded rank-1 tracker).

No network, no real orders: every external edge (rules/books/METAR/TAF/WS) is
stubbed; the paper port plus the real ``_paper_fire`` run for the audit-field
cases (paper fills only, nothing is ever sent to a venue).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import _r_cycle
from _r_globals import book_cache

#: the un-patched paper fire (the tests below swap ``_r_cycle._paper_fire`` for a recorder)
_REAL_PAPER_FIRE = _r_cycle._paper_fire

CITIES = {
    "paris": {"city_id": "paris", "icao": "LFPB", "timezone": "Europe/Paris",
              "market_unit": "C", "name": "Paris", "offset": "+0200"},
    "tokyo": {"city_id": "tokyo", "icao": "RJTT", "timezone": "Asia/Tokyo",
              "market_unit": "C", "name": "Tokyo", "offset": "+0900"},
}

# Frozen golden captured from the ccbd0c8 baseline in the *TAF* scenario. If the
# fix had changed anything on the TAF path this dict would not match.
TAF_GOLDEN = {
    "action_type": "re_fire",
    "broken_bucket_id": "b31",
    "budget_usdc": "10.0",
    "city_id": "paris",
    "direction": "high",
    "fire_no": 2,
    "icao": "LFPB",
    "jump": 1,
    "key": "paris|2026-09-11|high",
    "kind": "consensus_lock",
    "legs": [
        {"budget_usdc": "10.0", "cap": "0.85", "leg": "buy_no_broken",
         "outcome": "NO", "side": "BUY", "token_id": "N31"},
        {"budget_usdc": "15.0", "cap": "0.75", "leg": "buy_yes_new",
         "outcome": "YES", "side": "BUY", "token_id": "Y32"},
    ],
    "local_fire_time": "2026-09-11T14:00:00+02:00",
    "market_local_date": "2026-09-11",
    "market_unit": "C",
    "new_bucket_id": "b32",
    "ref_extreme": 33.0,
    "ref_source": "metar_breach_hedge",
    "running_extreme": 32.5,
}


def _run_breach_cycle(mode: str, city_key: str, *, real_fire: bool = False):
    """Run one deterministic breach/hedge cycle. Returns (captured, result).

    ``real_fire=True`` keeps the **real** ``_r_cycle._paper_fire`` (paper port) so the hedge fire
    actually fills and ``record_refire`` writes its own audit rows — that is what the audit-field
    tests need.  Default (``False``) is the original recorder stub the F2 regression uses.
    """
    from zoneinfo import ZoneInfo

    city = CITIES[city_key]
    icao = city["icao"]
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
    local_date = now.astimezone(ZoneInfo(city["timezone"])).date().isoformat()
    rule_key = f"{city['city_id']}|{local_date}|high"

    buckets = [
        {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "Y30", "no_token_id": "N30"},
        {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "Y31", "no_token_id": "N31"},
        {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "Y32", "no_token_id": "N32"},
    ]
    rule = {"city_id": city["city_id"], "market_local_date": local_date,
            "direction": "high", "buckets": buckets, "enabled": True}

    cache = book_cache()
    cache.clear()
    for tok, bid, ask in (("Y30", "0.01", "0.02"), ("N30", "0.90", "0.95"),
                          ("Y31", "0.55", "0.60"), ("N31", "0.75", "0.80"),
                          ("Y32", "0.28", "0.30"), ("N32", "0.68", "0.72")):
        cache[tok] = {"best_bid": bid, "best_ask": ask, "tick_size": "0.01",
                      "bids": [{"price": bid, "size": "500"}],
                      "asks": [{"price": ask, "size": "500"}]}

    metar = {icao: {"temp_c": 32.5, "obs_time": now - timedelta(seconds=60),
                    "source": "test", "raw": "TEST 111200Z 32005KT 32/20"}}
    taf = {icao: {"tx_c": 33.0, "tx_valid_utc": "2026-09-11T14:00:00Z",
                  "tn_c": 20.0, "tn_valid_utc": "2026-09-11T02:00:00Z"}} if mode == "taf" else {}

    state = {
        "paper_initial_capital_usdc": 700.0,
        "paper_total_debit_usdc": 15.0,
        "positions": {rule_key: {
            "key": rule_key, "kind": "consensus_lock", "city_id": city["city_id"],
            "direction": "high", "settled": False, "liquidated": False,
            "legs": [{"leg": "buy_yes_lock", "token_id": "Y31", "side": "BUY",
                      "outcome": "YES", "shares": "25.0", "cost_usdc": "15.0",
                      "avg_price": "0.60", "bucket_id": "b31", "settled": False}]}},
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
    }

    log_path = os.path.join(tempfile.mkdtemp(prefix="f2-breach-"), "events.jsonl")
    cfg = {
        "mode": "paper", "strategy_mode": "consensus_lock", "fire_budget_usdc": 10.0,
        "log_path": log_path, "market_ws_enabled": False, "ws_triggered_metar_enabled": False,
        "rules_refresh_interval_seconds": 10 ** 9, "idle_book_interval_seconds": 10 ** 9,
        "idle_metar_interval_seconds": 10 ** 9, "arm_metar_interval_seconds": 10 ** 9,
        "taf_refresh_interval_seconds": 10 ** 9, "settle_poll_seconds": 10 ** 9,
        "consensus_lock": {
            "filter_fast_stations_only": False, "early_stop_enabled": True,
            "early_stop_bid_floor": "0.45", "early_stop_next_bucket_surge": "0.35",
            "max_fires_per_session": 2, "risk_control_no_cap": "0.85",
            "risk_control_yes_cap": "0.75",
        },
    }

    _r_cycle._CONSENSUS_LOCK_STRAT = None
    _r_cycle.load_active_cities = lambda _cfg: [dict(city)]
    _r_cycle.target_dates_by_icao = lambda *a, **k: {}
    _r_cycle.refresh_rules = lambda *a, **k: None
    _r_cycle._load_rule_cache = lambda: ({rule_key: dict(rule)}, {})
    _r_cycle.prune_stale_sessions = lambda *a, **k: 0
    _r_cycle.refresh_books = lambda *a, **k: {}
    _r_cycle._fetch_metar = lambda *a, **k: dict(metar)
    _r_cycle._fetch_taf = lambda *a, **k: dict(taf)
    _r_cycle._LAST_GOOD_METAR.clear()
    _r_cycle._LAST_GOOD_TAF.clear()

    class _WS:
        running = False

        def start(self, *a, **k):
            pass

        def ensure_tokens(self, *a, **k):
            pass

        def telemetry(self):
            return {"running": False}

    _r_cycle.ws_bridge = lambda: _WS()

    captured: dict = {}

    def _recorder(cfg_, state_, fire_, now_):
        captured["hedge_fire"] = json.loads(json.dumps(fire_, default=str))
        captured["calls"] = captured.get("calls", 0) + 1
        if real_fire:
            return _REAL_PAPER_FIRE(cfg_, state_, fire_, now_)   # 真实 paper 填充 + record_refire 审计行
        return None, []

    _r_cycle._paper_fire = _recorder

    raised = None
    try:
        _r_cycle.run_cycle(cfg, state, now, force_metar=True, force_books=True, force_rules=True)
    except BaseException as exc:  # noqa: BLE001 — the whole point is to catch the defect
        raised = exc

    rows = []
    if os.path.exists(log_path):
        rows = [json.loads(l) for l in open(log_path, encoding="utf-8").read().splitlines() if l.strip()]
    _r_cycle._CONSENSUS_LOCK_STRAT = None
    return captured, {"raised": raised, "rows": rows, "log_path": log_path, "rule_key": rule_key}


def test_breach_hedge_no_taf_does_not_raise_and_fires():
    """The F2 defect: no TAF ⇒ market_rank1 fallback ⇒ must still emit the fire."""
    for city_key in ("paris", "tokyo"):
        cap, res = _run_breach_cycle("notaf", city_key)
        assert res["raised"] is None, (
            f"[{city_key}] run_cycle raised {type(res['raised']).__name__}: {res['raised']!r}")
        hf = cap.get("hedge_fire")
        assert hf is not None, f"[{city_key}] hedge_fire was never built"
        assert cap["calls"] == 1, f"[{city_key}] _paper_fire called {cap['calls']}x"
        assert hf["ref_source"] == "metar_breach_hedge"
        # no TAF ⇒ ref_extreme falls back to the METAR running extreme
        assert hf["ref_extreme"] == 32.5, hf
        assert hf["jump"] == 1 and hf["fire_no"] == 2
        assert [l["leg"] for l in hf["legs"]] == ["buy_no_broken", "buy_yes_new"]
        # local_fire_time must be present, parseable, and carry the real offset
        lft = hf["local_fire_time"]
        assert lft, hf
        parsed = datetime.fromisoformat(lft)
        assert parsed.strftime("%z") == CITIES[city_key]["offset"], (lft, parsed.strftime("%z"))
        assert parsed == datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc), parsed
        # the fire must be audited
        assert any(r.get("type") == "fire_attempt" for r in res["rows"]), res["rows"]
    print("PASS: test_breach_hedge_no_taf_does_not_raise_and_fires "
          "(no-TAF path emits hedge fire; local_fire_time iso-parseable +02:00/+09:00)")


def test_breach_hedge_taf_path_matches_baseline_golden():
    """With TAF the produced hedge_fire must be byte-for-byte the ccbd0c8 golden."""
    cap, res = _run_breach_cycle("taf", "paris")
    assert res["raised"] is None, res["raised"]
    hf = cap.get("hedge_fire")
    assert hf is not None
    assert hf == TAF_GOLDEN, json.dumps(hf, indent=1, sort_keys=True)
    assert json.dumps(hf, sort_keys=True) == json.dumps(TAF_GOLDEN, sort_keys=True)
    print("PASS: test_breach_hedge_taf_path_matches_baseline_golden "
          "(TAF hedge_fire byte-identical to ccbd0c8 golden)")


def test_breach_hedge_paths_differ_only_in_ref_extreme():
    """The added binding must not perturb any field other than the TAF reference."""
    cap_n, res_n = _run_breach_cycle("notaf", "paris")
    cap_t, res_t = _run_breach_cycle("taf", "paris")
    assert res_n["raised"] is None and res_t["raised"] is None
    hn, ht = cap_n["hedge_fire"], cap_t["hedge_fire"]
    assert ht["ref_extreme"] == 33.0 and hn["ref_extreme"] == 32.5
    dn = {k: v for k, v in hn.items() if k != "ref_extreme"}
    dt = {k: v for k, v in ht.items() if k != "ref_extreme"}
    assert dn == dt, json.dumps({"no_taf": dn, "taf": dt}, indent=1, sort_keys=True)
    print("PASS: test_breach_hedge_paths_differ_only_in_ref_extreme "
          "(33.0 TAF vs 32.5 METAR; every other field identical)")


def _run_entry_cycle(mode: str, city_key: str):
    """Run one deterministic **entry** cycle (session has no open position) end to end.

    Everything external is stubbed; ``_paper_fire`` is the **real** for the paper port and the real
    ``_record_fire_event`` writes the ``fire`` row.  The consensus tracker is seeded so the target
    bucket is rank-1 (the entry lane's gate 6).  Returns ``(captured, result)`` where ``captured``
    holds the emitted entry fire (captured by wrapping the real paper fire).
    """
    from zoneinfo import ZoneInfo
    from consensus_tracker import ConsensusTracker

    city = CITIES[city_key]
    icao = city["icao"]
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)      # Paris 14:00 本地（窗口内）
    local_date = now.astimezone(ZoneInfo(city["timezone"])).date().isoformat()
    rule_key = f"{city['city_id']}|{local_date}|high"

    buckets = [
        {"bucket_id": "b30", "lo": 30.0, "hi": 31.0, "yes_token_id": "Y30", "no_token_id": "N30"},
        {"bucket_id": "b31", "lo": 31.0, "hi": 32.0, "yes_token_id": "Y31", "no_token_id": "N31"},
        {"bucket_id": "b32", "lo": 32.0, "hi": 33.0, "yes_token_id": "Y32", "no_token_id": "N32"},
    ]
    rule = {"city_id": city["city_id"], "market_local_date": local_date,
            "direction": "high", "buckets": buckets, "enabled": True}

    cache = book_cache()
    cache.clear()
    # target bucket ask 0.60 ∈ (0.45, 0.75]; next bucket cheap (twap/instant price sub-gates pass)
    for tok, bid, ask in (("Y30", "0.02", "0.03"), ("N30", "0.90", "0.95"),
                          ("Y31", "0.58", "0.60"), ("N31", "0.30", "0.35"),
                          ("Y32", "0.10", "0.12"), ("N32", "0.75", "0.80")):
        cache[tok] = {"best_bid": bid, "best_ask": ask, "tick_size": "0.01",
                      "bids": [{"price": bid, "size": "500"}], "asks": [{"price": ask, "size": "500"}]}

    metar = {icao: {"temp_c": 31.2, "obs_time": now - timedelta(seconds=60),
                    "source": "test", "raw": "TEST 111200Z 32005KT 31/20"}}
    # TAF TX must land inside the b31 bucket (31–32 °C) and on the market's local date
    taf = {icao: {"tx_c": 31.5, "tx_valid_utc": "2026-09-11T14:00:00Z",
                  "tn_c": 20.0, "tn_valid_utc": "2026-09-11T02:00:00Z"}} if mode == "taf" else {}

    state = {
        "paper_initial_capital_usdc": 700.0,
        "positions": {},
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
    }

    log_path = os.path.join(tempfile.mkdtemp(prefix="audit-entry-"), "events.jsonl")
    cfg = {
        "mode": "paper", "strategy_mode": "consensus_lock", "fire_budget_usdc": 10.0,
        "log_path": log_path, "market_ws_enabled": False, "ws_triggered_metar_enabled": False,
        "rules_refresh_interval_seconds": 10 ** 9, "idle_book_interval_seconds": 10 ** 9,
        "idle_metar_interval_seconds": 10 ** 9, "arm_metar_interval_seconds": 10 ** 9,
        "taf_refresh_interval_seconds": 10 ** 9, "settle_poll_seconds": 10 ** 9,
        "consensus_lock": {
            "filter_fast_stations_only": False, "early_stop_enabled": True,
            "early_stop_bid_floor": "0.45", "early_stop_next_bucket_surge": "0.35",
            "max_fires_per_session": 2, "risk_control_no_cap": "0.85",
            "risk_control_yes_cap": "0.75", "yes_min_ask": "0.45", "yes_max_ask": "0.75",
        },
        "strategy": {"yes_min_ask": "0.45", "yes_max_ask": "0.75"},
    }

    # seed the tracker: b31 is the rank-1 bucket (10 samples inside the 1h TWAP window)
    seeded = ConsensusTracker()
    for step in range(10):
        ts = datetime(2026, 9, 11, 11, 10 + step, 0, tzinfo=timezone.utc)
        seeded.record_books(city["city_id"], local_date, "high", buckets, {
            "Y31": {"best_bid": "0.65", "best_ask": "0.68"},
            "Y32": {"best_bid": "0.10", "best_ask": "0.12"},
        }, ts)

    _r_cycle._CONSENSUS_LOCK_STRAT = None
    _r_cycle.tracker = lambda: seeded
    _r_cycle.load_active_cities = lambda _cfg: [dict(city)]
    _r_cycle.target_dates_by_icao = lambda *a, **k: {}
    _r_cycle.refresh_rules = lambda *a, **k: None
    _r_cycle._load_rule_cache = lambda: ({rule_key: dict(rule)}, {})
    _r_cycle.prune_stale_sessions = lambda *a, **k: 0
    _r_cycle.refresh_books = lambda *a, **k: {}
    _r_cycle._fetch_metar = lambda *a, **k: dict(metar)
    _r_cycle._fetch_taf = lambda *a, **k: dict(taf)
    _r_cycle._LAST_GOOD_METAR.clear()
    _r_cycle._LAST_GOOD_TAF.clear()

    class _WS:
        running = False

        def start(self, *a, **k):
            pass

        def ensure_tokens(self, *a, **k):
            pass

        def telemetry(self):
            return {"running": False}

    _r_cycle.ws_bridge = lambda: _WS()

    captured: dict = {}

    def _capturing_fire(cfg_, state_, fire_, now_):
        captured["entry_fire"] = json.loads(json.dumps(fire_, default=str))
        return _REAL_PAPER_FIRE(cfg_, state_, fire_, now_)

    _r_cycle._paper_fire = _capturing_fire

    raised = None
    try:
        _r_cycle.run_cycle(cfg, state, now, force_metar=True, force_books=True, force_rules=True)
    except BaseException as exc:  # noqa: BLE001 — a raise is itself a failure signal
        raised = exc

    rows = []
    if os.path.exists(log_path):
        rows = [json.loads(l) for l in open(log_path, encoding="utf-8").read().splitlines() if l.strip()]
    _r_cycle._paper_fire = _REAL_PAPER_FIRE
    _r_cycle._CONSENSUS_LOCK_STRAT = None
    return captured, {"raised": raised, "rows": rows, "log_path": log_path, "rule_key": rule_key}


AUDIT_FIELDS = ("taf_present", "taf_source", "fire_path", "fire_key")


def _assert_audit_fields(row: dict, *, want_taf: bool, want_path: str, want_key: str, label: str):
    """Every fire-path row must carry the 4 additive fields with consistent values."""
    for field in AUDIT_FIELDS:
        assert field in row, (label, field, row)
    assert row["taf_present"] is want_taf, (label, row)
    assert row["taf_source"] == ("taf" if want_taf else "market_rank1"), (label, row)
    assert row["fire_path"] == want_path, (label, row)
    assert row["fire_key"] == want_key, (label, row)


def test_audit_fields_hedge_path_x_taf_and_no_taf():
    """§2（对冲/追火 × 有 TAF / 无 TAF）：breach_risk_control / fire_attempt / fire 三行都带 4 字段。"""
    for mode in ("taf", "notaf"):
        cap, res = _run_breach_cycle(mode, "paris", real_fire=True)
        assert res["raised"] is None, (mode, res["raised"])
        want_taf = mode == "taf"
        rows = res["rows"]
        by_type = {}
        for row in rows:
            by_type.setdefault(row.get("type"), []).append(row)
        for kind in ("breach_risk_control", "fire_attempt", "fire"):
            assert by_type.get(kind), (mode, kind, [r.get("type") for r in rows])
            for row in by_type[kind]:
                _assert_audit_fields(row, want_taf=want_taf, want_path="hedge",
                                     want_key=res["rule_key"], label=f"{mode}/{kind}")
        # 与既有字段一致：无 TAF ⇒ ref_source 不是 "taf"，有 TAF ⇒ 是 "taf"（入口车道）或 hedge 标记
        hedge = by_type["fire_attempt"][-1]
        assert hedge["ref_source"] == "metar_breach_hedge", hedge
        hf = cap["hedge_fire"]
        assert (hf["ref_extreme"] == 33.0) is want_taf, hf       # TAF TX 33.0 只在有 TAF 时可用
        print(f"  [audit] hedge/{mode:5} rows={len(rows)} taf_present={want_taf} "
              f"taf_source={'taf' if want_taf else 'market_rank1'} fire_path=hedge")
    print("PASS: test_audit_fields_hedge_path_x_taf_and_no_taf "
          "(对冲路径 4 组合×3 类事件全部带 taf_present/taf_source/fire_path/fire_key)")


def test_audit_fields_entry_path_x_taf_and_no_taf():
    """§2（入口 fire × 有 TAF / 无 TAF）：fire_attempt + fire 两行都带 4 字段且与 ref_source 同源。"""
    for mode in ("taf", "notaf"):
        cap, res = _run_entry_cycle(mode, "paris")
        assert res["raised"] is None, (mode, res["raised"])
        want_taf = mode == "taf"
        rows = res["rows"]
        fires = [r for r in rows if r.get("type") == "fire_attempt"]
        written = [r for r in rows if r.get("type") == "fire"]
        assert fires, (mode, [r.get("type") for r in rows])
        assert written, (mode, "the entry fire must reach the ledger row")
        assert cap.get("entry_fire"), (mode, cap)
        for row in fires + written:
            _assert_audit_fields(row, want_taf=want_taf, want_path="entry",
                                 want_key=res["rule_key"], label=f"entry/{mode}")
        # taf_source 与既有 ref_source 同源（有 TAF ⇒ "taf"；无 TAF ⇒ "market_rank1"）
        want_src = "taf" if want_taf else "market_rank1"
        assert cap["entry_fire"]["ref_source"] == want_src, (mode, cap["entry_fire"]["ref_source"])
        assert fires[-1]["ref_source"] == want_src, (mode, fires[-1])
        assert written[-1]["ref_source"] == want_src, (mode, written[-1])
        print(f"  [audit] entry/{mode:5} rows={len(rows)} ref_source={want_src} "
              f"taf_present={want_taf} fire_path=entry")
    print("PASS: test_audit_fields_entry_path_x_taf_and_no_taf "
          "(入口 fire 4 组合：taf_present/taf_source 与 ref_source 逐字一致)")


if __name__ == "__main__":
    test_breach_hedge_no_taf_does_not_raise_and_fires()
    test_breach_hedge_taf_path_matches_baseline_golden()
    test_breach_hedge_paths_differ_only_in_ref_extreme()
    test_audit_fields_hedge_path_x_taf_and_no_taf()
    test_audit_fields_entry_path_x_taf_and_no_taf()
    print("ALL BREACH-HEDGE TESTS PASSED!")
