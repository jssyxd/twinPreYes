#!/usr/bin/env python3
"""Execution **port** — one strategy, two fill channels.

Why a port and not a second bot: the operator's rule is that live and paper must share the
strategy, the logic and the infrastructure, differing **only** in how a fill happens. So the
engine keeps doing exactly what it does today (data pull, arm/fire decision, windows, consensus
filter, sleeve, leg sizing, state schema, events, health, settlement) and calls this port for
the two things that are genuinely channel-specific:

``preflight(fire, cfg)``
    may this fire proceed at all? (paper: always yes; live: real balance/positions + the
    ``LIVE_*`` hard caps through ``risk_gate`` / ``check_limits``)
``match(leg, book, limit, shares)``
    turn one ladder intent into a fill: ``paper`` → the in-memory FAK matcher
    (``re_execution.paper_match_fak``), ``live`` → a real CLOB v2 order reconciled
    to its real fill.
``fund(state, cfg, fire, total_cost)``
    book the cost in the one ledger the engine already owns (``paper_capital.reserve``) —
    shared by both modes so accounting stays identical.

No strategy branch lives here: the port never decides *whether*, *which leg* or *how much*.
``get_port`` refuses (machine-readable reason, never a silent downgrade to paper) when live is
requested without its gates.

LIVE take rule (operator instruction, 2026-09-11; corrected 2026-09-12)
---------------------------------------------------------------------
Live used to be passive-only: every ``send_fak`` ladder intent became a ``post_only`` order that
by construction could never fill, while paper filled it by walking the asks.  The operator's rule
is now **leg-level independent permission, and never a passive fallback** — the second half is the
critical safety semantics: a passive order on a fake breakout fills against the bid and buys a
bucket that is going to zero (observed on 2026-09-11: Toronto YES → 0.001, Warsaw −69%).

* **YES leg** (``buy_yes_new`` / ``buy_yes_sleeve`` / ``outcome == "YES"``): takes with a **FAK**
  order **only while its own price sits inside ``(yes_min_ask, yes_max_ask]``** (cfg-driven,
  defaults 0.48 / 0.90, half-open: 0.48 excluded, 0.90 included).  The band is read from the
  *strategy's existing* ``yes_min_ask`` / ``yes_max_ask`` (flat ``cfg`` key or ``cfg["strategy"]``).
  **Outside the band — or with no usable price, or with an illegal band value — the leg is
  dropped: no order of any kind is sent.**  (A YES ask of 0.40 is a fake breakout below the
  floor; resting there would fill at the bid and buy a bucket that is dying.)
* **NO leg** (any non-YES leg): bounded by **its own cap** (the strategy's ``no_max_ask``, currently
  1.0 — see the note below) and by **book depth**.  It takes with **FAK** while the leg's book
  shows a resting ask; with **no ask** it is skipped as ``no_book``, and with a quote that is
  present but unusable (non-numeric, ``<= 0`` or ``> 1``) it is refused as ``ask_out_of_range``
  rather than mislabelled as an empty book.  It is **never** sent as a passive order either.
* **YES leg of the parallel next-bucket channel** (``entry_channel == "next_bucket"``, added
  2026-09-12): judged by the leg's **own** window ``(floor, cap]`` carried on the ladder intent —
  cfg's ``yes_min_ask``/``yes_max_ask`` cannot move it, and an unusable leg window fails closed
  (``leg_window_unusable``) instead of being replaced by a band nobody asked for.  Only a leg with
  **no window at all** falls back to the cfg band.  The decision travels on the audit rows as
  ``entry_channel`` / ``leg_window`` / ``window_source`` / ``next_entry_window`` (stamped from the
  port's own leg-derived values, never from a caller-supplied ``audit_extra``).
* **no passive fallback anywhere on the live path**: whenever a window / cap / book / price
  condition is not met the leg is refused or skipped — a resting ``post_only`` order is never
  produced by ``LivePort.match``.  (``live/v2_transport.execute_leg`` keeps its historical maker
  branch for the explicit diagnostics in ``live/smoke.py``; the trading path never uses it.)
* fail-closed, all of which mean **refuse, never downgrade**: a cap that is missing, unparseable,
  non-positive or ``> 1``; a YES band that is present but illegal; no YES price evidence at all; a
  YES price outside the band; a limit that is ``<= 0``, ``> cap`` or ``> 1``.  A cap of exactly
  ``1.0`` is accepted (it is the shipped ``no_max_ask``) but the absolute ``<= 1`` limit still
  binds, so a taker can never exceed the venue's maximum price.
* **F-D (2026-09-12): the venue's own minimum order size is enforced on this path.**  Until now the
  engine fire path never consulted ``min_order_size`` (the repo's only enforcement point was
  ``live/order_plan.py``, reachable only from ``live/smoke.py`` / ``sign_dryrun``), so a leg whose
  planned share count is below the venue minimum was sent and necessarily rejected.  ``match`` now
  refuses such a leg locally (``order_mode=skip``, ``status=below_min_order_size``, nothing sent)
  whenever the book carries a usable minimum; a book that does **not** carry the field keeps its
  exact previous behaviour (no new refusal, since the venue told us nothing).
* the YES price evidence is resolved, in order, from **this leg's own context**: the YES-leg ladder
  intent handed to ``match`` (``leg["best_ask"]``) → ``fire["ladder"]``'s YES row →
  ``fire["yes_ask"] / fire["yes_price"] / fire["yes_best_ask"]`` → one read-only re-quote of the
  fire's YES token through ``transport.refetch_book`` (cached for the lifetime of that exact fire
  dict, so later rungs of the same fire reuse it).  Remaining failure modes are ``yes_price_unknown``
  and ``fire_yes_price_read_failed``.
* the decision travels on the **existing** audit records: ``order_mode`` / ``taker_gate`` /
  ``yes_price`` / ``yes_price_source`` are merged into the mandatory ``intent``/``submit`` rows,
  so a pure decision (a ``match`` that sends nothing) adds **no** log line and the
  ``submit.py --summary`` action counts are not polluted.  ``match`` returns ``order_mode``
  (``taker``/``skip``) plus ``taker_gate``/``yes_price`` so the engine's ladder log shows why a
  leg did or did not go out.  Every other red line — triple gate, ``risk_gate``, ``check_limits``,
  tick alignment, least-privilege sentinels, the append-only audit — is untouched.  (The venue's
  ``min_order_size`` used to be *outside* this path; F-D added the guard, see the bullet above.)
  ``describe()`` advertises the channel/leg-window rule and the new local refusal.

.. note:: ``config/yes2re_reversal.json`` ships ``no_max_ask = "1.0"`` while ``AGENTS.md``
   documents the NO cap as ``0.65``.  The operator's instruction is "everything else unchanged",
   so 1.0 stays; the discrepancy is reported rather than silently "fixed".  With ``cap = 1.0`` the
   only ceiling on a NO take is the absolute ``<= 1`` bound.
"""
from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # `python3.13 live/...py` — make relative imports work
    __package__ = "live"  # `python3.13 live/port.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import re_execution
    from paper_capital import reserve

    from live import creds as creds_mod, reconcile, risk_gate, submit
else:  # `python3.13 tests_port.py` / `import live.port`
    import re_execution
    from paper_capital import reserve

    from . import creds as creds_mod, reconcile, risk_gate, submit

ZERO = Decimal("0")
PAPER = "paper"
LIVE = "live"

#: live gates, service-side analogues of the CLI flags (all deliberately absent from .env)
ENV_ENABLE_SUBMIT = "YES2RE_LIVE_ENABLE_SUBMIT"
ENV_CONFIRM = "YES2RE_LIVE_CONFIRM"

REASON_OK = "ok"
REASON_UNKNOWN_MODE = "unknown_mode"
REASON_LIVE_DEPS = "live_deps_missing"
REASON_LIVE_DISABLED = "live_port_disabled"

# --------------------------------------------------------------------- LIVE taker rule
#: how a leg meets the book (recorded as ``order_mode`` in the audit log / ladder log)
MAKER = "maker"                 # kept for the diagnostics path (live/smoke.py) — never the trading path
TAKER = "taker"
SKIP = "skip"                   # this leg produced no order at all (refused / no book)
#: audit labels of the two independent leg gates
GATE_YES_BAND = "yes_band"      # YES leg: its own price inside (yes_min_ask, yes_max_ask]
GATE_NO_LEG_ASK = "no_leg_ask"  # non-YES leg: a resting ask exists in this leg's own book
#: YES-leg names in a fire spec — pure data mirror of the strategy's leg names
YES_LEG_NAMES = ("buy_yes_new", "buy_yes_sleeve", "buy_yes_lock", "buy_yes_next")
#: band defaults when cfg carries no yes_min_ask / yes_max_ask
DEFAULT_YES_MIN_ASK = Decimal("0.48")
DEFAULT_YES_MAX_ASK = Decimal("0.90")

#: entry channels (audit): the existing target-bucket channel vs the parallel next-bucket one
CHANNEL_TARGET = "target_bucket"      # 既有目标桶通道（窗口来自 cfg 的 yes_min_ask/yes_max_ask）
CHANNEL_NEXT = "next_bucket"          # 下一档桶廉价入场通道（窗口来自腿自带的 floor/cap）
ENTRY_CHANNELS = (CHANNEL_TARGET, CHANNEL_NEXT)

#: where this leg's take window came from (audit) — leg window first, cfg band as the fallback
WINDOW_SOURCE_LEG = "leg_window"                 # 腿自带窗口（通道自有、独立）
WINDOW_SOURCE_CFG = "cfg_yes_band"               # 无腿窗口 ⇒ 回退 cfg 的 strategy.yes_min_ask/yes_max_ask
WINDOW_SOURCE_CFG_FALLBACK = "cfg_yes_band_fallback"

#: machine-readable reasons behind one leg's take/skip decision
FILL_TAKER = "yes_band"                    # YES leg inside (lo, hi] ⇒ FAK
FILL_TAKER_NO = "no_leg_ask"               # non-YES leg with a resting ask ⇒ FAK
FILL_SKIP_NO_BOOK = "no_book"              # this leg's book has no resting ask ⇒ skip
FILL_REFUSE_ASK_BAD = "ask_out_of_range"   # a quote IS there but unusable (<= 0 / > 1 / NaN) ⇒ refuse
FILL_REFUSE_BELOW = "yes_price_below_band"
FILL_REFUSE_ABOVE = "yes_price_above_band"
FILL_REFUSE_UNKNOWN = "yes_price_unknown"
FILL_REFUSE_BAND_BAD = "yes_band_unparsed"
FILL_REFUSE_LEG_WINDOW = "leg_window_unusable"   # 腿自带窗口缺失/非法 ⇒ 弃单（绝不回退、绝不降级）
FILL_REFUSE_NO_CAP = "taker_cap_missing"   # no explicit, usable cap ⇒ refuse (L-2)
#: C（2026-09-13）**显式 Floor Guard**：该腿有效 best ask < 该腿**有效地板** ⇒ 拒绝下单并落审计。
#: 有效地板：next_bucket 通道 = 腿自带 ``floor``（改动后 0.27，闭区间下界）；目标桶通道 =
#: cfg ``yes_min_ask``（0.45 —— 与既有 band 下界同值，因此**既有 reason 码与行为一字不改**）。
#: fail-closed：不发单、不回退 cfg 带、不降级为挂单。仅 next_bucket 通道会产出该新码（见行内注释）。
FILL_REFUSE_ASK_BELOW_FLOOR = "ask_below_floor"
#: F-D（2026-09-12）：planned shares below the venue's own minimum order size ⇒ local refuse.
#: The engine fire path never consulted ``min_order_size`` (the repo's only enforcement point was
#: ``live/order_plan.py``, reachable only from smoke/sign_dryrun) ⇒ such a leg was sent and
#: necessarily rejected by the venue.  Same reason string ``order_plan.BELOW_MIN_ORDER_SIZE``.
FILL_REFUSE_BELOW_MIN = "below_min_order_size"

ONE = Decimal("1")


def _book_min_order_size(book) -> Decimal | None:
    """Venue minimum order size in **shares** from a book dict; ``None`` when unknown.

    Read-only and non-throwing.  ``None`` (field absent / ``None`` / malformed / ``<= 0``) means
    the venue did not tell us — the caller then does **not** add a refusal, so a book shape that
    omits the field keeps its exact previous behaviour.
    """
    if not isinstance(book, dict):
        return None
    raw = book.get("min_order_size")
    if raw is None:
        raw = book.get("minOrderSize")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value.is_finite() and value > ZERO else None


def _band_number(value) -> Decimal | None:
    """Parse one band bound; ``None`` when it is absent or unusable (caller fails closed)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    if not out.is_finite() or out <= ZERO or out > 1:
        return None
    return out


def _band_raw(cfg: dict | None, name: str):
    """Read a strategy parameter from ``cfg`` — flat first, then ``cfg["strategy"]``."""
    if not isinstance(cfg, dict):
        return None
    value = cfg.get(name)
    if value is not None:
        return value
    strategy = cfg.get("strategy")
    if isinstance(strategy, dict):
        return strategy.get(name)
    return None


def _cap_number(value) -> Decimal | None:
    """One leg's ask cap (positive and finite); ``None`` when absent/unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    if not out.is_finite() or out <= ZERO:
        return None
    return out


def taker_cap_number(value) -> Decimal | None:
    """The cap a take is allowed to lean on: an explicit ``0 < cap <= 1``.

    ``None`` when the leg carries no cap, an unparseable one, a non-positive one, or one above the
    venue maximum (``> 1``).  A missing/invalid cap must never authorise an aggressive order, and
    the transport refuses ``taker=True`` without a usable one (L-2).  ``cap == 1.0`` is accepted
    because that is the shipped ``no_max_ask``; with it, the absolute ``<= 1`` limit is what binds.
    """
    out = _cap_number(value)
    if out is None or out > ONE:
        return None
    return out


def is_yes_leg(leg: dict | None) -> bool:
    """Is this the fire's YES leg?  Pure data mirror of the strategy's leg names/outcomes.

    The two legs are judged **independently** (operator correction, 2026-09-12): only the YES leg
    is bound by the ``(yes_min_ask, yes_max_ask]`` band, while a non-YES leg is bound by its own
    cap and its own book depth.
    """
    if not isinstance(leg, dict):
        return False
    return (str(leg.get("leg")) in YES_LEG_NAMES
            or str(leg.get("outcome") or "").upper() == "YES")


def ask_state_of(book) -> dict:
    """Classify this leg's own quote: ``{"present": bool, "ask": Decimal | None, "raw": Any}``.

    ``present=False`` means the leg's book genuinely carries **no** resting ask (``best_ask``
    missing, ``None`` or blank) ⇒ the caller skips the leg as ``no_book``.

    ``present=True`` with ``ask is None`` means a quote *is* there but is not a usable price
    (non-numeric, ``<= 0`` or ``> 1``) ⇒ the caller **refuses** it as ``ask_out_of_range``.
    Reporting a corrupt/out-of-range quote as ``no_book`` would send the operator hunting for a
    missing feed instead of a bad one (audit LOW, 2026-09-12).  Pure: reads nothing but ``book``.
    """
    if not isinstance(book, dict):
        return {"present": False, "ask": None, "raw": None}
    raw = book.get("best_ask")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"present": False, "ask": None, "raw": raw}
    return {"present": True, "ask": _band_number(raw), "raw": raw}


def best_ask_of(book) -> Decimal | None:
    """This leg's own best ask from a normalized book dict (``None`` when there is no *usable* ask).

    Thin wrapper over :func:`ask_state_of` (same contract as before it grew a reason), kept because
    callers and tests use it directly.  Use ``ask_state_of`` when the *reason* matters.
    """
    return ask_state_of(book)["ask"]


def yes_price_band(cfg: dict | None) -> dict:
    """The YES band that permits taking: ``(yes_min_ask, yes_max_ask]``.

    Absent ⇒ the defaults (0.48 / 0.90).  Present but unparseable/out of range ⇒ ``ok=False``
    and the caller **must not** take (fail closed).  Pure: reads nothing but ``cfg``.
    """
    lo_raw = _band_raw(cfg, "yes_min_ask")
    hi_raw = _band_raw(cfg, "yes_max_ask")
    lo = DEFAULT_YES_MIN_ASK if lo_raw is None else _band_number(lo_raw)
    hi = DEFAULT_YES_MAX_ASK if hi_raw is None else _band_number(hi_raw)
    if lo is None or hi is None or lo >= hi:
        return {"ok": False, "lo": None, "hi": None,
                "detail": (f"yes band unusable (yes_min_ask={lo_raw!r}, yes_max_ask={hi_raw!r}) "
                           f"— failing closed to passive")}
    return {"ok": True, "lo": lo, "hi": hi,
            "detail": f"({lo}, {hi}]"}


def _consensus_lock_band_raw(cfg: dict | None, name: str):
    """目标桶通道的**权威**取值：与策略同源（M1 修复，2026-09-13）。

    策略侧的生效配置是 ``{**cfg["strategy"], **cfg["consensus_lock"]}``（见
    ``_r_cycle._get_consensus_lock_strat``），因此 ``yes_max_ask`` 由 ``consensus_lock`` 段覆盖。
    执行层原先只读 ``strategy`` 段 ⇒ ``ask ∈ (0.75, 0.81]`` 会出现「策略 fire 成功、执行层必然
    拒单」的**零成交 fire**，并白占 ``max_fires_per_session``。operator 决策（方案 2）：让执行层
    **同读**该键。

    ``consensus_lock`` 缺该键 ⇒ 回退原有的 flat → ``strategy`` 解析，行为与改动前一致。
    """
    if isinstance(cfg, dict):
        block = cfg.get("consensus_lock")
        if isinstance(block, dict) and block.get(name) is not None:
            return block.get(name)
    return _band_raw(cfg, name)


def target_yes_band(cfg: dict | None) -> dict:
    """目标桶通道的 YES 带 ``(lo, hi]`` —— 与 :func:`yes_price_band` 同构，但**与策略同源**。

    仅用于 ``entry_channel == "target_bucket"`` 的腿；反手/legacy/袖套腿（``channel is None``）
    继续走 :func:`yes_price_band`（``strategy`` 段），**逐字不变**。缺失取默认 (0.48, 0.90]；
    存在但不可解析 / 越界 / ``lo >= hi`` ⇒ ``ok=False``，调用方**必须拒绝下单**（fail closed）。
    """
    lo_raw = _consensus_lock_band_raw(cfg, "yes_min_ask")
    hi_raw = _consensus_lock_band_raw(cfg, "yes_max_ask")
    lo = DEFAULT_YES_MIN_ASK if lo_raw is None else _band_number(lo_raw)
    hi = DEFAULT_YES_MAX_ASK if hi_raw is None else _band_number(hi_raw)
    if lo is None or hi is None or lo >= hi:
        return {"ok": False, "lo": None, "hi": None,
                "detail": (f"yes band unusable (yes_min_ask={lo_raw!r}, yes_max_ask={hi_raw!r}) "
                           f"— failing closed to passive")}
    return {"ok": True, "lo": lo, "hi": hi,
            "detail": f"({lo}, {hi}]"}


def in_yes_band(price, lo, hi) -> bool:
    """Half-open band test: ``lo < price <= hi`` (0.48 excluded, 0.90 included)."""
    px = _band_number(price)
    if px is None:
        return False
    try:
        low = Decimal(str(lo))
        high = Decimal(str(hi))
    except (InvalidOperation, AttributeError, ValueError):
        return False
    if not (low.is_finite() and high.is_finite()) or low >= high:
        return False
    return bool(low < px <= high)


def _yes_leg_of(fire: dict) -> dict:
    """The fire's YES leg spec (``buy_yes_new`` / ``buy_yes_sleeve``), if it has one."""
    for leg in (fire or {}).get("legs") or []:
        if is_yes_leg(leg):
            return leg
    return {}


def leg_entry_channel(leg: dict | None, fire: dict | None = None) -> str | None:
    """This leg's entry channel, from the leg itself first, then its own fire row (pure).

    ``entry_channel`` is written by the strategy on the *fire* and its legs and travels
    unchanged onto the ladder intent.  A leg that declares nothing (every legacy fire) is
    simply "no channel": the caller then keeps the historical cfg-band behaviour.  Only the
    two known channels are ever reported, so a corrupt value can never masquerade as one.
    """
    for value in ((leg or {}).get("entry_channel"), (fire or {}).get("entry_channel")):
        if value and str(value) in ENTRY_CHANNELS:
            return str(value)
    name = str((leg or {}).get("leg") or "")
    for row in (fire or {}).get("legs") or []:
        if not isinstance(row, dict) or str(row.get("leg")) != name:
            continue
        if row.get("entry_channel") and str(row["entry_channel"]) in ENTRY_CHANNELS:
            return str(row["entry_channel"])
    return None


def _leg_window_bounds(leg: dict | None, fire: dict | None) -> tuple[Any, Any]:
    """The raw ``(floor, cap)`` a YES leg carries — the leg (ladder intent) first, else its fire row.

    A fire carries no window of its own: the legs are judged one by one.  The fire's own leg spec
    is only consulted for a field the intent did not carry, and always matched **by leg name**.
    """
    leg = leg if isinstance(leg, dict) else {}
    lo_raw = leg.get("floor")
    hi_raw = leg.get("cap")
    if lo_raw is not None and hi_raw is not None:
        return lo_raw, hi_raw
    name = str(leg.get("leg") or "")
    for row in (fire or {}).get("legs") or []:
        if not isinstance(row, dict) or str(row.get("leg")) != name:
            continue
        return (lo_raw if lo_raw is not None else row.get("floor"),
                hi_raw if hi_raw is not None else row.get("cap"))
    return lo_raw, hi_raw


def leg_window_number(value) -> tuple[Decimal | None, str]:
    """Parse one leg-window bound ⇒ ``(value, state)`` with ``state in {"ok","absent","bad"}``.

    An empty/``None``/zero floor means "no lower bound" (``ok`` with ``None``) — the leg simply
    does not clamp from below.  Anything unparseable, non-finite, negative or ``> 1`` is ``bad``
    and the caller **fails closed**: a corrupt leg window must never be silently replaced by the
    cfg band (that would be a downgrade to a window nobody asked for).
    """
    if value is None or isinstance(value, bool):
        return None, "absent"
    text = str(value).strip()
    if text in ("", "None"):
        return None, "absent"
    try:
        out = Decimal(text)
    except (InvalidOperation, AttributeError, ValueError):
        return None, "bad"
    if not out.is_finite() or out < ZERO or out > ONE:
        return None, "bad"
    return (out if out > ZERO else None), "ok"


def leg_take_window(leg: dict | None, fire: dict | None, cfg: dict | None) -> dict:
    """The window this leg's take is judged against: ``(lo, hi]``.

    Leg-level rule (operator instruction 2026-09-12, extended 2026-09-12 to the parallel
    next-bucket channel): a YES leg belonging to the **next-bucket** channel carries its **own**
    window in ``floor``/``cap`` and is judged by *that* window only — cfg's
    ``yes_min_ask``/``yes_max_ask`` cannot move it, and an unusable leg window fails closed
    (``leg_window_unusable``) instead of falling back to a band nobody asked for.  A leg with
    **no window at all** falls back to the cfg band (the documented fallback).

    Every other leg (the existing target-bucket channel, sleeves, refires, the reversal
    strategy, and the whole historical test surface) keeps the cfg band **exactly** as before:
    ``yes_price_band(cfg)`` — the same call, the same reason code, the same source label.

    Returns ``{ok, lo, hi, closed_lo, label, detail, source, channel, reason}`` where ``reason`` is
    the machine-readable refusal when ``ok`` is ``False``.  ``closed_lo`` marks the **closed lower
    bound** (B 项，2026-09-13): ``True`` only for the next-bucket channel, whose own window is
    ``[floor, cap]`` (0.27 and 0.32 **both** inclusive); every other channel keeps the historical
    half-open ``(lo, hi]``.  Pure: reads nothing but its arguments.
    """
    channel = leg_entry_channel(leg, fire)
    if channel != CHANNEL_NEXT:
        # M1（2026-09-13，operator 方案 2）：**目标桶通道**与策略同源，读 consensus_lock 段；
        # 反手 / legacy / 袖套腿（channel is None）继续读 strategy 段，**逐字不变**
        # （反手 YES 腿顶价仍 0.75，不被本题改动波及）。
        band = target_yes_band(cfg) if channel == CHANNEL_TARGET else yes_price_band(cfg)
        label = band["detail"] if band["ok"] else None
        return {**band, "source": WINDOW_SOURCE_CFG, "channel": channel, "closed_lo": False,
                "label": label, "reason": FILL_REFUSE_BAND_BAD}
    lo_raw, hi_raw = _leg_window_bounds(leg, fire)
    if lo_raw is None and hi_raw is None:
        band = yes_price_band(cfg)               # 无腿窗口 ⇒ 回退 cfg（规范允许的唯一回退）
        label = band["detail"] if band["ok"] else None
        return {**band, "source": WINDOW_SOURCE_CFG_FALLBACK, "channel": channel, "closed_lo": False,
                "label": label, "reason": FILL_REFUSE_BAND_BAD}
    lo, lo_state = leg_window_number(lo_raw)
    hi, hi_state = leg_window_number(hi_raw)
    if lo_state == "bad" or hi_state == "bad" or hi is None or (lo is not None and lo >= hi):
        return {"ok": False, "lo": None, "hi": None, "channel": channel, "closed_lo": False,
                "label": None, "source": WINDOW_SOURCE_LEG, "reason": FILL_REFUSE_LEG_WINDOW,
                "detail": (f"leg window unusable (floor={lo_raw!r}, cap={hi_raw!r}; need "
                           f"0 <= floor < cap <= 1) — refuse, no order sent "
                           f"(never a fallback to the cfg band, never a passive order)")}
    lo_val = lo if lo is not None else ZERO
    return {"ok": True, "lo": lo_val, "hi": hi, "channel": channel, "closed_lo": True,
            "source": WINDOW_SOURCE_LEG,
            "detail": (f"[{lo_val}, {hi}] (closed lower bound: {lo_val} inclusive — 闭区间下界)"),
            "label": f"[{lo_val}, {hi}]", "reason": REASON_OK}


def leg_effective_floor(leg: dict | None, fire: dict | None, cfg: dict | None,
                        band: dict | None = None) -> dict:
    """C（2026-09-13）该腿的**有效地板** —— 显式 Floor Guard 的比较基准（纯函数）。

    * **next_bucket 通道**：腿自带 ``floor``（= 腿窗口下界，改动后即 0.27）。来源标 ``leg_floor``。
    * **目标桶通道 / legacy（无通道）**：cfg ``yes_min_ask``（0.45）。来源标 ``cfg_yes_min_ask``。
      该值与既有 ``(yes_min_ask, yes_max_ask]`` 的下界**同值**，因此目标桶通道的比较结果与既有
      band 判定完全一致 ⇒ 既有行为与既有 reason 码（``yes_price_below_band``）**一字不改**。

    返回 ``{floor: Decimal | None, source: str}``（``floor`` 不可解析/非法 ⇒ ``None`` ⇒ 调用方
    fail-closed，绝不回退成"无地板"）。读取顺序与 :func:`leg_take_window` 同源：腿 → fire 行。
    """
    band = band if isinstance(band, dict) else {}
    channel = band.get("channel") or leg_entry_channel(leg, fire)
    if channel == CHANNEL_NEXT:
        lo, _state = leg_window_number(band.get("lo") if band.get("ok") else None)
        if band.get("ok") and band.get("lo") is not None:
            return {"floor": Decimal(str(band["lo"])), "source": "leg_floor"}
        if lo is not None:
            return {"floor": lo, "source": "leg_floor"}
        return {"floor": None, "source": "leg_floor"}
    floor = _band_number(_consensus_lock_band_raw(cfg, "yes_min_ask"))
    if floor is None:
        floor = DEFAULT_YES_MIN_ASK if _consensus_lock_band_raw(cfg, "yes_min_ask") is None else None
    return {"floor": floor, "source": "cfg_yes_min_ask"}


def yes_leg_price(fire: dict | None, *, leg: dict | None = None) -> dict:
    """Resolve **this YES leg's own** price and where it came from (pure).

    Order of evidence, closest to the leg first: the YES-leg ladder intent handed to ``match``
    (``leg["best_ask"]``) → the fire's own ladder log YES row → an explicit
    ``yes_ask``/``yes_price``/``yes_best_ask`` field.  A ``None`` price is not an error, it is
    "no evidence ⇒ drop the leg" (the caller may still try one read-only re-quote).
    """
    fire = fire if isinstance(fire, dict) else {}
    if isinstance(leg, dict) and is_yes_leg(leg):
        px = _band_number(leg.get("best_ask"))
        if px is not None:
            return {"price": px, "source": "leg_best_ask"}
    for row in fire.get("ladder") or []:
        if not isinstance(row, dict):
            continue
        if is_yes_leg(row):
            px = _band_number(row.get("best_ask"))
            if px is not None:
                return {"price": px, "source": "fire_ladder"}
    for field in ("yes_ask", "yes_price", "yes_best_ask"):
        px = _band_number(fire.get(field))
        if px is not None:
            return {"price": px, "source": f"fire_{field}"}
    return {"price": None, "source": "no_fire_yes_price"}


class PortRefused(RuntimeError):
    """The requested port cannot be handed out — the caller must stand down (never downgrade)."""

    def __init__(self, reason: str, detail: str = "", mode: str = LIVE):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.mode = mode


class ExecutionPort:
    """The contract. Implementations differ only in how a fill is obtained."""

    mode = "abstract"

    def preflight(self, *, fire: dict, cfg: dict) -> dict:
        raise NotImplementedError

    def match(self, *, leg: dict, book: Any, limit: Decimal, shares: Decimal, fire: dict | None = None,
              cfg: dict | None = None) -> dict:
        raise NotImplementedError

    def fund(self, *, state: dict, cfg: dict, fire: dict, total_cost: Decimal) -> dict:
        """Book the filled cost in the engine ledger (shared by every mode)."""
        if total_cost <= ZERO:
            return {"ok": True, "reason": "nothing_filled", "detail": "no fill to fund"}
        booked = reserve(state, total_cost)
        if booked is None:
            return {"ok": False, "reason": "fire_insufficient_capital",
                    "detail": f"cannot reserve {total_cost} from the ledger"}
        return {"ok": True, "reason": REASON_OK, "detail": f"reserved {booked}",
                "reserved": str(booked)}

    def describe(self) -> dict:
        return {"mode": self.mode}


class PaperPort(ExecutionPort):
    """Existing behaviour, byte for byte: in-memory capped FAK against the warmed ladder."""

    mode = PAPER

    def preflight(self, *, fire: dict, cfg: dict) -> dict:
        return {"ok": True, "reason": REASON_OK, "detail": "paper: no live gates"}

    def match(self, *, leg: dict, book: Any, limit: Decimal, shares: Decimal, fire: dict | None = None,
              cfg: dict | None = None) -> dict:
        match = re_execution.paper_match_fak(book, limit, shares)
        return {"filled_shares": match["filled_shares"], "avg_price": match["avg_price"],
                "cost": match["cost"], "unfilled": match["unfilled"], "status": "paper_fak",
                "source": PAPER}

    def describe(self) -> dict:
        return {"mode": PAPER, "matcher": "re_execution.paper_match_fak",
                "ledger": "paper_capital.reserve"}


class LivePort(ExecutionPort):
    """Real fills through the CLOB v2 transport (post-only GTC, reconciled to real fills)."""

    mode = LIVE

    def __init__(self, transport, *, env: dict, gates: dict, limits: dict | None = None,
                 poll_attempts: int = 6, poll_sleep: float = 1.0, sleep=None,
                 account_reader=None, audit_path=None):
        self.transport = transport
        self.env = env or {}
        self.gates = gates
        self.limits = limits or {}
        self.poll_attempts = poll_attempts
        self.poll_sleep = poll_sleep
        self.sleep = sleep
        self.account_reader = account_reader
        self.audit_path = audit_path
        self._last_account: dict | None = None
        #: fire-level YES-price memo (see ``_fire_yes_price``) — keyed by the exact fire dict
        self._band_fire: dict | None = None
        self._band_ctx: dict | None = None
        #: token -> neg_risk 签名域缓存（盘口缺该字段时用 live 客户端重取一次）
        self._neg_risk_cache: dict[str, bool] = {}

    def resolve_neg_risk(self, client, token_id, book) -> tuple[bool | None, str]:
        """返回 ``(neg_risk, source)``。

        neg-risk 市场的订单**必须**用 neg-risk 交易所做 EIP-712 签名，否则 CLOB 以
        ``invalid POLY_PROXY signature`` 直接拒单（实盘 fire 会 100% 静默下不出去）。
        盘口自带该字段就用它；缺失时用 live 客户端重取一次盘口（CLOB ``/book`` 响应带
        ``neg_risk``，与 ``refetch_book`` 同源）并按 token 缓存；仍取不到 ⇒ ``None``，
        调用方 fail-closed（弃单，绝不按错误的签名域硬发）。
        """
        if isinstance(book, dict) and book.get("neg_risk") is not None:
            return bool(book["neg_risk"]), "book"
        tok = str(token_id or "")
        if not tok:
            return None, "no_token"
        if tok in self._neg_risk_cache:
            return self._neg_risk_cache[tok], "cache"
        refetch = getattr(self.transport, "refetch_book", None)
        if not callable(refetch):
            return None, "no_refetch_api"
        try:
            fresh = refetch(client, tok) or {}
        except Exception:  # noqa: BLE001 - lookup failure ⇒ fail closed below
            return None, "refetch_failed"
        got = fresh.get("neg_risk") if isinstance(fresh, dict) else None
        if got is None:
            return None, "unknown"
        self._neg_risk_cache[tok] = bool(got)
        return bool(got), "refetch"

    # ---------------------------------------------------------------- preflight
    def _read_account(self, client, credentials) -> dict:
        if self.account_reader is not None:
            return self.account_reader(client)
        return self.transport.read_account(client, address=credentials["funder_address"])

    def ensure_client(self) -> tuple[Any, str]:
        """Open (once) the real client for the **exit** path — *without* the entry-type caps.

        A live SELL must pass the same three gates as a live BUY, but it must **never** be blocked
        by the entry-shaped ceilings (``max_open_positions`` / ``max_capital_usdc`` / the fire
        budget / committed notional): selling consumes no quota and opens no position (operator
        rule ⑥, 2026-09-13).  So this deliberately does only "credentials + network + client" and
        skips ``risk_gate.evaluate`` / ``submit.check_limits`` — those are ``preflight``'s
        **entry** logic.  The client is cached on ``self._client`` and shared with ``preflight``
        (no second handshake).  Returns ``(client, source)``; on failure ``(None, reason)`` —
        fail closed, the caller defers the exit instead of guessing.
        """
        client = getattr(self, "_client", None)
        if client is not None:
            return client, "cached"
        try:
            credentials = creds_mod.validate_creds(self.env)
            submit.prepare_network(self.env)
            client = self.transport.build_client(credentials)
        except Exception as exc:  # noqa: BLE001 — no client ⇒ no order (fail closed)
            self._client = None
            return None, f"live_client_unavailable: {type(exc).__name__}: {exc}"
        self._client = client
        self._credentials = credentials
        return client, "opened"

    def read_account_now(self) -> dict | None:
        """Real account snapshot for the post-sell audit (rule ⑪: 卖后记真实余额/持仓).

        Best effort by contract: an unreadable account leaves the *fill* untouched (the fill was
        answered by the venue), it only means the audit carries ``account: None``.
        """
        client = getattr(self, "_client", None)
        credentials = getattr(self, "_credentials", None)
        if client is None or not isinstance(credentials, dict):
            return None
        try:
            return self._read_account(client, credentials)
        except Exception:  # noqa: BLE001 — audit-only, never blocks the exit
            return None

    def preflight(self, *, fire: dict, cfg: dict) -> dict:
        """Real balance/positions/caps must all agree before a single order is sent."""
        try:
            credentials = creds_mod.validate_creds(self.env)
            submit.prepare_network(self.env)
            client = self.transport.build_client(credentials)
            account = self._read_account(client, credentials)
        except Exception as exc:  # noqa: BLE001 - fail closed, never guess
            self._client = None
            return {"ok": False, "reason": "live_account_unreadable",
                    "detail": f"{type(exc).__name__}: {exc}", "stage": "account"}
        self._last_account = account
        self._client = client
        self._credentials = credentials
        budget = Decimal(str(fire.get("budget_usdc") or cfg.get("fire_budget_usdc") or 0))
        gate = risk_gate.evaluate(
            usdc_balance=account.get("usdc_balance"),
            open_positions=len(account.get("positions") or []),
            committed_usdc=account.get("positions_value_usdc") or 0,
            fire_budget_usdc=self.limits.get("fire_budget_usdc"),
            max_open_positions=self.limits.get("max_open_positions"),
            max_capital_usdc=self.limits.get("max_capital_usdc"),
        )
        if not gate["allow"]:
            # fail closed: a denied gate leaves no client behind, so a later ``match`` cannot
            # slip an order through a preflight that said no (matters now that orders may cross)
            self._client = None
            return {"ok": False, "reason": f"risk_gate:{gate['reason']}", "detail": gate["detail"],
                    "stage": "risk_gate", "account": _slim(account)}
        limits = submit.check_limits(
            notional_usdc=budget,
            fire_budget_usdc=self.limits.get("fire_budget_usdc"),
            committed_usdc=account.get("positions_value_usdc") or 0,
            max_capital_usdc=self.limits.get("max_capital_usdc"),
        )
        if not limits["ok"]:
            self._client = None
            return {"ok": False, "reason": f"limits:{limits['reason']}", "detail": limits["detail"],
                    "stage": "limits", "account": _slim(account)}
        return {"ok": True, "reason": REASON_OK, "detail": limits["detail"],
                "account": _slim(account), "risk_gate": gate, "limits": limits}

    # ---------------------------------------------------------------- fill mode (taker rule)
    def _refetch_yes_price(self, fire: dict, client) -> tuple[Decimal | None, str]:
        """Read-only re-quote of this fire's YES token (one call per fire; never a write)."""
        token = fire.get("new_yes_token") or _yes_leg_of(fire).get("token_id")
        fetch = getattr(self.transport, "refetch_book", None)
        if not token or not callable(fetch):
            return None, "no_fire_yes_price"
        try:
            book = fetch(client, str(token))
        except Exception:  # noqa: BLE001 - a quote failure only means "drop this leg"
            return None, "fire_yes_price_read_failed"
        if not isinstance(book, dict):
            return None, "fire_yes_price_read_failed"
        px = _band_number(book.get("best_ask"))
        return (px, "refetch_book_yes_leg") if px is not None else (None, "fire_yes_price_read_failed")

    def _fire_yes_price(self, fire: dict, leg: dict | None, client) -> dict:
        """The fire's YES price, resolved once per fire (memoised on this exact fire dict)."""
        got = yes_leg_price(fire, leg=leg)
        if got["price"] is not None:
            self._band_fire, self._band_ctx = fire, {"yes_price": got["price"], "source": got["source"]}
            return self._band_ctx
        if self._band_fire is fire and self._band_ctx is not None:
            memo = dict(self._band_ctx)
            memo["source"] = f"{memo['source']}_memo"
            return memo
        price, source = self._refetch_yes_price(fire, client)
        if price is not None:
            self._band_fire, self._band_ctx = fire, {"yes_price": price, "source": source}
            return self._band_ctx
        return {"yes_price": None, "source": source}

    def fill_mode(self, *, fire: dict | None, leg: dict | None, cfg: dict | None,
                  book=None, client=None) -> dict:
        """Decide, for **this leg alone**, whether a FAK order goes out — or nothing at all.

        Operator rule (corrected 2026-09-12): **leg-level independent permission and never a
        passive fallback.**  A YES leg takes only while its own price is inside the window that
        applies **to that leg** — the leg's own ``(floor, cap]`` when it carries one (the parallel
        next-bucket channel), otherwise the cfg band ``(yes_min_ask, yes_max_ask]`` (defaults
        0.48 / 0.90); a non-YES leg takes while its own cap and own book allow it.  Every other
        outcome is a **refusal/skip**, never a resting ``post_only`` order — a passive order on a
        fake breakout fills at the bid and buys a bucket that is going to zero.

        Fail-closed, in order: unusable cap (missing / unparseable / ``<= 0`` / ``> 1``) ⇒ refuse;
        **unusable leg window** (``leg_window_unusable``: floor ``>=`` cap, non-numeric, negative,
        …) ⇒ refuse — never a fallback to the cfg band; no resting ask at all in this leg's own
        book ⇒ ``no_book`` skip; an ask that is *present but unusable* (non-numeric / ``<= 0`` /
        ``> 1``) ⇒ ``ask_out_of_range`` refuse — a bad quote is not an empty book; for a YES leg
        only, no price evidence or a price outside its window ⇒ refuse.  Pure decision (plus the
        optional read-only YES re-quote); no order is placed and nothing is mutated except this
        port's per-fire memo.

        .. note:: **Which window can stand a fire down (F-E, 2026-09-12 — corrected invariant).**
           A leg that carries its own window is judged **only** against that window: the cfg band
           does not participate, so a corrupt ``yes_min_ask``/``yes_max_ask`` (e.g. 0.95 / 0.90)
           no longer stands a next-bucket leg down — that leg is still governed by its own
           fail-closed window.  The cfg band still governs a leg with **no** window at all (the
           legacy target-bucket / sleeve legs), and there an illegal band still refuses
           (``yes_band_unparsed``) and therefore still stands that leg — and with it the whole
           fire — down.  The old blanket claim "a corrupt cfg band stands the whole fire down" is
           no longer true once a fire carries an independent leg window, and it is **not** a
           safety hole: the leg window is itself fail-closed and never falls back.

        (``min_order_size`` is checked by :meth:`match`, not here: this method answers *"is this leg
        allowed to take?"*, and the venue's minimum is a property of the order's **size**.)
        """
        fire = fire if isinstance(fire, dict) else {}
        leg = leg if isinstance(leg, dict) else {}
        band = leg_take_window(leg, fire, cfg)
        is_yes = is_yes_leg(leg)
        gate = GATE_YES_BAND if is_yes else GATE_NO_LEG_ASK
        base = {"taker": False, "order_mode": SKIP, "taker_gate": gate,
                "yes_price": None, "source": "n/a", "cap": None,
                "lo": band["lo"], "hi": band["hi"], "leg": leg.get("leg"),
                "entry_channel": band.get("channel"), "window_source": band.get("source"),
                # B/C（2026-09-13）：窗口的可读标签（next_bucket 为闭区间 ``[lo, hi]``）与
                # **闭下界**标记；``match`` 据此写出 ``leg_window`` 并判定地板拒绝。
                "label": band.get("label"), "closed_lo": bool(band.get("closed_lo"))}
        cap = taker_cap_number(leg.get("cap"))
        if cap is None:
            return {**base, "reason": FILL_REFUSE_NO_CAP,
                    "detail": (f"leg {leg.get('leg')!r} carries no usable cap "
                               f"(cap={leg.get('cap')!r}; need 0 < cap <= 1) — refuse")}
        base["cap"] = cap
        if not band["ok"]:
            return {**base, "reason": band["reason"], "source": band["source"],
                    "detail": band["detail"]}
        quote = ask_state_of(book)
        if not quote["present"]:
            return {**base, "reason": FILL_SKIP_NO_BOOK, "source": "leg_book",
                    "detail": (f"leg {leg.get('leg')!r} has no resting ask in its own book — "
                               f"no_book (no order is sent)")}
        if quote["ask"] is None:
            # a quote IS on the book but is not a usable price: calling that "no_book" would
            # point the operator at a missing feed instead of a corrupt one.
            return {**base, "reason": FILL_REFUSE_ASK_BAD, "source": "leg_book_bad_quote",
                    "detail": (f"leg {leg.get('leg')!r} quotes an unusable ask "
                               f"({quote['raw']!r}; need 0 < ask <= 1) — ask_out_of_range, "
                               f"refuse (no order is sent, never a passive order)")}
        ask = quote["ask"]
        base["ask"] = ask
        if not is_yes:
            # non-YES leg: its own cap and its own book depth are the whole rule
            return {**base, "taker": True, "order_mode": TAKER, "reason": FILL_TAKER_NO,
                    "source": "leg_book_ask", "detail": (f"NO leg ask {ask} <= cap {cap} — take "
                                                         f"(own cap + book depth)")}
        evidence = self._fire_yes_price(fire, leg, client)
        px = evidence["yes_price"]
        source = evidence["source"]
        if px is None:
            return {**base, "reason": FILL_REFUSE_UNKNOWN, "source": source,
                    "detail": "no YES price evidence — refuse (never a passive order)"}
        base["yes_price"] = px
        base["source"] = source
        # ---- C: 显式 Floor Guard（执行层价格下限）------------------------------
        # 有效地板：next_bucket = 腿自带 floor（0.27）；目标桶/legacy = cfg yes_min_ask（0.45）。
        floor_info = leg_effective_floor(leg, fire, cfg, band)
        base["floor"] = floor_info["floor"]
        base["floor_source"] = floor_info["source"]
        if band.get("closed_lo"):
            # B 项（2026-09-13，**仅 next_bucket**）：腿窗口是**闭区间** [floor, cap] —— 下界含等号
            # （ask == floor ⇒ 入场）。C 项：ask < floor ⇒ **显式地板拒绝**（新码 ask_below_floor，
            # 由 ``match`` 落 deny 审计）。fail-closed：不发单、不回退 cfg 带、不降级为挂单。
            floor = floor_info["floor"]
            if floor is None:
                return {**base, "reason": FILL_REFUSE_LEG_WINDOW, "source": band.get("source"),
                        "detail": ("leg floor unusable — refuse (fail-closed, never a cfg-band "
                                   "fallback, never a passive order)")}
            if px < floor:
                return {**base, "reason": FILL_REFUSE_ASK_BELOW_FLOOR, "source": "leg_floor",
                        "detail": (f"YES {px} < leg floor {floor} (chan={band.get('channel')}) — "
                                   f"ask_below_floor: refuse (fail-closed; no order, no cfg-band "
                                   f"fallback, no passive order)")}
            if px > band["hi"]:
                return {**base, "reason": FILL_REFUSE_ABOVE, "source": source,
                        "detail": (f"YES {px} > leg cap {band['hi']} — refuse, no order sent "
                                   f"({source})")}
            return {**base, "taker": True, "order_mode": TAKER, "reason": FILL_TAKER,
                    "detail": f"YES {px} in [{floor}, {band['hi']}] — take ({source})"}
        if in_yes_band(px, band["lo"], band["hi"]):
            return {**base, "taker": True, "order_mode": TAKER, "reason": FILL_TAKER,
                    "detail": f"YES {px} in ({band['lo']}, {band['hi']}] — take ({source})"}
        below = px <= band["lo"]
        return {**base, "reason": FILL_REFUSE_BELOW if below else FILL_REFUSE_ABOVE,
                "detail": (f"YES {px} {'<=' if below else '>'} band "
                           f"({band['lo']}, {band['hi']}] — refuse, no order sent ({source})")}

    # ---------------------------------------------------------------- fill
    def _audit_floor_deny(self, ctx: dict, leg: dict, fire: dict | None) -> None:
        """C: 把**显式地板拒绝**写成一条 ``deny`` 审计行（best effort；绝不阻塞、绝不发单）。

        走 transport 既有的 ``_deny_audit`` 机制（与 ``no_fak_order_type`` / ``above_cap`` 等
        early-deny 行同源），字段固定带 ``ask`` / ``floor`` / ``leg`` / ``entry_channel`` /
        ``window_source``。transport 没有该钩子（测试桩）或写盘失败 ⇒ 静默跳过：拒绝已经生效，
        审计只是证据，绝不能让审计失败反过来放行一笔单。
        """
        deny = getattr(self.transport, "_deny_audit", None)
        if not callable(deny):
            return
        params = {"ask": (str(ctx.get("ask")) if ctx.get("ask") is not None else None),
                  "floor": (str(ctx.get("floor")) if ctx.get("floor") is not None else None),
                  "leg": leg.get("leg"),
                  "entry_channel": ctx.get("entry_channel"),
                  "window_source": ctx.get("window_source")}
        try:
            deny(self.audit_path, FILL_REFUSE_ASK_BELOW_FLOOR, params)
        except Exception:  # noqa: BLE001 - 审计失败绝不改判（这笔单本来就不发）
            pass

    @staticmethod
    def _fill_cost(result: dict) -> dict:
        """③（2026-09-13）把 transport 的 fill 回报折算成**实际成交成本**。

        优先**名义额 USDC**（``notional_usdc``，链上真实扣款），其次 ``实际成交股数 × 实际成交均价``
        （``avg_price``）。二者都取不到 ⇒ cost=ZERO 且 ``cost_source=unavailable`` —— 绝不退回
        ``股数 × 限价``（那正是本次修掉的缺陷：限价 0.31 会把 5.04582 记成 10.03625）。
        """
        filled = result.get("filled_shares") or ZERO
        notional = result.get("notional_usdc")
        if notional is not None:
            try:
                return {"cost": Decimal(str(notional)), "cost_source": "notional_usdc"}
            except (InvalidOperation, ValueError):
                pass
        avg = result.get("avg_price")
        if filled > ZERO and avg is not None:
            try:
                return {"cost": (filled * Decimal(str(avg))).quantize(Decimal("0.0001")),
                        "cost_source": "shares_x_avg_price"}
            except (InvalidOperation, ValueError):
                pass
        return {"cost": ZERO, "cost_source": "unavailable" if filled > ZERO else "no_fill"}

    def match(self, *, leg: dict, book: Any, limit: Decimal, shares: Decimal, fire: dict | None = None,
              cfg: dict | None = None) -> dict:
        """One **take-or-nothing** order through the v2 transport; real fills come back.

        A leg either goes out as an aggressive **FAK** take (YES leg inside its band; non-YES leg
        with a resting ask inside its cap) or it does not go out at all — ``match`` never places a
        passive/``post_only`` order (see the module docstring).  The decision, its gate and the
        price evidence are returned as ``order_mode`` (``taker``/``skip``) / ``taker_gate`` /
        ``yes_price`` and — because a pure decision must not grow the audit log — travel on the
        existing ``intent``/``submit`` rows (``audit_extra``), not on a row of their own.
        """
        client = getattr(self, "_client", None)
        if client is None:
            return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                    "status": "live_not_preflighted", "source": LIVE,
                    "detail": "preflight must run before any order"}
        ctx = self.fill_mode(fire=fire, leg=leg, cfg=cfg, book=book, client=client)
        #: the window the decision was actually judged against, as a readable label (audit).
        #: next_bucket 通道的输出是**闭区间** ``[lo, hi]``（B 项）；其余通道仍是 ``(lo, hi]``。
        leg_window = ctx.get("label")
        if leg_window is None and ctx.get("lo") is not None and ctx.get("hi") is not None:
            leg_window = f"({ctx['lo']}, {ctx['hi']}]"
        audit_extra = {"taker_gate": ctx["taker_gate"], "taker_gate_ok": ctx["taker"],
                       "yes_price": str(ctx["yes_price"]) if ctx["yes_price"] is not None else None,
                       "yes_price_source": ctx["source"], "fill_mode": ctx["reason"],
                       "leg": leg.get("leg"),
                       # 通道来源（端口从腿/fire 推导的权威值；下面 execute_leg 会再盖章一次，
                       # 外部 audit_extra 无法伪造或翻转）
                       "entry_channel": ctx.get("entry_channel"),
                       "leg_window": leg_window,
                       "window_source": ctx.get("window_source"),
                       "next_entry_window": (fire or {}).get("next_entry_window")}
        if not ctx["taker"]:
            # take-or-nothing: a leg that is out of band / uncapped / unpriced / has no book is
            # dropped here.  No order of ANY kind is sent — the historical passive fallback is gone.
            if ctx.get("reason") == FILL_REFUSE_ASK_BELOW_FLOOR:
                # C（2026-09-13）：显式地板拒绝**必须落审计**（这是它与其他纯决策的唯一区别）。
                # best effort：审计写失败绝不改判（拒绝已经生效，且本来就不发单）。
                self._audit_floor_deny(ctx, leg, fire)
            return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                    "status": ctx["reason"], "order_id": None, "residual_risk": False,
                    "limit_price": None, "clamped": False, "detail": ctx["detail"],
                    "order_mode": SKIP, "taker_gate": ctx["taker_gate"],
                    "yes_price": ctx["yes_price"], "fill_and_kill": False, "source": LIVE,
                    "entry_channel": ctx.get("entry_channel"), "leg_window": leg_window}
        # F-D（2026-09-12，fail-closed 本地守卫）：计划股数 < venue 的 min_order_size ⇒ 本腿弃单，
        # **不发**注定被交易所拒的单（审计 finding：引擎 fire 路径从不检查 min_order_size）。判定
        # 只在 venue 真的给了可用 min 时才生效（缺失/畸形 ⇒ 不加新拒单，行为与以前逐字相同）。
        min_size = _book_min_order_size(book)
        if min_size is not None and shares < min_size:
            return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                    "status": FILL_REFUSE_BELOW_MIN, "order_id": None, "residual_risk": False,
                    "limit_price": None, "clamped": False, "source": LIVE,
                    "detail": (f"planned {shares} share(s) < venue min_order_size {min_size} — "
                               f"refuse locally, nothing sent (below_min_order_size)"),
                    "order_mode": SKIP, "taker_gate": ctx["taker_gate"],
                    "yes_price": ctx["yes_price"], "fill_and_kill": False,
                    "min_order_size": str(min_size),
                    "entry_channel": ctx.get("entry_channel"), "leg_window": leg_window}
        
        # 盘口买一/卖一深度预审 (Liquidity Guard: 可吃单名义额必须 >= 2.0 USDC)
        if isinstance(book, dict):
            asks = book.get("asks") or []
            if asks and isinstance(asks[0], dict):
                try:
                    ask_px = Decimal(str(asks[0].get("price") or 0))
                    ask_sz = Decimal(str(asks[0].get("size") or 0))
                    top_notional = ask_px * ask_sz
                    if top_notional < Decimal("2.0"):
                        return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                                "status": "refuse_shallow_depth", "order_id": None, "residual_risk": False,
                                "limit_price": None, "clamped": False, "source": LIVE,
                                "detail": (f"top ask depth notional {top_notional:.2f} USDC < 2.0 USDC — "
                                           f"refuse locally to prevent slippage/dust (shallow_book_depth)"),
                                "order_mode": SKIP, "taker_gate": ctx["taker_gate"],
                                "yes_price": ctx["yes_price"], "fill_and_kill": False,
                                "entry_channel": ctx.get("entry_channel"), "leg_window": leg_window}
                except Exception:
                    pass
        token_id = leg.get("token_id")
        neg_risk, neg_src = self.resolve_neg_risk(client, token_id, book)
        if neg_risk is None:
            # 签名域未知 ⇒ 弃单（fail-closed）。按错误的 neg-risk 域签名会被 CLOB 以
            # "invalid POLY_PROXY signature" 拒绝，那是静默失败的源头。
            return {"filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": shares,
                    "status": "neg_risk_unknown", "order_id": None, "residual_risk": False,
                    "limit_price": None, "clamped": False, "source": LIVE,
                    "detail": "neg-risk signing domain unknown; refusing to sign a mis-scoped order",
                    "order_mode": SKIP, "neg_risk_source": neg_src,
                    "yes_price": ctx["yes_price"], "fill_and_kill": False,
                    "entry_channel": ctx.get("entry_channel"), "leg_window": leg_window}
        result = self.transport.execute_leg(
            client,
            token_id=str(token_id),
            side=str(leg.get("side") or "BUY"),
            price=limit,
            size=shares,
            book=book,
            tick=leg.get("tick") or (book or {}).get("tick_size") if isinstance(book, dict) else None,
            neg_risk=bool(neg_risk),
            gates=self.gates,
            post_only=False,
            clamp=False,
            taker=True,
            cap=ctx["cap"],
            poll_attempts=self.poll_attempts,
            poll_sleep=self.poll_sleep,
            sleep=self.sleep,
            audit_path=self.audit_path,
            # 通道字段走**专用入参**（不是 audit_extra）：execute_leg 在合并完 audit_extra 之后
            # 用它盖章，因此调用方塞进 audit_extra 的 entry_channel/leg_window 一律被剔除 ⇒ 不可伪造。
            # F-B/F-C（2026-09-12）：window_source / next_entry_window 与 neg_risk_source 同样改走专用
            # 入参（此前它们只在 audit_extra 里 ⇒ 裸 API 调用方可注入同名字段）。
            leg_channel=ctx.get("entry_channel"),
            leg_window=leg_window,
            leg_window_source=ctx.get("window_source"),
            leg_next_entry_window=(fire or {}).get("next_entry_window"),
            neg_risk_source=neg_src,
            audit_extra=dict(audit_extra),
        )
        booked = self._fill_cost(result)
        return {"filled_shares": result.get("filled_shares") or ZERO,
                "avg_price": result.get("avg_price"),
                "cost": booked["cost"],
                "cost_source": booked["cost_source"],
                "notional_usdc": result.get("notional_usdc"),
                "unfilled": result.get("unfilled") if result.get("unfilled") is not None else shares,
                "status": result.get("status"),
                "order_id": result.get("order_id"),
                "residual_risk": bool(result.get("residual_risk")),
                "limit_price": result.get("limit_price"),
                "clamped": bool(result.get("clamped")),
                "detail": result.get("detail", ""),
                "order_mode": result.get("order_mode") or TAKER,
                "taker_gate": ctx["taker_gate"],
                "yes_price": ctx["yes_price"],
                "fill_and_kill": bool(result.get("fill_and_kill")),
                "entry_channel": ctx.get("entry_channel"), "leg_window": leg_window,
                "source": LIVE}

    def describe(self) -> dict:
        return {"mode": LIVE, "transport": "live.v2_transport (py-clob-client-v2)",
                "released": list(getattr(self.transport, "RELEASE_WRITE_METHODS", ())),
                "gates": self.gates.get("checks"), "limits": self.limits,
                "order_modes": [TAKER, SKIP], "passive_fallback": False,
                "taker_gates": [GATE_YES_BAND, GATE_NO_LEG_ASK],
                "taker_scope": "leg_level_independent", "yes_legs": list(YES_LEG_NAMES),
                "entry_channels": list(ENTRY_CHANNELS),
                #: F-D：本端口**自己**就拒的那些单（不必等交易所回 400）；C 项加入地板拒绝
                "local_refusals": [FILL_REFUSE_BELOW_MIN, FILL_REFUSE_ASK_BELOW_FLOOR],
                "min_order_size_rule": ("腿的计划股数 < 该腿 book 的 min_order_size ⇒ 本地弃单 "
                                        "(order_mode=skip, status=below_min_order_size, 不发单)；"
                                        "book 未提供该字段 ⇒ 不新增拒单（按未知处理）"),
                #: C 项：显式执行层地板（fail-closed，落 deny 审计，绝不回退/降级）
                "floor_guard_rule": ("YES 腿有效 best ask < 有效地板 ⇒ 拒单 ask_below_floor + deny "
                                     "审计（ask/floor/leg/entry_channel/window_source）；有效地板："
                                     "next_bucket = 腿自带 floor(0.27)，目标桶 = cfg yes_min_ask(0.45)"),
                "next_entry_window_rule": ("entry_channel == 'next_bucket' ⇒ 腿自带 [floor, cap] "
                                           "**闭区间**窗口生效（下界与上界都含等号），cfg 的 "
                                           "yes_min_ask/yes_max_ask 不动它；无腿窗口才回退 cfg；"
                                           "目标桶通道仍是半开 (yes_min_ask, yes_max_ask]")}


def _slim(account: dict) -> dict:
    return {key: account.get(key) for key in
            ("usdc_balance", "open_orders", "positions_value_usdc")}


def live_limits(env: dict) -> dict:
    """``LIVE_*`` hard caps from the environment (same keys the read-only layer uses)."""
    return {name: env.get(key) or None for name, key in reconcile.LIMIT_KEYS.items()}


_CACHE: dict[str, ExecutionPort] = {}


def port_status(cfg: dict, env: dict | None = None, *, enable_submit: bool | None = None,
                confirm: str | None = None) -> dict:
    """Read-only: which port would be handed out and, for live, which gate is missing."""
    env = env if env is not None else creds_mod.load_env_file()
    mode = str((cfg or {}).get("mode") or PAPER).strip().lower()
    if mode == PAPER:
        return {"mode": PAPER, "ok": True, "reason": REASON_OK, "detail": "paper port available"}
    if mode != LIVE:
        return {"mode": mode, "ok": False, "reason": REASON_UNKNOWN_MODE,
                "detail": f"unknown mode {mode!r} (want paper/live)"}
    enable = (env.get(ENV_ENABLE_SUBMIT) == "1") if enable_submit is None else bool(enable_submit)
    gates = submit.gate_status(enable_submit=enable, env=env,
                               confirm=env.get(ENV_CONFIRM) if confirm is None else confirm)
    return {"mode": LIVE, "ok": gates["ok"], "reason": gates["ok"] and REASON_OK or gates["reason"],
            "detail": gates["detail"], "gates": gates, "limits": live_limits(env)}


def get_port(cfg: dict, env: dict | None = None, *, enable_submit: bool | None = None,
             confirm: str | None = None, transport=None, port: ExecutionPort | None = None) -> ExecutionPort:
    """Hand out the port for ``cfg['mode']``.

    ``paper`` → :class:`PaperPort` (no gates, no third-party imports).
    ``live``  → :class:`LivePort`, but only when **all three** service gates are satisfied
    (``YES2RE_LIVE_ENABLE_SUBMIT=1``, ``LIVE_SUBMIT_ENABLED=1``, today's confirm phrase);
    otherwise :class:`PortRefused` — the caller stands the fire down, it never falls back to
    paper silently.
    """
    if port is not None:
        return port
    mode = str((cfg or {}).get("mode") or PAPER).strip().lower()
    if mode == PAPER:
        return _CACHE.setdefault(PAPER, PaperPort())
    if mode != LIVE:
        raise PortRefused(REASON_UNKNOWN_MODE, f"unknown mode {mode!r} (want paper/live)", mode=mode)

    env = env if env is not None else creds_mod.load_env_file()
    status = port_status(cfg, env, enable_submit=enable_submit, confirm=confirm)
    if not status["ok"]:
        raise PortRefused(status["reason"], status["detail"])
    if LIVE in _CACHE:
        return _CACHE[LIVE]
    if transport is None:                     # lazy: the paper path never imports the v2 SDK
        try:
            from . import v2_transport as transport  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise PortRefused(REASON_LIVE_DEPS, f"{type(exc).__name__}: {exc}") from None
    available = getattr(transport, "sdk_available", None)
    if callable(available) and not available():
        # precise reason instead of discovering the missing SDK mid-preflight (still fail-closed)
        raise PortRefused(REASON_LIVE_DEPS, getattr(transport, "V2_HINT", "py-clob-client-v2 missing"))
    live = LivePort(transport, env=env, gates=status["gates"], limits=status["limits"])
    _CACHE[LIVE] = live
    return live


def reset_cache() -> None:
    """Forget cached ports (tests / operator re-configuration)."""
    _CACHE.clear()


def _demo() -> None:
    paper = get_port({"mode": "paper"}, env={})
    assert isinstance(paper, PaperPort) and paper.mode == PAPER
    assert paper.preflight(fire={}, cfg={})["ok"] is True
    assert paper.match(leg={}, book={"asks": [{"price": "0.50", "size": "10"}]},
                       limit=Decimal("0.50"), shares=Decimal("2"))["filled_shares"] == Decimal("2")
    assert PaperPort().fund(state={}, cfg={}, fire={}, total_cost=ZERO)["ok"] is True
    try:
        get_port({"mode": "live"}, env={})
    except PortRefused as exc:
        assert exc.reason == submit.GATE_FLAG, exc.reason
    else:
        raise AssertionError("live without gates must be refused")
    try:
        get_port({"mode": "warp"}, env={})
    except PortRefused as exc:
        assert exc.reason == REASON_UNKNOWN_MODE
    else:
        raise AssertionError("unknown mode must be refused")
    print("port demo OK")


if __name__ == "__main__":
    _demo()
