"""live 真实 SELL 出场通道 —— 消除 live 下的「虚拟平仓」(2026-09-13)。

**问题（生产隐患）**：引擎的 4 处真实持仓退场原先一律走
``paper_capital.close_leg_at_best_bid`` —— 按 ``best_bid`` 记账、把腿标成 ``settled``、
**不下真实卖单**。在 live 下这意味着真币仍留在钱包、链上之后按 1/0 结算，而引擎账本却以为
已在 bid 出场 ⇒ **账本与真实账户背离**（与「权益一律以真实账户为准」的口径直接冲突）。

**本模块**给出 live 侧的真实 FAK 卖通道，并把「决策」与「网络」分开：

纯函数（零 I/O，可直接单测）
  * :func:`exit_settings` —— 解析 ``live_sell_enabled`` / ``live_sell_floor`` /
    ``live_sell_max_attempts_per_cycle``（缺失/非法 ⇒ fail-closed，绝不回退成无保护卖单）
  * :func:`best_bid_of` / :func:`parse_floor` / :func:`plan_live_exit` —— 一次退场尝试的决策
    （**卖 / 弃**）与端点语义（``bid < floor`` 不卖，``bid == floor`` 卖）
  * :func:`apply_live_exit_fill` —— 按**真实成交**更新账本：部分成交只减对应股数，
    ``leg.settled`` 仅在股数清零时置位，``pos.liquidated`` 仅在该仓所有 YES 腿了结时置位；
    taker 费（默认 2%）显式记账（``exit_fee_usdc``）与净回收

网络（全部经注入的 ``channel``；测试用桩 transport/桩 client 覆盖，零真实订单）
  * :class:`LiveExitChannel` / :func:`get_channel` —— 三闸门 + 真实客户端，**不过**入场型上限
    （卖出不消耗额度、不新增持仓）
  * :func:`live_exit_leg` —— 撤真实挂单（先）→ 读盘口 → 地板/闸门守卫 → SELL FAK
    （``execute_leg``，``floor=`` 保护限价）→ 按真实成交记账 → 审计（``side=SELL`` /
    ``exit_channel`` / ``sell_floor`` / ``leg_window`` / 成交明细 / 卖后真实账户）

**不变式**：live 下绝不调用 ``paper_capital.close_leg_at_best_bid``（``_r_cycle`` 的
``if mode == "live"`` 分支 + 运行时哨兵）；fail-closed —— 被拒 / 无买盘 / 深度不足 / 地板不满足 ⇒
仓位保持 open、**绝不**标 ``liquidated``、记 ``exit_deferred``、下轮重试；至结算仍未卖出则走既有
``settle_markets`` 路径（链上 1/0，账本以真实结算为准）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from paper_capital import release

from . import submit

ZERO = Decimal("0")
ONE = Decimal("1")
#: 股数精度（venue 的 market taker amount：4 位小数）
QTY = Decimal("0.0001")
#: 金额精度（与 ``paper_capital.close_leg_at_best_bid`` 的 proceeds 量化一致）
USDC = Decimal("0.0001")

SELL = "SELL"
BUY = "BUY"

#: 退场通道（写进每一条事件/审计行；操作者规则 ⑪）。字面量与 ``live/v2_transport.EXIT_CHANNELS``
#: 必须一致 —— 那边是审计盖章用的白名单副本。
CH_EARLY_STOP = "early_stop"          # 抢先止损（strategy.evaluate_early_stop_loss）
CH_REFIRE_LIQ = "refire_liq"          # 追火旧桶清算（record_refire）
CH_SLEEVE_TIMEOUT = "sleeve_timeout"  # 未破位 sleeve 超时退场
CH_BREACH_RC = "breach_rc"            # METAR 破位风控清算
CH_TAKE_PROFIT = "take_profit"        # 50% 阶梯止盈
CH_ABORTION_RC = "abortion_rc"        # 气象不可逆夭折证伪
EXIT_CHANNELS = (CH_EARLY_STOP, CH_REFIRE_LIQ, CH_SLEEVE_TIMEOUT, CH_BREACH_RC,
                 CH_TAKE_PROFIT, CH_ABORTION_RC)

#: 决策动作
ACTION_PAPER = "paper"   # 非 live ⇒ 调用方走既有 paper 平仓（逐字不变）
ACTION_SELL = "sell"     # live 真实 FAK 卖
ACTION_DEFER = "defer"   # fail-closed：这轮不卖（仓位保持 open，下轮重试）

#: 延期/拒单原因（全部 fail-closed：绝不 drop 余量、绝不虚拟平仓）
REASON_NOT_LIVE = "not_live_mode"
REASON_DISABLED = "live_sell_disabled"
REASON_FLOOR_REQUIRED = "exit_floor_required"
REASON_FLOOR_INVALID = "exit_floor_invalid"
REASON_NO_BID = "no_bid"
REASON_NO_BOOK = "no_book"
REASON_BELOW_FLOOR = "below_sell_floor"
REASON_NO_SHARES = "no_open_shares"
#: 余量低于 venue 最小下单量（``book.min_order_size``，单位 = 股）⇒ **本地弃单、零发单**。
#: 与入场侧 F-D 守卫（``live/port.py::FILL_REFUSE_BELOW_MIN`` / ``order_plan.BELOW_MIN_ORDER_SIZE``）
#: **同一根因、同一 reason 字面量**：股数 < venue 最小量时发出去只会被交易所拒（2026-09-13 r104 实测：
#: 部分成交后账面余量 0.0041 股，每轮真实提交、每轮 400 ``invalid maker amount`` ⇒ 无限空转）。
#: 弃单只挂在本腿的余量上：仓位保持 open、等 ``settle`` 兜底，绝不降级、绝不虚拟平仓。
REASON_BELOW_MIN = "below_min_order_size"
REASON_ATTEMPTS = "attempts_exhausted"
REASON_NO_CLIENT = "live_client_unavailable"
REASON_NO_TOKEN = "no_token_id"
REASON_GATES = "gates_missing"
REASON_REFUSED = "refused"
REASON_DEPTH = "insufficient_depth"
REASON_NO_ORDER = "no_order_id"
REASON_NEG_RISK = "neg_risk_unknown"
REASON_NO_ORDER_API = "no_market_order_api"
#: 通道不可用（三闸门未全过）时的原因前缀：``gate_<submit.gate_status 的 reason>``
GATE_PREFIX = "gate_"

#: 新增 config 键的代码默认（``config/yes2re_reversal.json`` 的 ``consensus_lock`` 同步）
#: **fail-safe（2026-09-13 事故后定）**：真实 SELL 属新的链上动作，必须由 config **显式开启**。
#: 事故背景：本键曾默认为 True，r94 覆盖 config 时删掉该键 ⇒ 回落成"开" ⇒ **重启即启用真实卖出**。
#: 依赖"第三方可删的配置键"来关闭资金动作是不安全的，故默认值改为关闭（与 next_entry_enabled 同模式）。
DEFAULT_LIVE_SELL_ENABLED = False
DEFAULT_LIVE_SELL_FLOOR = "0.05"
DEFAULT_LIVE_SELL_MAX_ATTEMPTS_PER_CYCLE = 1
DEFAULT_TAKER_FEE_RATE = "0.02"

#: 不可用（fail-closed）的成交状态：这些状态**不算**卖出，仓位保持 open
_NO_FILL_STATUSES = ("exit_floor_required", "exit_floor_invalid", "below_exit_floor",
                     "price_out_of_range", "price_not_on_tick", "amount_below_precision",
                     "below_min_order_size",
                     "taker_cap_required", "above_cap", "no_fak_order_type",
                     "no_market_order_api", "submit_failed", "no_order_id",
                     "cant_cancel_first", "neg_risk_unknown", "live_client_unavailable",
                     "live_not_preflighted", "invalid_input", "not_started", "poll_error")


class ExitRefused(RuntimeError):
    """live 退场通道不可用（三闸门未全过 / 未知模式）—— 调用方 fail-closed 延期，绝不降级。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- 小工具（纯）

def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return out if out.is_finite() else None


def _dec_or_zero(value: Any) -> Decimal:
    out = _dec_or_none(value)
    return out if out is not None and out > ZERO else ZERO


def parse_floor(value: Any) -> Decimal | None:
    """地板解析：可用 ⇒ ``0 < floor <= 1`` 的 ``Decimal``；否则 ``None``（调用方拒单）。

    缺失 / 不可解析 / 非有限 / ``<= 0`` / ``> 1`` 一律 ``None`` —— **绝不**用默认值兜底。
    """
    out = _dec_or_none(value)
    if out is None or out <= ZERO or out > ONE:
        return None
    return out


def best_bid_of(book: Any) -> Decimal | None:
    """盘口 ``best_bid``；缺失/非法/``<= 0`` ⇒ ``None``（= 无买盘，fail-closed 不卖）。"""
    if not isinstance(book, dict):
        return None
    out = _dec_or_none(book.get("best_bid"))
    return out if out is not None and out > ZERO else None


def book_min_order_size(book: Any) -> Decimal | None:
    """盘口 ``min_order_size``（venue 最小下单量，单位 = 股）；不可用 ⇒ ``None``。

    缺失 / 不可解析 / 非有限 / ``<= 0`` ⇒ ``None`` = **不知道** venue 最小量 ⇒ 守卫不生效（fail-open）。
    与入场侧 F-D 守卫同口径（``live/port.py::_book_min_order_size``）：只有确实知道最小量、
    且股数**确实小于**它时才本地弃单 —— 不知道 ⇒ 维持原行为，绝不因为缺字段挡住真实退场。
    """
    if not isinstance(book, dict):
        return None
    out = _dec_or_none(book.get("min_order_size"))
    return out if out is not None and out > ZERO else None


def _mode_of(cfg: dict | None) -> str:
    return str((cfg or {}).get("mode") or "paper").strip().lower()


#: 每腿每轮尝试上限的状态落点（**只在 live 下写** ⇒ paper 的 state/golden 逐字不变）。
#: ``cycle`` 由 ``run_cycle`` 每轮 +1；``n`` 是本轮该 token 真实下单的次数。
STATE_CYCLE_KEY = "live_exit_cycle"
STATE_ATTEMPTS_KEY = "live_exit_attempts"


def bump_cycle(state: dict[str, Any]) -> int:
    """live 轮次序号 +1（由 ``run_cycle`` 每轮调用一次）。

    ``live_sell_max_attempts_per_cycle`` 的判定基准：同一轮内同一 token 的真实下单次数。
    只在 live 下写状态，因此不会给 paper 的 state/golden 添任何键。
    """
    state[STATE_CYCLE_KEY] = int(state.get(STATE_CYCLE_KEY) or 0) + 1
    return state[STATE_CYCLE_KEY]


def attempts_this_cycle(state: dict[str, Any], token: Any) -> int:
    """本轮（``STATE_CYCLE_KEY`` 相同）该 token 已经真实下单的次数；跨轮 ⇒ 0。"""
    rec = (state.get(STATE_ATTEMPTS_KEY) or {}).get(str(token))
    if not isinstance(rec, dict) or rec.get("cycle") != state.get(STATE_CYCLE_KEY):
        return 0
    try:
        return int(rec.get("n") or 0)
    except (TypeError, ValueError):
        return 0


def note_attempt(state: dict[str, Any], token: Any, channel: str,
                 now_utc: datetime | None = None) -> int:
    """记一次真实下单尝试（卖出**已发出**才算），返回本轮累计次数。"""
    n = attempts_this_cycle(state, token) + 1
    stamp = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state.setdefault(STATE_ATTEMPTS_KEY, {})[str(token)] = {
        "cycle": state.get(STATE_CYCLE_KEY), "n": n, "exit_channel": str(channel),
        "at_utc": stamp.isoformat().replace("+00:00", "Z")}
    return n


def _truthy(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def exit_settings(cfg: dict | None) -> dict[str, Any]:
    """解析 live 卖出配置（``strategy`` + ``consensus_lock`` 两个块的合并视图）。

    * ``live_sell_enabled``（代码默认 ``False`` = fail-safe；生产由 config 显式开启）：关闭 ⇒ 退场**只延期**（fail-closed，绝不做虚拟平仓，
      由 settle 兜底）—— 这是唯一的回退开关，回退后依然不存在虚拟平仓。
    * ``live_sell_floor``（默认 ``"0.05"``）：卖出地板；非法 ⇒ ``floor=None`` ⇒ 退场延期（拒单）。
    * ``live_sell_max_attempts_per_cycle``（默认 ``1``）：单轮对同一腿的最大尝试次数。
    * ``fee_rate``：taker 每边费率（``base_fee_rate``，默认 0.02），卖出按成交额显式扣。
    """
    src: dict[str, Any] = {}
    for key in ("strategy", "consensus_lock"):
        block = (cfg or {}).get(key)
        if isinstance(block, dict):
            src.update(block)
    enabled = _truthy(src.get("live_sell_enabled", DEFAULT_LIVE_SELL_ENABLED),
                      DEFAULT_LIVE_SELL_ENABLED)
    floor_raw = src.get("live_sell_floor", DEFAULT_LIVE_SELL_FLOOR)
    floor = parse_floor(floor_raw)
    attempts_raw = src.get("live_sell_max_attempts_per_cycle",
                           DEFAULT_LIVE_SELL_MAX_ATTEMPTS_PER_CYCLE)
    try:
        max_attempts = int(attempts_raw)
    except (TypeError, ValueError):
        max_attempts = -1
    fee_rate = _dec_or_none((cfg or {}).get("base_fee_rate", DEFAULT_TAKER_FEE_RATE))
    if fee_rate is None or fee_rate < ZERO or fee_rate >= ONE:
        fee_rate = Decimal(DEFAULT_TAKER_FEE_RATE)
    return {"enabled": enabled, "floor": floor, "floor_raw": floor_raw,
            "max_attempts": max_attempts, "fee_rate": fee_rate,
            "floor_reason": (None if floor is not None
                             else (REASON_FLOOR_REQUIRED if floor_raw is None
                                   else REASON_FLOOR_INVALID))}


def plan_live_exit(*, mode: str, settings: dict[str, Any], best_bid: Any = None,
                   gates: dict | None = None, attempts: int = 0,
                   shares: Any = None, min_order_size: Any = None) -> dict[str, Any]:
    """一次退场尝试的**纯决策**：``sell`` / ``defer`` / ``paper``（+ 机器可读 ``reason``）。

    判定顺序（全部 fail-closed，任何一条不过都**不卖**，且绝不回退到虚拟平仓）：

    1. 非 live ⇒ ``paper``（调用方走既有 paper 平仓，逐字不变）；
    2. ``live_sell_enabled=False`` ⇒ ``defer:live_sell_disabled``；
    3. 三闸门未全过 ⇒ ``defer:gate_<reason>``（卖出仍需三闸门）；
    4. 地板缺失 ⇒ ``defer:exit_floor_required``；地板非法 ⇒ ``defer:exit_floor_invalid``；
    5. 无可卖股数 ⇒ ``defer:no_open_shares``；
    6. 余量 < venue ``min_order_size``（股）⇒ ``defer:below_min_order_size``
       （**本地弃单、零发单**；venue 最小量未知 ⇒ 本条不生效，维持原行为）；
    7. 本腿本轮尝试次数 ≥ ``live_sell_max_attempts_per_cycle`` ⇒ ``defer:attempts_exhausted``；
    8. 无买盘（``best_bid`` 缺失/非法/``<= 0``）⇒ ``defer:no_bid``；
    9. ``best_bid < floor`` ⇒ ``defer:below_sell_floor``（端点语义：``== floor`` **卖**）；
    10. 否则 ``sell``。

    纯函数：只读入参，零 I/O、零状态变更。
    """
    bid = _dec_or_none(best_bid)
    base = {"action": ACTION_DEFER, "reason": None, "bid": bid,
            "floor": settings.get("floor"), "shares": _dec_or_none(shares),
            "gate": (gates or {}).get("reason") if isinstance(gates, dict) else None}
    if str(mode or "").strip().lower() != "live":
        return {**base, "action": ACTION_PAPER, "reason": REASON_NOT_LIVE}
    if not settings.get("enabled", True):
        return {**base, "reason": REASON_DISABLED}
    if not submit.gates_all_passed(gates):
        gate_reason = (gates or {}).get("reason") if isinstance(gates, dict) else None
        return {**base, "reason": f"{GATE_PREFIX}{gate_reason or REASON_GATES}"}
    if settings.get("floor") is None:
        return {**base, "reason": settings.get("floor_reason") or REASON_FLOOR_INVALID}
    held = _dec_or_zero(shares)
    if shares is not None and held <= ZERO:
        return {**base, "reason": REASON_NO_SHARES}
    minimum = _dec_or_none(min_order_size)
    if minimum is not None and minimum <= ZERO:
        minimum = None
    if minimum is not None and shares is not None and held < minimum:
        # venue 最小下单量是硬约束：小于它必然被交易所拒（r104 实测 400 invalid maker amount）
        # ⇒ 本地弃单（零发单、零尝试计数、仓位保持 open 等 settle），绝不降级、绝不虚拟平仓
        return {**base, "reason": REASON_BELOW_MIN, "min_order_size": minimum}
    try:
        max_attempts = int(settings.get("max_attempts", DEFAULT_LIVE_SELL_MAX_ATTEMPTS_PER_CYCLE))
    except (TypeError, ValueError):
        max_attempts = -1
    if max_attempts <= 0 or int(attempts or 0) >= max_attempts:
        return {**base, "reason": REASON_ATTEMPTS}
    if bid is None:
        return {**base, "reason": REASON_NO_BID}
    if bid < settings["floor"]:
        return {**base, "reason": REASON_BELOW_FLOOR}
    return {**base, "action": ACTION_SELL, "reason": "sell"}


def fill_failed_reason(fill: dict | None) -> str | None:
    """把一个 ``execute_leg`` 结果翻成「没卖出」的机器可读原因；真成交 ⇒ ``None``。

    ``None`` 的判定只用**成交事实**：``ok`` 为真且 ``filled_shares > 0``。其余（被拒 / 无买盘 /
    深度不足 ⇒ FAK 被冲掉 0 股 / 异常）一律算没卖出 ⇒ 调用方必须延期重试。
    """
    if not isinstance(fill, dict):
        return REASON_REFUSED
    filled = _dec_or_zero(fill.get("filled_shares"))
    if fill.get("ok") and filled > ZERO:
        return None
    status = str(fill.get("status") or "").strip().lower()
    detail = str(fill.get("detail") or "")
    if status in ("cancelled", "canceled") and not filled:
        # FAK 被交易所杀掉 0 成交（无对手盘 / 深度不足）
        return REASON_DEPTH if "no orders found to match" in detail.lower() else REASON_DEPTH
    if status in _NO_FILL_STATUSES:
        return status
    return status or REASON_REFUSED


# --------------------------------------------------------------------------- 账本（纯）

def apply_live_exit_fill(*, state: dict[str, Any], pos: dict[str, Any] | None,
                         leg: dict[str, Any], fill: dict[str, Any], channel: str,
                         floor: Decimal, fee_rate: Decimal,
                         now_utc: datetime | None = None,
                         account: dict | None = None) -> dict[str, Any]:
    """按**真实成交**更新账本与仓位状态（纯：只改传入的 state/pos/leg 对象）。

    规则（操作者拍板）：

    * 部分成交 ⇒ **只减** ``leg["shares"]``（余量留在仓位，下一轮续卖，绝不丢弃）；
    * ``leg.settled`` 仅在股数清零时置位（并记 ``closed_by`` / ``payout_credit_usdc`` = 净回收，
      与 ``paper_capital.close_leg_at_best_bid`` 的字段语义一致，供 equity_valuation 使用）；
    * ``pos.liquidated`` 仅在该仓**所有 YES 腿**已了结时置位；
    * taker 费显式记账（``exit_fee_usdc``）+ 净回收（``exit_net_usdc``），净额记回现金池
      （``paper_capital.release``，与真实钱包回收一致）；
    * 余量 > 0 ⇒ 在该仓登记 ``pending_exit``（下一轮续卖的入口）。
    """
    now = now_utc or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    filled = _dec_or_zero(fill.get("filled_shares"))
    avg = _dec_or_none(fill.get("avg_price"))
    if avg is None and filled > ZERO:
        # 平均值读不到时用限价（= best_bid）估值：保守且可审计，绝不用 0 抹掉真实回收
        avg = _dec_or_none(fill.get("limit_price")) or floor
    limit = _dec_or_none(fill.get("limit_price"))
    shares_before = _dec_or_zero(leg.get("shares"))
    gross = (filled * avg).quantize(USDC) if (filled > ZERO and avg is not None) else ZERO
    fee = (gross * fee_rate).quantize(USDC) if gross > ZERO else ZERO
    net = (gross - fee).quantize(USDC) if gross > ZERO else ZERO
    remaining = shares_before - filled
    if remaining < ZERO:
        remaining = ZERO
    remaining = remaining.quantize(QTY)
    if net > ZERO:
        release(state, net)
    if filled > ZERO:
        leg["shares"] = str(remaining)
        leg["exit_gross_usdc"] = str((_dec_or_zero(leg.get("exit_gross_usdc")) + gross).quantize(USDC))
        leg["exit_fee_usdc"] = str((_dec_or_zero(leg.get("exit_fee_usdc")) + fee).quantize(USDC))
        leg["exit_net_usdc"] = str((_dec_or_zero(leg.get("exit_net_usdc")) + net).quantize(USDC))
        leg["exit_channel"] = str(channel)
        leg["sell_floor"] = str(floor)
        leg["bid_at_close"] = str(limit) if limit is not None else None
        fills = leg.setdefault("exit_fills", [])
        fills.append({"shares": str(filled), "price": str(avg) if avg is not None else None,
                      "gross_usdc": str(gross), "fee_usdc": str(fee), "net_usdc": str(net),
                      "order_id": fill.get("order_id"), "exit_channel": str(channel),
                      "at_utc": stamp})
        if fill.get("order_id"):
            leg["exit_last_order_id"] = fill.get("order_id")
        leg_settled = remaining <= ZERO
        if leg_settled:
            # 只有**全部股数已卖出**才置位 settled（部分成交绝不置位）
            leg["settled"] = True
            leg["leg_won"] = False
            leg["closed_by"] = str(channel)
            leg["closed_at_utc"] = stamp
            leg["settled_at_utc"] = stamp
            leg["close_proceeds_usdc"] = leg["exit_net_usdc"]
            leg["payout_credit_usdc"] = leg["exit_net_usdc"]
            leg["resolution_source"] = f"live_sell:{channel}"
    else:
        leg_settled = False
    # ---- 仓位状态：liquidated 只看 YES 腿是否全部了结（NO 腿仍走 settle 兜底）-----------
    yes_legs = [lg for lg in (pos or {}).get("legs", [])
                if str(lg.get("outcome") or "").upper() == "YES"]
    pos_liquidated = bool(yes_legs) and all(lg.get("settled") for lg in yes_legs)
    if pos is not None:
        if pos_liquidated:
            pos["liquidated"] = True
            pos["liquidated_at_utc"] = stamp
        if remaining > ZERO and str(channel) != CH_TAKE_PROFIT:
            pending = pos.setdefault("pending_exit", {})
            pending[str(leg.get("token_id") or "")] = {
                "token_id": leg.get("token_id"), "leg": leg.get("leg"),
                "bucket_id": leg.get("bucket_id"), "exit_channel": str(channel),
                "shares": str(remaining), "sell_floor": str(floor),
                "updated_at_utc": stamp, "last_reason": fill_failed_reason(fill),
            }
        elif isinstance(pos.get("pending_exit"), dict):
            pos["pending_exit"].pop(str(leg.get("token_id") or ""), None)
            if not pos["pending_exit"]:
                pos.pop("pending_exit", None)
    return {
        "channel": str(channel), "deferred": False,
        "reason": None if filled > ZERO else (fill_failed_reason(fill) or REASON_REFUSED),
        "sold": filled > ZERO, "leg_settled": bool(leg_settled),
        "pos_liquidated": bool(pos_liquidated),
        # paper 形状兼容（调用点的下游代码/事件字段沿用同一套键名）
        "shares": str(filled if filled > ZERO else shares_before),
        "shares_before": str(shares_before), "remaining_shares": str(remaining),
        "filled_shares": str(filled), "unfilled_shares": str((shares_before - filled).quantize(QTY)
                                                             if shares_before > filled else ZERO),
        "bid": str(limit) if limit is not None else None,
        "avg_price": str(avg) if avg is not None else None,
        "gross_usdc": str(gross), "fee_usdc": str(fee), "proceeds_usdc": str(net),
        "net_usdc": str(net), "exit_fee_usdc": str(fee), "sell_floor": str(floor),
        "order_id": fill.get("order_id"), "status": fill.get("status"),
        "limit_price": str(limit) if limit is not None else None,
        "account": account, "ts_utc": stamp,
    }


# --------------------------------------------------------------------------- 通道（网络）

class LiveExitChannel:
    """live 退场通道：三闸门 + 真实客户端 + 真实 FAK 卖 + 卖后真实账户（薄封装 ``LivePort``）。

    刻意**不**复用 ``LivePort.preflight``：那是入场逻辑，会跑 ``risk_gate`` / ``check_limits``
    （持仓数 / 资金上限 / 预算），而卖出不得被入场型上限阻挡。三闸门由 ``get_channel`` 用
    ``submit.gate_status`` 现算，客户端由 ``port.ensure_client()`` 打开。
    """

    def __init__(self, port: Any, *, env: dict | None = None, gates: dict | None = None,
                 audit_path=None, poll_attempts: int = 6, poll_sleep: float = 1.0, sleep=None,
                 account_reader: Callable[[Any], dict] | None = None,
                 cancel_sweep: Callable[..., dict] | None = None) -> None:
        self.port = port
        self.env = env if env is not None else getattr(port, "env", {}) or {}
        self.gates = gates if gates is not None else getattr(port, "gates", None)
        self.audit_path = audit_path if audit_path is not None else getattr(port, "audit_path", None)
        self.poll_attempts = poll_attempts
        self.poll_sleep = poll_sleep
        self.sleep = sleep
        self.account_reader = account_reader
        self.cancel_sweep = cancel_sweep
        #: 本通道本轮的真实撤单记录（审计/测试：撤单必须**先于**卖单）
        self.cancels: list[dict] = []

    # -- 依赖转发（测试注入桩 port 时只需实现这几个口）---------------------------
    @property
    def transport(self) -> Any:
        return getattr(self.port, "transport", None)

    def ensure_client(self) -> tuple[Any, str]:
        fn = getattr(self.port, "ensure_client", None)
        if not callable(fn):
            return None, f"{REASON_NO_CLIENT}: port has no ensure_client()"
        return fn()

    def resolve_neg_risk(self, client, token_id, book) -> tuple[bool | None, str]:
        """签名域解析：**沿用既有实现**（``LivePort.resolve_neg_risk``，操作者规则 ⑫）。"""
        fn = getattr(self.port, "resolve_neg_risk", None)
        if not callable(fn):
            return None, "no_resolver"
        return fn(client, token_id, book)

    def account(self) -> dict | None:
        if self.account_reader is not None:
            try:
                return self.account_reader(self)
            except Exception:  # noqa: BLE001 — 审计用，绝不影响成交记账
                return None
        fn = getattr(self.port, "read_account_now", None)
        return fn() if callable(fn) else None

    def refetch_book(self, client, token_id: str) -> dict | None:
        """盘口缺失时用 live 客户端重取一次（只读；与 ``neg_risk`` 重取同源）。"""
        fetch = getattr(self.transport, "refetch_book", None)
        if not callable(fetch) or not token_id:
            return None
        try:
            book = fetch(client, str(token_id))
        except Exception:  # noqa: BLE001 — 取不到 ⇒ 当作无买盘（fail-closed 不卖）
            return None
        return book if isinstance(book, dict) else None

    # -- 真实撤单（规则 ⑦：发卖单前先撤该 session 的真实挂单）------------------------
    def cancel_open_orders(self, client, token_ids, *, audit_path=None) -> dict:
        """撤掉这些 token 上的**真实**挂单；返回 ``{ok, canceled, failed, count, detail}``。

        交易所上无法按"引擎 session"过滤，因此按**该仓全部腿的 token** 过滤（保守：宁可多撤，
        也绝不让一张旧挂单在卖单之后成交）。撤单失败 ⇒ ``ok=False``，调用方 fail-closed 弃卖这
        一轮（先撤后卖是硬顺序，绝不允许"卖在挂单后面"）。
        """
        path = audit_path if audit_path is not None else self.audit_path
        wanted = {str(t) for t in (token_ids or []) if t}
        out = {"ok": True, "canceled": [], "failed": [], "count": 0, "detail": ""}
        if not wanted:
            return {**out, "detail": "no live orders to cancel (no tokens)"}
        transport = self.transport
        list_open = getattr(transport, "list_open_orders", None)
        if not callable(list_open):
            return {**out, "ok": False, "detail": "transport has no list_open_orders()"}
        try:
            rows = (list_open(client) or {}).get("orders") or []
        except Exception as exc:  # noqa: BLE001 — 读不到挂单 ⇒ 不敢卖（可能卖在挂单后面）
            return {**out, "ok": False,
                    "detail": f"open-orders read failed ({type(exc).__name__}: {exc}) — defer"}
        targets = [str(r.get("id")) for r in rows
                   if str(r.get("asset_id") or "") in wanted and r.get("id")]
        out["count"] = len(targets)
        if self.cancel_sweep is not None:
            sweep = self.cancel_sweep(client, targets, audit_path=path)
            res = {**out, **sweep}
            self.cancels.append(res)
            return res
        # 撤单只经由 v2 通道的**唯一** call site（``v2_transport.cancel_with_retry`` →
        # ``client.cancel_orders([id])``）：本模块绝不自己碰交易所写口。
        cancel_with_retry = getattr(transport, "cancel_with_retry", None)
        if not callable(cancel_with_retry):
            out["ok"] = False
            out["detail"] = "transport has no cancel_with_retry() — no order sent"
            self.cancels.append(dict(out))
            return out
        for oid in targets:
            try:
                res = cancel_with_retry(client, oid, audit_path=path)
                ok = bool(res.get("ok"))
                if not ok:
                    out["detail"] = str(res.get("detail") or "cancel refused")
            except Exception as exc:  # noqa: BLE001 — 撤单失败 = 残单风险 ⇒ 弃卖本轮
                ok = False
                out["detail"] = f"{type(exc).__name__}: {exc}"
            (out["canceled"] if ok else out["failed"]).append(oid)
            if not ok:
                out["ok"] = False
        self.cancels.append(dict(out))
        if out["ok"]:
            out["detail"] = f"canceled {len(out['canceled'])} live order(s) before the SELL"
        return out

    # -- 真实卖（唯一下单口）----------------------------------------------------
    def sell_leg(self, *, leg: dict, book: Any, floor: Decimal, shares: Any, channel: str,
                 token_ids=None, leg_window: str | None = None, tick=None) -> dict:
        """一次 SELL FAK：撤真实挂单 → 解析签名域 → ``execute_leg(SELL, floor=...)``。

        任何一步不成立 ⇒ 返回**未成交**结果（``filled_shares=0``，带机器可读 ``status``），
        绝不重试、绝不改价、绝不无保护地卖。返回值与 ``execute_leg`` 同形 + ``cancel`` 摘要。
        """
        out = {"ok": False, "status": "not_started", "order_id": None,
               "filled_shares": ZERO, "avg_price": None, "cost": ZERO, "unfilled": _dec_or_zero(shares),
               "limit_price": None, "clamped": False, "order_mode": "taker", "side": SELL,
               "sell_floor": str(floor), "exit_channel": str(channel), "detail": "", "cancel": None}
        token = str(leg.get("token_id") or "")
        if not token:
            return {**out, "status": REASON_NO_TOKEN, "detail": "leg carries no token_id"}
        min_size = book_min_order_size(book)
        if min_size is not None and _dec_or_zero(shares) < min_size:
            # 防御纵深（与决策层同一条边界）：股数 < venue 最小量 ⇒ 交易所必拒，
            # 本地弃单、零签名、零发单（r104 实测：0.0041 股余量被 400 invalid maker amount 拒)
            return {**out, "status": REASON_BELOW_MIN,
                    "detail": (f"{_dec_or_zero(shares)} share(s) < venue min_order_size {min_size}"
                               " — refuse locally, nothing sent")}
        limit = best_bid_of(book)
        if limit is None:
            return {**out, "status": REASON_NO_BOOK, "detail": "no usable best_bid — no order sent"}
        # 极速滑点市价吃单 (IOC/FAK 快速逃生)：破位/夭折时以低于买一价 2 个 tick 挂 FAK 深度吃单
        if channel in (CH_BREACH_RC, CH_ABORTION_RC):
            limit = max(floor, limit - Decimal("0.02"))
        client, src = self.ensure_client()
        if client is None:
            return {**out, "status": REASON_NO_CLIENT, "detail": str(src)}
        neg_risk, neg_src = self.resolve_neg_risk(client, token, book)
        if neg_risk is None:
            return {**out, "status": REASON_NEG_RISK,
                    "detail": "neg-risk signing domain unknown; refusing to sign a mis-scoped sell",
                    "neg_risk_source": neg_src}
        cancel_res = self.cancel_open_orders(client, list(token_ids or [token]))
        if not cancel_res.get("ok"):
            # 撤单失败 ⇒ 绝不发卖单（先撤后卖是硬顺序；否则卖单可能排在旧挂单后面）
            return {**out, "status": "cant_cancel_first", "cancel": cancel_res,
                    "detail": f"pre-sell cancel failed: {cancel_res.get('detail')} — no order sent"}
        transport = self.transport
        result = transport.execute_leg(
            client, token_id=token, side=SELL, price=limit, size=shares, book=book,
            tick=tick if tick is not None else ((book or {}).get("tick_size")
                                                if isinstance(book, dict) else None),
            neg_risk=bool(neg_risk), gates=self.gates, post_only=False, clamp=False, taker=True,
            floor=floor, exit_channel=channel, leg_window=leg_window,
            neg_risk_source=neg_src, poll_attempts=self.poll_attempts,
            poll_sleep=self.poll_sleep, sleep=self.sleep, audit_path=self.audit_path)
        return {**result, "cancel": cancel_res, "side": SELL, "sell_floor": str(floor),
                "exit_channel": str(channel), "neg_risk_source": neg_src}


def get_channel(cfg: dict | None, *, env: dict | None = None, port: Any = None,
                transport: Any = None) -> LiveExitChannel:
    """取得 live 退场通道：**三闸门全过**才给（否则 ``ExitRefused('gate_*')``）。

    闸门与入场完全同一套（``YES2RE_LIVE_ENABLE_SUBMIT`` + ``LIVE_SUBMIT_ENABLED`` + 当日短语）；
    但这里**不**跑 ``risk_gate`` / ``check_limits``（入场型上限不得阻挡卖出）。
    """
    mode = _mode_of(cfg)
    if mode != "live":
        raise ExitRefused(REASON_NOT_LIVE, f"mode={mode!r} — the exit channel is live-only")
    cfg = cfg or {}
    if port is None:
        from .port import PortRefused, get_port  # 局部 import：paper 路径不引入 v2 依赖

        try:
            port = get_port(cfg, env=env, transport=transport)
        except PortRefused as exc:
            raise ExitRefused(f"{GATE_PREFIX}{exc.reason}", exc.detail) from None
    env = env if env is not None else getattr(port, "env", {}) or {}
    gates = getattr(port, "gates", None)
    if gates is None:
        gates = submit.gate_status(enable_submit=env.get("YES2RE_LIVE_ENABLE_SUBMIT") == "1",
                                   env=env, confirm=env.get("YES2RE_LIVE_CONFIRM"))
    if not submit.gates_all_passed(gates):
        reason = (gates or {}).get("reason") if isinstance(gates, dict) else REASON_GATES
        detail = (gates or {}).get("detail") if isinstance(gates, dict) else "no gate record"
        raise ExitRefused(f"{GATE_PREFIX}{reason or REASON_GATES}", str(detail))
    return LiveExitChannel(port, env=env, gates=gates,
                          audit_path=getattr(port, "audit_path", None))


# --------------------------------------------------------------------------- 编排（决策 + 网络）

def _emit(log: Callable[[dict], Any] | None, payload: dict) -> None:
    if log is None:
        return
    try:
        log(payload)
    except Exception:  # noqa: BLE001 — 事件日志失败绝不改变成交/账本事实
        pass


def live_exit_leg(*, cfg: dict, state: dict, leg: dict, channel: str,
                  pos: dict | None = None, books: dict | None = None,
                  shares_override: Decimal | None = None,
                  now_utc: datetime | None = None, leg_window: str | None = None,
                  attempts: int = 0, channel_obj: LiveExitChannel | None = None,
                  port: Any = None, env: dict | None = None, transport: Any = None,
                  log: Callable[[dict], Any] | None = None) -> dict[str, Any]:
    """live 腿退场的**完整**编排（一腿一次尝试）：决策 → 真实 SELL → 按真实成交记账 → 审计。

    返回统一结果字典（含 ``deferred`` / ``reason`` / ``sold`` / ``leg_settled`` /
    ``pos_liquidated`` / ``remaining_shares`` / ``proceeds_usdc`` / ``fee_usdc`` / ``order_id``）。
    fail-closed：任何未成交结果 ⇒ ``deferred=True``，仓位保持 open，记 ``exit_deferred`` 事件，
    并（``apply_live_exit_fill`` 之外）在该仓登记 ``pending_exit`` 供下一轮续卖。
    """
    now = now_utc or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    settings = exit_settings(cfg)
    leg = leg if isinstance(leg, dict) else {}
    token = str(leg.get("token_id") or "")
    shares_total = _dec_or_zero(leg.get("shares"))
    shares = _dec_or_zero(shares_override) if shares_override is not None else shares_total
    base = {"channel": str(channel), "token_id": token, "leg": leg.get("leg"),
            "position_key": (pos or {}).get("key"), "sold": False, "leg_settled": False,
            "pos_liquidated": False, "deferred": True, "action": ACTION_DEFER,
            "reason": None, "bid": None, "sell_floor": (str(settings["floor"])
                                                        if settings["floor"] is not None else None),
            "shares_before": str(shares), "filled_shares": "0", "remaining_shares": str(shares),
            "gross_usdc": "0", "fee_usdc": "0", "proceeds_usdc": "0", "order_id": None,
            "status": None, "ts_utc": stamp}

    def _defer(reason: str, **extra) -> dict[str, Any]:
        out = {**base, "reason": str(reason), **extra}
        _emit(log, {"type": "exit_deferred", "exit_channel": str(channel), "reason": str(reason),
                    "position_key": out.get("position_key"), "leg": leg.get("leg"),
                    "token_id": token, "shares": str(shares), "bid": out.get("bid"),
                    "sell_floor": out.get("sell_floor"), "detail": extra.get("detail"),
                    "ts_utc": stamp})
        return out

    # 0) 只有 live 才走真实卖；paper 由调用方走既有虚拟平仓（此处只是运行时兜底）
    if _mode_of(cfg) != "live":
        return _defer(REASON_NOT_LIVE, action=ACTION_PAPER)
    if leg.get("settled") or shares <= ZERO:
        return _defer(REASON_NO_SHARES)
    if not token:
        return _defer(REASON_NO_TOKEN)
    # 1) 三闸门 + 真实客户端（不过入场型上限）
    try:
        chan = channel_obj if channel_obj is not None else get_channel(
            cfg, env=env, port=port, transport=transport)
    except ExitRefused as exc:
        return _defer(exc.reason, detail=exc.detail)
    # 2) 盘口（缺失 ⇒ 用 live 客户端重取一次；仍无 ⇒ 无买盘 ⇒ 不卖）
    bid = best_bid_of((books or {}).get(token)) if books else None
    book = (books or {}).get(token) if books else None
    if not isinstance(book, dict) or bid is None:
        client_probe, _src = chan.ensure_client()
        if client_probe is not None:
            fresh = chan.refetch_book(client_probe, token)
            if isinstance(fresh, dict) and best_bid_of(fresh) is not None:
                book, bid = fresh, best_bid_of(fresh)
    # 3) 纯决策（尝试次数取"本轮已下单次数"与调用方给值的较大者 ⇒ 每轮上限是真的）
    attempts = max(int(attempts or 0), attempts_this_cycle(state, token))
    plan = plan_live_exit(mode="live", settings=settings, best_bid=bid, gates=chan.gates,
                          attempts=attempts, shares=shares,
                          min_order_size=book_min_order_size(book))
    if plan["action"] != ACTION_SELL:
        reason = plan["reason"]
        return _defer(reason, bid=str(bid) if bid is not None else None,
                      action=(ACTION_PAPER if reason == REASON_NOT_LIVE else ACTION_DEFER))
    # 4) 真实卖（含先撤单 + 签名域解析）；先把这次尝试记进本轮计数
    note_attempt(state, token, str(channel), now)
    win = leg_window if leg_window is not None else f"[{settings['floor']}, {bid}]"
    token_ids = [str(lg.get("token_id")) for lg in ((pos or {}).get("legs") or [])
                 if lg.get("token_id")]
    fill = chan.sell_leg(leg=leg, book=book, floor=settings["floor"], shares=shares,
                         channel=str(channel), token_ids=token_ids or [token], leg_window=win)
    account = chan.account() if _dec_or_zero(fill.get("filled_shares")) > ZERO else None
    res = apply_live_exit_fill(state=state, pos=pos, leg=leg, fill=fill, channel=str(channel),
                               floor=settings["floor"], fee_rate=settings["fee_rate"],
                               now_utc=now, account=account)
    missing = fill_failed_reason(fill)
    if missing is not None or not res["sold"]:
        # 一股都没成 ⇒ 延期（仓位保持 open；余量一分不动）
        reason = missing or res["reason"] or REASON_REFUSED
        deferred = _defer(reason, bid=res.get("bid"), status=fill.get("status"),
                          detail=fill.get("detail"))
        if pos is not None:
            pending = pos.setdefault("pending_exit", {})
            pending[token] = {"token_id": token, "leg": leg.get("leg"),
                              "bucket_id": leg.get("bucket_id"), "exit_channel": str(channel),
                              "shares": str(shares), "sell_floor": str(settings["floor"]),
                              "updated_at_utc": stamp, "last_reason": str(reason),
                              "last_status": fill.get("status")}
        deferred["status"] = fill.get("status")
        return deferred
    out = {**base, "action": ACTION_SELL, "deferred": False, "sold": True, "reason": None,
           "status": fill.get("status"), "bid": res.get("bid"), "avg_price": res.get("avg_price"),
           "filled_shares": res["filled_shares"], "remaining_shares": res["remaining_shares"],
           "leg_settled": res["leg_settled"], "pos_liquidated": res["pos_liquidated"],
           "gross_usdc": res["gross_usdc"], "fee_usdc": res["fee_usdc"],
           "proceeds_usdc": res["proceeds_usdc"], "net_usdc": res["net_usdc"],
           "order_id": res.get("order_id"), "limit_price": res.get("limit_price"),
           "cancel": fill.get("cancel"), "account": account,
           "sell_floor": res["sell_floor"], "leg_window": win}
    _emit(log, {"type": "live_exit", "exit_channel": str(channel), "side": SELL,
                "position_key": out.get("position_key"), "leg": leg.get("leg"),
                "token_id": token, "shares_before": res["shares_before"],
                "filled_shares": res["filled_shares"], "remaining_shares": res["remaining_shares"],
                "avg_price": res.get("avg_price"), "bid": out.get("bid"),
                "sell_floor": res["sell_floor"], "leg_window": win,
                "gross_usdc": res["gross_usdc"], "fee_usdc": res["fee_usdc"],
                "net_usdc": res["net_usdc"], "order_id": res.get("order_id"),
                "status": res.get("status"), "leg_settled": res["leg_settled"],
                "pos_liquidated": res["pos_liquidated"],
                "cancel_before_sell": (fill.get("cancel") or {}).get("count"),
                "account": account, "ts_utc": stamp})
    return out


def describe() -> dict[str, Any]:
    """通道自述（诊断/审计用）。"""
    return {"module": "live/exit.py", "order_side": SELL, "order_type": "FAK",
            "order_api": "market", "exit_channels": list(EXIT_CHANNELS),
            "defaults": {"live_sell_enabled": DEFAULT_LIVE_SELL_ENABLED,
                         "live_sell_floor": DEFAULT_LIVE_SELL_FLOOR,
                         "live_sell_max_attempts_per_cycle": DEFAULT_LIVE_SELL_MAX_ATTEMPTS_PER_CYCLE},
            "entry_type_caps_apply": False,
            "cancel_before_sell": True, "fail_closed": True,
            "paper_fallback": "caller keeps paper_capital.close_leg_at_best_bid (paper mode only)"}
