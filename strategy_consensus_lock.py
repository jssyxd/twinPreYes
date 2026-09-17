"""strategy_consensus_lock.py — 优化版"稳了"共识锁定策略与前置异动极速风控引擎 (V2 生产级)

基于实盘微观结构三大优化落地：
1. 【执行优化 - Capped Taker / 价格门控吃单】:
   - 摒弃 90% mid 被动挂单（避免好单 0 成交、成交必是接盘毒流的逆向选择陷阱）；
   - 在严格满足"站上极值+共识第一+下一档<20¢"高胜率形态下，采用带安全顶价的 Taker 吃单；
   - 价格区间约束在 [yes_min_ask=0.45, yes_max_ask=0.75] 之间，超 0.75 绝不追高，保住利润空间；
   - 同时保留 best_bid_peg 作为可选备用模式。
2. 【风控优化 - Pre-METAR 盘口异动先导抢跑止损】:
   - 不坐等新 METAR 落地（官方报文落地时二元期权买盘通常已蒸发归零）；
   - 先导监测下一档桶异动：若下一档（如32℃）YES 盘口暴涨突破 35¢，或当前持仓桶 YES 买盘跌破 45¢；
   - 立即在 METAR 到达前 30~60 秒执行【抢跑止损】，以尚存的盘口买价（0.45~0.55）果断割肉，保全 50%+ 本金！
3. 【频次与追火约束 - 单日单方向最多 1 次反手（上限 2 次）】:
   - 严格继承线上实战检验的 Session 计数器（fires_count <= 2）；
   - 反手时对破位桶 NO 做真实微观结构检查：NO 卖价 <= 0.85 才买，若 NO 已被扫空至 0.99/1.00 则自动跳过；
   - 杜绝连续跳桶引发的双重亏损（Double Wipeout）。

4. 【并行通道 - 下一档桶廉价入场 (buy_yes_next)，默认关闭】:
   - 实测依据：`preyes_param_sim_20260912.md` §5/§6 —— 既有目标桶通道的非价格门全通过时，
     目标桶 ask 已被市场定价到 0.81–0.99（被 cap 0.75 挡死，8 小时零成交）；而"下一档桶"
     报价 q<=0.30 的入口也被同一个 cap 封死 ⇒ 放宽任何价格阈值都换不来机会。
   - 因此**改变入场对象**：在"下一档桶"报价低廉时直接建仓该桶的 YES。
   - 非价格门与既有通道**逐字一致、一个都不放松**（站点频次 → 时间窗 → 速度/变率 →
     预期极值桶位 → 共识 rank1）；价格约束换成该通道**自有且独立**的窗口
     `[next_entry_min_ask, next_entry_max_ask]`（部署值 `[0.27, 0.40]`，**闭区间**：下界与上界
     都含等号，见 B 项语义补完；既有目标桶通道仍为半开 `(0.45, 0.81]`）。
   - 区间外 ⇒ **彻底弃单**（绝不降级、绝不挂被动单、绝不回退既有窗口）；与既有通道
     **预算隔离**（新通道用 `next_entry_budget_pct` × fire 预算，既有通道只用剩余额度）。
   - ⚠ EV 前提（"市场系统性低估该桶"）未经结算验证 ⇒ 代码默认 `next_entry_enabled=false`，
     部署由 config 显式开启，且必须保持小额、可一键关闭。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

from consensus_tracker import ConsensusTracker

ZERO = Decimal("0")
ONE = Decimal("1")

# 1. 经 57MB 真实日志严格检验的 <=30分钟 高频发布 METAR 城市白名单 (22个站点)
FAST_METAR_CITIES: set[str] = {
    "amsterdam",     # EHAM (30m)
    "ankara",        # LTAC (30m)
    "beijing",       # ZBAA (30m)
    "guangzhou",     # ZGGG (30m)
    "helsinki",      # EFHK (30m)
    "istanbul",      # LTFM (30m)
    "karachi",       # OPKC (30m)
    "kuala-lumpur",  # WMKK (30m)
    "london",        # EGLC (30m)
    "madrid",        # LEMD (30m)
    "milan",         # LIMC (30m)
    "moscow",        # UUWW (30m)
    "munich",        # EDDM (30m)
    "paris",         # LFPB (30m)
    "seoul-incheon", # RKSI (30m)
    "shanghai",      # ZSPD (30m)
    "singapore",     # WSSS (30m)
    "taipei",        # RCSS (20-30m)
    "tel-aviv",      # LLBG (30m)
    "tokyo",         # RJTT (30m)
    "warsaw",        # EPWA (30m)
    "wellington",    # NZWN (30m)
}

# 优化后生产配置
DEFAULT_CONFIG = {
    "filter_fast_stations_only": False,
    "high_local_start": 14,                           # 避开正午强对流与急剧升温期 (14:00~18:00)
    "high_local_end": 18,
    "low_local_start": 0,
    "low_local_end": 9,
    # 稳了门槛
    "next_bucket_max_twap": Decimal("0.26"),          # 下一档 1h TWAP 阈值 (< 0.26，捕获如新加坡等胜率盘)
    "next_bucket_twap_window_s": 3600,                 # 1小时窗口
    "next_bucket_max_instant_ask": Decimal("0.25"),    # 下一档瞬时 Ask 必须 < 0.25 (防快钱突袭)
    "next_bucket_max_instant_bid": Decimal("0.15"),    # 下一档瞬时 Bid 必须 < 0.15 (防多头潜伏)
    "min_dwell_seconds_if_rising": 1800,               # 升温跃升后必须在该温度停滞至少 30 分钟方可开仓
    # 执行模式: "capped_taker" (推荐) 或 "best_bid_peg"
    "entry_mode": "capped_taker",
    "yes_min_ask": Decimal("0.45"),                   # YES 必须确认一定胜率 (>0.45)
    "yes_max_ask": Decimal("0.75"),                   # YES 安全入场顶价，与 live 端口 taker 带门统一为 0.75，绝不追高 (>0.75 放弃)
    #: ⚠ 已废弃（DEPRECATED, F-A 2026-09-12）：旧 sizing 基数，**不再**是权威值。
    #: 唯一基数 = 生效 fire 预算（引擎注入的 ``fire_budget_usdc``，见 ``order_budget()``）；
    #: 本键只在 ``fire_budget_usdc`` 缺失时作为兼容回退（缺省 15.0），保留仅为不破坏旧调用方。
    "order_budget_usdc": Decimal("15.0"),              # 每次开仓 15 USDC（旧基数，已废弃）
    "mid_discount": Decimal("0.90"),                   # 仅在 peg 模式下使用的折价
    "resting_order_timeout_s": 180,                    # peg 模式下挂单最长 3 分钟
    # 前置风控 (抢跑止损)
    "early_stop_enabled": True,
    "early_stop_next_bucket_surge": Decimal("0.35"),  # 下一档暴涨至 0.35 触发抢跑止损
    "early_stop_bid_floor": Decimal("0.65"),          # 当前持仓 YES 盘口跌破 0.45 触发抢跑止损
    # ------------------------------------------------------------------- ① 止损门限按通道解耦
    # 目标桶通道逐字保持 0.45（上面的 early_stop_bid_floor）；next_bucket 通道的门限**不再**沿用
    # 0.45（该通道的可成交买价 ≤ 0.32，与 0.45 数学上不相容 ⇒ 每笔 next_bucket 开火 24~40s 内
    # 必被"虚拟平仓"，实测 4/4）。改为按**入场实际成交均价**相对止损，并以绝对地板兜底：
    #     触发线 = max(入场均价 × next_bucket_stop_loss_pct, next_bucket_bid_floor)
    # 例：0.32 入场 ⇒ max(0.16, 0.12) = 0.16（与"Bid 跌穿 0.16 才止损"一致）。
    "next_bucket_stop_loss_pct": Decimal("0.50"),      # 相对止损：触发线 = 入场均价 × pct
    "next_bucket_bid_floor": Decimal("0.12"),          # 绝对低位地板（防超低价入场时触发线过低）
    # ------------------------------------------------------------------- ② 开仓冷静期
    # 仅对**条件 B（bid_floor_broken）**生效：距 pos.entry_ts_utc 不足本秒数时**不执行**止损
    # （条件 A next_bucket_surge 不受影响）。被抑制的止损**必须落审计**（early_stop_suppressed）。
    "early_stop_grace_seconds": 1200,                  # 开仓冷静期（整数秒）
    # 破位与追火约束
    "max_fires_per_session": 2,                       # 单日同一标的最多 2 次 (初次 + 1次追火，止损后熔断阻断)
    "risk_control_no_cap": Decimal("0.85"),           # 破位 NO 腿超过 0.85 坚决不买 (防扫空)
    "risk_control_yes_cap": Decimal("0.75"),          # 新 YES 腿同样限顶价 0.75
    # ------------------------------------------------------------------ 并行通道
    # 下一档桶廉价入场 (buy_yes_next)：入场对象 = 当前预期极值桶的"上一档"桶，价格窗口自有独立。
    # 代码默认**关闭**（EV 前提未经结算验证）；本次部署由 config/yes2re_reversal.json 显式开启。
    "next_entry_enabled": False,
    "next_entry_min_ask": Decimal("0.20"),            # 窗口下界（半开：0.20 本身不含 ⇒ 弃单）
    "next_entry_max_ask": Decimal("0.32"),            # 窗口上界（半开：0.32 本身含 ⇒ 入场）
    "next_entry_budget_pct": Decimal("0.5"),          # 通道预算 = fire 预算 × 0.5（与既有通道隔离）
    # ------------------------------------------------------------------ live 真实 SELL 出场
    # live 模式下每处真实持仓退场都走**真实 FAK 卖**（live/exit.py），不得虚拟平仓（2026-09-13）。
    # **代码默认 False（fail-safe，2026-09-13 事故后定）**：真实 SELL 属新的链上动作，必须由 config
    # **显式开启**（与 next_entry_enabled 同模式）。事故背景：本键曾默认 True，第三方覆盖 config 删掉
    # 该键后回落成"开" ⇒ 重启即启用真实卖出。依赖"可被第三方删掉的键"来关闭资金动作不安全。
    # 置 False 时退场一律**延期**（fail-closed）由 settle 兜底，依旧不存在虚拟平仓。
    "live_sell_enabled": False,
    "live_sell_floor": Decimal("0.05"),               # 卖出地板：best_bid < floor ⇒ 不卖（== floor 卖）
    "live_sell_max_attempts_per_cycle": 1,            # 单轮对同一腿的最大卖出尝试次数
}

#: 通道标识（写进 fire/腿/审计，用来区分来源）
CHANNEL_TARGET = "target_bucket"     # 既有目标桶通道（yes_min_ask/yes_max_ask = 0.45/0.75）
CHANNEL_NEXT = "next_bucket"         # 新增"下一档桶廉价入场"通道（next_entry_min/max_ask）

#: 新通道腿名（端口 YES 腿判定为数据镜像，见 live/port.py::YES_LEG_NAMES）
NEXT_ENTRY_LEG = "buy_yes_next"


def _dec(val: Any, default: str = "0") -> Decimal:
    if val is None:
        return Decimal(default)
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal(default)


def _dec_or_none(val: Any) -> Decimal | None:
    """严格解析（不可解析/非有限 ⇒ ``None``）；窗口配置必须用它，绝不用默认值兜底。"""
    if val is None or isinstance(val, bool):
        return None
    try:
        out = Decimal(str(val).strip())
    except Exception:
        return None
    return out if out.is_finite() else None


def _dec_or_zero(val: Any) -> Decimal:
    out = _dec_or_none(val)
    return out if out is not None and out > ZERO else ZERO


def _fmt_dec(val: Decimal) -> str:
    """Decimal → 审计串里的紧凑十进制文本（``0.1600`` → ``0.16``；绝不用科学计数法）。

    仅用于人读的 reason 串（数值本身仍走 Decimal 比较）；`format(..., "f")` 保证 ``0.16``
    而不是 ``1.6E-1``。
    """
    try:
        return format(val.normalize(), "f")
    except Exception:  # noqa: BLE001 - 审计串绝不能抛异常
        return str(val)


def parse_next_entry_window(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """解析并行通道"下一档桶廉价入场"的**自有**价格窗口 ``[next_entry_min_ask, next_entry_max_ask]``。

    **闭区间**（B 项语义补完，2026-09-13，仅本通道）：下界**含**（ask == lo ⇒ 入场）、上界**含**
    （ask == hi ⇒ 入场）—— 用户口径为 ``0.27 <= ask <= 0.40``。既有目标桶通道
    ``(yes_min_ask, yes_max_ask]`` 保持**半开**不变（不要顺手改它的语义）。任何不可用配置
    （缺失/不可解析/负数/上界 > 1/``lo >= hi``）一律返回 ``ok=False`` ⇒ 调用方**整通道弃单**，
    绝不回退既有窗口、绝不降级挂单。纯函数：只读 ``cfg``。
    """
    src = cfg if isinstance(cfg, dict) else {}
    lo_raw = src.get("next_entry_min_ask", DEFAULT_CONFIG["next_entry_min_ask"])
    hi_raw = src.get("next_entry_max_ask", DEFAULT_CONFIG["next_entry_max_ask"])
    lo = _dec_or_none(lo_raw)
    hi = _dec_or_none(hi_raw)
    if lo is None or hi is None or lo < ZERO or hi > ONE or lo >= hi:
        return {"ok": False, "lo": None, "hi": None, "label": None,
                "detail": (f"next-entry window unusable (next_entry_min_ask={lo_raw!r}, "
                           f"next_entry_max_ask={hi_raw!r}) — 整通道弃单（不回退既有窗口）")}
    return {"ok": True, "lo": lo, "hi": hi, "label": f"[{lo}, {hi}]",
            "detail": (f"[{lo}, {hi}] (closed: {lo} and {hi} both included, lower bound "
                       f"inclusive — 闭区间下界含等号)")}


def bucket_contains(bucket: dict[str, Any], val: float) -> bool:
    lo = bucket.get("lo")
    hi = bucket.get("hi")
    return (lo is None or val >= float(lo)) and (hi is None or val < float(hi))


def mid_value(bucket: dict[str, Any]) -> float:
    lo = bucket.get("lo")
    hi = bucket.get("hi")
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2.0
    if lo is not None:
        return float(lo)
    if hi is not None:
        return float(hi)
    return 0.0


def ordered_buckets(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(buckets, key=mid_value)


def find_bucket(buckets: list[dict[str, Any]], val: float) -> dict[str, Any] | None:
    for b in buckets:
        if bucket_contains(b, val):
            return b
    return None


def get_book_mid(book: Any) -> Decimal | None:
    if not book:
        return None
    ask_raw = book.get("best_ask") if isinstance(book, dict) else getattr(book, "best_ask", None)
    bid_raw = book.get("best_bid") if isinstance(book, dict) else getattr(book, "best_bid", None)
    if ask_raw is None or bid_raw is None:
        return None
    try:
        a = Decimal(str(ask_raw))
        b = Decimal(str(bid_raw))
        if a <= ZERO or b <= ZERO or b > a:
            return None
        return (a + b) / Decimal("2")
    except Exception:
        return None


def get_best_bid(book: Any) -> Decimal | None:
    if not book:
        return None
    raw = book.get("best_bid") if isinstance(book, dict) else getattr(book, "best_bid", None)
    if raw is None:
        return None
    try:
        v = Decimal(str(raw))
        return v if v > ZERO else None
    except Exception:
        return None


def get_best_ask(book: Any) -> Decimal | None:
    if not book:
        return None
    raw = book.get("best_ask") if isinstance(book, dict) else getattr(book, "best_ask", None)
    if raw is None:
        return None
    try:
        v = Decimal(str(raw))
        return v if v > ZERO else None
    except Exception:
        return None


@dataclass
class RestingOrder:
    order_id: str
    session_key: str
    city_id: str
    bucket_id: str
    token_id: str
    side: str
    limit_price: Decimal
    shares: Decimal
    cost_usdc: Decimal
    created_at_epoch: float
    timeout_seconds: float = 180.0
    status: str = "OPEN"   # OPEN, FILLED, CANCELLED_TIMEOUT, CANCELLED_BREACH

    def is_expired(self, now_epoch: float) -> bool:
        return self.status == "OPEN" and (now_epoch - self.created_at_epoch) >= self.timeout_seconds


@dataclass
class PositionRecord:
    session_key: str
    bucket_id: str
    yes_token_id: str
    shares: Decimal
    cost_usdc: Decimal
    avg_price: Decimal
    entry_ts_utc: str
    liquidated: bool = False
    liquidation_type: str = ""   # EARLY_STOP_SURGE, EARLY_STOP_BID_FLOOR, METAR_BREACH
    recovered_usdc: Decimal = ZERO
    realized_pnl_usdc: Decimal = ZERO
    entry_channel: str = ""


@dataclass
class ConsensusLockState:
    active_orders: dict[str, RestingOrder] = field(default_factory=dict)
    open_positions: dict[str, PositionRecord] = field(default_factory=dict)
    locked_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_fires_count: dict[str, int] = field(default_factory=dict)  # session_key -> int
    breached_sessions: set[str] = field(default_factory=set)
    stopped_out_sessions: set[str] = field(default_factory=set)        # 触发提前止损熔断标的
    #: 预算隔离（加法式）：新通道在每个会话已占用的名义额度（USDC）。
    #: 既有目标桶通道只用 ``fire 预算 − 这里的占用`` ⇒ 两通道永不重复花同一笔钱。
    session_next_entry_used: dict[str, Decimal] = field(default_factory=dict)
    #: ② 开仓冷静期去重（2026-09-13）：``session_key -> 上次已审计的抑制签名``。
    #: 被抑制的止损必须落审计，但**同一仓位/同一轮只记一次** ⇒ 用 (entry_ts, reason) 签名去重，
    #: 避免每个 tick 刷屏。
    early_stop_suppressed: dict[str, str] = field(default_factory=dict)


class ConsensusLockStrategy:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = {**DEFAULT_CONFIG, **(cfg or {})}
        self.state = ConsensusLockState()
        self._metar_history: dict[str, list[dict[str, Any]]] = {}      # session_key -> [{"temp": float, "obs_time": float, "recorded_at": float}]
        #: 上一次"下一档桶通道"为何没下单（审计用；`next_entry_disabled` ⇒ None）
        self.last_next_entry_skip: str | None = None

    def order_budget(self) -> Decimal:
        """**唯一 sizing 基数** = 生效 fire 预算（F-A，2026-09-12）。

        解析优先级（纯函数：只读 ``self.cfg``，不可解析/缺失一律回退，绝不抛异常）：

        1. ``cfg["fire_budget_usdc"]`` —— 引擎（``_r_cycle._get_consensus_lock_strat``）注入的
           **生效** fire 预算：已含 ``YES2RE_FIRE_BUDGET_USDC`` env 覆盖，且与 ``_paper_fire``
           实际使用的 ``cfg.get("fire_budget_usdc", DEFAULTS["fire_budget_usdc"])`` 同源。
        2. ``cfg["order_budget_usdc"]`` —— **已废弃** 旧键，仅作兼容回退。
        3. ``"15.0"`` —— 旧默认值（同样已废弃）。

        为什么必须统一（审计 F-A，MEDIUM）：新通道把"已占用额度"记成
        ``基数 × next_entry_budget_pct``，引擎按 ``fire_budget_usdc × pct`` 实际发单；
        基数不等时 (i) fire < 旧基数 ⇒ 既有通道被静默少给 ``(旧基数 − fire) × pct``；
        (ii) fire > 旧基数 ⇒ ``新通道 + 既有通道`` 合计可超过 fire 预算
        （实测 fire=30/order=15 时 15.0 + 22.5 = 37.5 > 30，SPEC 第 5 项不变量被打破）。
        统一到同一基数后 ``新通道预算 + 既有通道剩余 == fire`` 逐字成立。
        """
        fire = _dec_or_none(self.cfg.get("fire_budget_usdc"))
        if fire is not None:
            return fire
        return _dec(self.cfg.get("order_budget_usdc"), "15.0")

    def is_fast_station(self, city_id: str) -> bool:
        if not self.cfg.get("filter_fast_stations_only", True):
            return True
        return city_id.lower() in FAST_METAR_CITIES

    def is_in_time_window(self, dt_utc: datetime, tz_name: str, direction: str) -> tuple[bool, int]:
        try:
            loc_dt = dt_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            loc_dt = dt_utc
        hour = loc_dt.hour
        dir_norm = direction.lower()
        loc_time = hour + loc_dt.minute / 60.0
        if dir_norm == "high":
            st = float(self.cfg.get("high_local_start", 12.5))
            ed = float(self.cfg.get("high_local_end", 18))
            return (st <= loc_time < ed), hour
        else:
            st = float(self.cfg.get("low_local_start", 0))
            ed = float(self.cfg.get("low_local_end", 9))
            return (st <= loc_time < ed), hour

    def check_consensus_and_next_bucket(
        self,
        city_id: str,
        market_local_date: str,
        direction: str,
        ordered_bks: list[dict[str, Any]],
        target_bk: dict[str, Any],
        tracker: ConsensusTracker,
        now_utc: datetime,
        books_by_token: dict[str, Any] | None = None,
        price_gates: bool = True,
    ) -> tuple[bool, str, dict[str, Any]]:
        """检查 target_bk 是否为共识第一 (Rank-1) 且下一档可能破位的桶稳定 < 26¢ 且瞬时盘口未异动。

        ``price_gates``（加法式开关，默认 ``True`` ⇒ 既有目标桶通道逐字不变）：
        为 ``False`` 时**只**做非价格判定（rank1 + 下一档桶定位），把下一档的价格子门
        （twap / instant ask / instant bid）留给调用方自己的价格窗口 —— 这正是并行通道
        "下一档桶廉价入场" 的用法（其自有窗口 ``(next_entry_min_ask, next_entry_max_ask]``
        取代这三个子门）。子门读数仍完整写入 ``meta`` 作为证据，只是不再拦截。
        """
        meta: dict[str, Any] = {}
        ranked = tracker.rank_buckets(
            city_id, market_local_date, direction,
            now_utc=now_utc, window_seconds=self.cfg["next_bucket_twap_window_s"],
        )
        if not ranked:
            return False, "no_consensus_samples", meta

        rank1_id, rank1_mid, rank1_lead = ranked[0]
        meta["rank1_bucket_id"] = rank1_id
        meta["rank1_mid"] = str(rank1_mid)
        target_id = str(target_bk.get("bucket_id") or target_bk.get("id") or "")
        if str(rank1_id) != target_id:
            return False, f"target_is_not_rank1 (target={target_id}, rank1={rank1_id})", meta

        # 寻找下一档桶 (HIGH 为更高温度桶，LOW 为更低温度桶)
        t_idx = None
        for i, b in enumerate(ordered_bks):
            bid = str(b.get("bucket_id") or b.get("id") or "")
            if bid == target_id:
                t_idx = i
                break
        if t_idx is None:
            return False, "target_bucket_not_in_ladder", meta

        next_idx = t_idx + 1 if direction.lower() == "high" else t_idx - 1
        if 0 <= next_idx < len(ordered_bks):
            next_bk = ordered_bks[next_idx]
            next_id = str(next_bk.get("bucket_id") or next_bk.get("id") or "")
            next_series = tracker._series.get(f"{city_id}|{market_local_date}|{direction}", {}).get(next_id)
            next_twap = next_series.twap_mid(now_utc, self.cfg["next_bucket_twap_window_s"]) if next_series else None
            meta["next_bucket_id"] = next_id
            meta["next_bucket_twap"] = str(next_twap) if next_twap is not None else "none"

            # 针对清晨 Low 最低温市场放宽 TWAP 门槛（夜间点差偏大），High 仍严格保持 0.26
            if direction.lower() == "low":
                max_allowed = _dec(self.cfg.get("low_next_bucket_max_twap", "0.35"), "0.35")
            else:
                max_allowed = _dec(self.cfg.get("next_bucket_max_twap", "0.26"), "0.26")
            # 价格子门：``price_gates=False``（并行通道）时只留读数作为证据，不拦截。
            if price_gates and next_twap is not None and next_twap >= max_allowed:
                return False, f"next_bucket_twap_too_high ({next_twap} >= {max_allowed})", meta

            # 防线 3: 下一档瞬时盘口校验 (弥补 1h TWAP 滞后性)
            next_yes_tok = next_bk.get("yes_token_id") or next_bk.get("_yes_token_id")
            if next_yes_tok and books_by_token:
                next_book = books_by_token.get(str(next_yes_tok))
                if next_book:
                    next_ask = get_best_ask(next_book)
                    next_bid = get_best_bid(next_book)
                    meta["next_bucket_instant_ask"] = str(next_ask) if next_ask is not None else "none"
                    meta["next_bucket_instant_bid"] = str(next_bid) if next_bid is not None else "none"

                    max_instant_ask = _dec(self.cfg.get("next_bucket_max_instant_ask", "0.25"), "0.25")
                    if price_gates and next_ask is not None and next_ask >= max_instant_ask:
                        return False, f"next_bucket_instant_ask_too_high ({next_ask} >= {max_instant_ask})", meta

                    max_instant_bid = _dec(self.cfg.get("next_bucket_max_instant_bid", "0.15"), "0.15")
                    if price_gates and next_bid is not None and next_bid >= max_instant_bid:
                        return False, f"next_bucket_instant_bid_too_high ({next_bid} >= {max_instant_bid})", meta
        else:
            meta["next_bucket_id"] = "boundary_terminal"
            meta["next_bucket_twap"] = "0"

        return True, "ok", meta

    # ------------------------------------------------------- 并行通道: 下一档桶廉价入场
    def next_entry_window(self) -> dict[str, Any]:
        """本通道**自有**价格窗口 ``[next_entry_min_ask, next_entry_max_ask]``（**闭区间**）。"""
        return parse_next_entry_window(self.cfg)

    def next_entry_window_label(self) -> str | None:
        """窗口的可读标签（审计用；配置不可用时 ``None``）。"""
        return self.next_entry_window().get("label")

    def target_channel_budget(self, session_key: str, fire_budget: Decimal | str) -> Decimal:
        """预算隔离：既有目标桶通道在某会话可用的额度 = fire 预算 − 新通道已占用。

        新通道每次 fire 把 ``next_entry_budget_pct × fire 预算`` 记进
        ``state.session_next_entry_used``；这里把同一笔钱从既有通道的额度里扣掉，
        两个通道**永不重复花同一份预算**（默认无新通道 fire ⇒ 返回值 == fire 预算，
        与改动前逐字一致）。
        """
        used = self.state.session_next_entry_used.get(session_key) or ZERO
        left = _dec(fire_budget, "0") - used
        return left if left > ZERO else ZERO

    def evaluate_next_bucket_entry(
        self,
        city: dict[str, Any],
        market_local_date: str,
        direction: str,
        ordered_bks: list[dict[str, Any]],
        target_bk: dict[str, Any],
        tracker: ConsensusTracker,
        now_utc: datetime,
        books_by_token: dict[str, Any] | None,
        cur_fires: int,
        expected_extreme_temp: float | None,
        metar_obs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """并行通道：**下一档桶廉价入场**（``next_entry_enabled``，代码默认关闭）。

        调用约定（由 ``evaluate_entry`` 在 1–5 门之后、共识价格子门之前调用）：
        非价格门（站点频次 / 会话计数 / 时间窗 / METAR / 速度-变率 / 预期极值桶位）**已逐字
        通过**；共识这里只要 **rank1**（``price_gates=False``）。入场对象 = 下一档桶
        （``new_bucket_id`` 对应桶）的 YES token；价格约束 = 本通道**自有且独立**的窗口。

        返回 fire dict（``action == "execute_taker_fire"``, ``entry_channel == "next_bucket"``）
        或 skip dict（原因写明，调用方继续既有通道）。区间外/无盘口/窗口非法一律
        **彻底弃单**：不挂被动单、不降级、不回退既有窗口。风控（三重闸门、risk_gate、
        check_limits、tick 对齐、min_order_size）仍由 live 端口按腿独立执行，此处不涉及。
        """
        city_id = city["city_id"]
        key = f"{city_id}|{market_local_date}|{direction}"
        if not self.cfg.get("next_entry_enabled", False):
            return {"action": "skip", "reason": "next_entry_disabled", "key": key,
                    "entry_channel": CHANNEL_NEXT}
        win = self.next_entry_window()
        if not win["ok"]:
            return {"action": "skip", "reason": f"next_entry_window_invalid ({win['detail']})",
                    "key": key, "entry_channel": CHANNEL_NEXT}

        # 1. 15:00 之后若气温已掉头降温，严禁开突破单 (Cooling Filter)
        if direction.lower() == "high":
            try:
                from zoneinfo import ZoneInfo
                tz_name = city.get("timezone") or "UTC"
                loc_dt = now_utc.astimezone(ZoneInfo(tz_name))
                loc_hr = loc_dt.hour
            except Exception:
                loc_hr = now_utc.hour
            
            # 若当地时间 >= 15:00 且气温已回落低于日内极值，坚决一票否决
            if loc_hr >= 15 and metar_obs and expected_extreme_temp is not None:
                curr_t = float(metar_obs.get("temp") or 0)
                # running_extreme
                city_running = tracker.running_extremes.get(city_id, {}).get("high")
                if city_running is not None and curr_t < float(city_running):
                    return {"action": "skip", "reason": f"cooling_filter_after_15h (hr={loc_hr}>=15, temp={curr_t}<peak={city_running})",
                            "key": key, "meta": None, "entry_channel": CHANNEL_NEXT}

            cutoff_cfg = self.cfg.get("next_entry_high_cutoff_hour")
            if cutoff_cfg is not None:
                cutoff_hour = int(cutoff_cfg)
                try:
                    from zoneinfo import ZoneInfo
                    tz_name = city.get("timezone") or "UTC"
                    loc_dt = now_utc.astimezone(ZoneInfo(tz_name))
                    loc_hr = loc_dt.hour
                except Exception:
                    loc_hr = now_utc.hour
                if loc_hr >= cutoff_hour:
                    return {"action": "skip", "reason": f"next_entry_after_solar_cutoff (hr={loc_hr} >= {cutoff_hour})",
                            "key": key, "meta": None, "entry_channel": CHANNEL_NEXT}

        # 2. METAR 遇 CB/TS/RA 一票否决 (Weather Veto)
        if direction.lower() == "high" and metar_obs:
            raw_ob = str(metar_obs.get("rawOb") or metar_obs.get("raw_text") or metar_obs.get("raw") or "").upper()
            wx_str = str(metar_obs.get("wxString") or metar_obs.get("weather") or "").upper()
            clouds = str(metar_obs.get("clouds") or "").upper()
            has_convective = any(kw in raw_ob for kw in ("CB", "TS", "RA")) or                              any(kw in wx_str for kw in ("CB", "TS", "RA")) or                              ("CB" in clouds)
            if has_convective:
                return {"action": "skip", "reason": f"weather_veto_convective (CB/TS/RA in METAR: {raw_ob[:30]})",
                        "key": key, "meta": None, "entry_channel": CHANNEL_NEXT}

        # 共识：只要 rank1（下一档的价格子门由本通道自有窗口取代；读数仍写进 meta 作证据）
        ok_cons, reason_cons, meta_cons = self.check_consensus_and_next_bucket(
            city_id, market_local_date, direction, ordered_bks, target_bk, tracker,
            now_utc, books_by_token, price_gates=False,
        )
        if not ok_cons:
            return {"action": "skip", "reason": f"next_entry_consensus_failed: {reason_cons}",
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}

        next_id = str(meta_cons.get("next_bucket_id") or "")
        if not next_id or next_id == "boundary_terminal":
            return {"action": "skip", "reason": "next_entry_no_next_bucket", "key": key,
                    "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        next_bk = next(
            (b for b in ordered_bks
             if str(b.get("bucket_id") or b.get("id") or "") == next_id),
            None,
        )
        if not next_bk:
            return {"action": "skip", "reason": f"next_entry_bucket_not_in_ladder ({next_id})",
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}

        tok_next = next_bk.get("yes_token_id") or next_bk.get("_yes_token_id")
        if not tok_next:
            return {"action": "skip", "reason": "next_entry_no_yes_token_for_next_bucket",
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        next_book = (books_by_token or {}).get(str(tok_next))
        if not next_book:
            return {"action": "skip", "reason": "next_entry_book_missing",
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        next_ask = get_best_ask(next_book)
        if next_ask is None:
            return {"action": "skip", "reason": "next_entry_no_active_ask",
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        # 闭区间（B 项，2026-09-13）：下界**含**等号（ask == 0.27 ⇒ 入场）、上界含。区间外 ⇒
        # 彻底弃单（绝不降级/绝不挂被动单）。仅本通道为闭区间；既有目标桶通道保持半开。
        if next_ask < win["lo"]:
            return {"action": "skip",
                    "reason": (f"next_entry_ask_below_window ({next_ask} < {win['lo']}; "
                               f"window {win['label']})"),
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        if next_ask > win["hi"]:
            return {"action": "skip",
                    "reason": (f"next_entry_ask_above_window ({next_ask} > {win['hi']}; "
                               f"window {win['label']})"),
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}

        # 4. 要求 Bid >= 0.18 且点差 <= 0.12 (Order Book Quality Filter, 配置指定时生效)
        min_bid_cfg = self.cfg.get("next_entry_min_bid")
        if min_bid_cfg is not None:
            min_bid = Decimal(str(min_bid_cfg))
            next_bid = get_best_bid(next_book) or ZERO
            if next_bid < min_bid:
                return {"action": "skip",
                        "reason": f"next_entry_bid_too_low ({next_bid} < {min_bid})",
                        "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        max_spread_cfg = self.cfg.get("next_entry_max_spread")
        if max_spread_cfg is not None:
            max_spread = Decimal(str(max_spread_cfg))
            next_bid = get_best_bid(next_book) or ZERO
            spread = next_ask - next_bid
            if spread > max_spread:
                return {"action": "skip",
                        "reason": f"next_entry_spread_too_wide ({spread} > {max_spread})",
                        "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}

        pct_raw = self.cfg.get("next_entry_budget_pct", DEFAULT_CONFIG["next_entry_budget_pct"])
        pct = _dec_or_none(pct_raw)
        if pct is None or pct <= ZERO or pct > ONE:
            return {"action": "skip",
                    "reason": f"next_entry_budget_pct_invalid ({pct_raw!r}; need 0 < pct <= 1)",
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        # F-A（2026-09-12）：sizing 基数改用**唯一基数解析器**（生效 fire 预算）。数学逐字保留：
        # 仍是 ``基数 × pct`` 并按 0.01 取整；只是基数不再可能与引擎发的钱不一致。
        budget = (self.order_budget() * pct).quantize(Decimal("0.01"))
        shares = (budget / next_ask).to_integral_value(rounding=ROUND_DOWN)
        if shares <= ZERO:
            return {"action": "skip", "reason": "next_entry_zero_shares_calculated",
                    "key": key, "meta": meta_cons, "entry_channel": CHANNEL_NEXT}
        cost = next_ask * shares

        # 与既有 capped_taker 分支同构的账务：记仓位、锁会话、推进计数（沿用既有语义）
        pos = PositionRecord(
            session_key=key,
            bucket_id=next_id,
            yes_token_id=str(tok_next),
            shares=shares,
            cost_usdc=cost,
            avg_price=next_ask,
            entry_ts_utc=now_utc.isoformat(),
            liquidated=False,
        )
        self.state.open_positions[key] = pos
        self.state.locked_sessions[key] = {
            "locked_at": now_utc.isoformat(),
            "target_bucket_id": next_id,
            "expected_temp": expected_extreme_temp,
            "consensus_meta": meta_cons,
            "entry_channel": CHANNEL_NEXT,
            "entry_window": win["label"],
        }
        self.state.session_fires_count[key] = cur_fires + 1
        # 预算隔离簿记：本会话新通道占用的名义额度（既有通道只剩 fire 预算 − 这笔）
        self.state.session_next_entry_used[key] = budget

        return {
            "action": "execute_taker_fire",
            "key": key,
            "entry_channel": CHANNEL_NEXT,
            "entry_mode": "capped_taker",
            "fire_no": self.state.session_fires_count[key],
            "bucket_id": next_id,
            "token_id": str(tok_next),
            "side": "BUY",
            "outcome": "YES",
            "fill_price": str(next_ask),
            "shares": str(shares),
            "cost_usdc": str(cost),
            "cap": str(win["hi"]),
            "floor": str(win["lo"]),
            "window": win["label"],
            "next_entry_window": win["label"],
            "budget_pct": str(pct),
            "budget_usdc": str(budget),
            "bucket_lo": next_bk.get("lo"),
            "bucket_hi": next_bk.get("hi"),
            "bucket_label": next_bk.get("label") or next_bk.get("bucket_label"),
            "consensus_meta": meta_cons,
            "detail": f"next_bucket_cheap_entry (ask {next_ask} in {win['label']})",
        }

    def record_metar_observation(
        self,
        session_key: str,
        curr_temp: float,
        metar_obs: dict[str, Any] | None,
        now_utc: datetime,
    ) -> list[dict[str, Any]]:
        """记录 METAR 历史观测序列 (供变率停滞检查及高温夭折证伪复用)."""
        obs_age = float((metar_obs or {}).get("obs_age_s") or 0.0)
        obs_time = now_utc.timestamp() - obs_age
        hist = self._metar_history.setdefault(session_key, [])
        now_ts = now_utc.timestamp()
        if not hist or abs(obs_time - hist[-1].get("obs_time", 0.0)) >= 300 or hist[-1].get("temp") != curr_temp:
            hist.append({"temp": curr_temp, "obs_time": obs_time, "recorded_at": now_ts})
            if len(hist) > 20:
                hist.pop(0)
        return hist

    def evaluate_entry(
        self,
        city: dict[str, Any],
        market_local_date: str,
        direction: str,
        buckets: list[dict[str, Any]],
        expected_extreme_temp: float | None,
        metar_obs: dict[str, Any] | None,
        books_by_token: dict[str, Any],
        tracker: ConsensusTracker,
        now_utc: datetime,
    ) -> dict[str, Any]:
        """评估是否触发 '稳了' 入场 (优化版：默认 Capped Taker，避免挂单逆向选择)。"""
        city_id = city["city_id"]
        key = f"{city_id}|{market_local_date}|{direction}"
        # 并行通道审计字段按 tick 重置：只有本 tick 真的评估过新通道才会有原因（additive）
        self.last_next_entry_skip = None

        # 1. 站点频次筛选 (只做 <=30min 站点)
        if not self.is_fast_station(city_id):
            return {"action": "skip", "reason": "infrequent_metar_station", "key": key}

        # 2. 检查单日追火/开仓次数上限与熔断
        if key in self.state.stopped_out_sessions:
            return {"action": "skip", "reason": "session_already_stopped_out", "key": key}
        if key in self.state.breached_sessions:
            return {"action": "skip", "reason": "session_already_breached", "key": key}
        if key in self.state.open_positions and not self.state.open_positions[key].liquidated:
            return {"action": "skip", "reason": "session_already_has_open_position", "key": key}

        cur_fires = self.state.session_fires_count.get(key, 0)
        max_fires = int(self.cfg.get("max_fires_per_session", 2))
        if cur_fires >= max_fires:
            return {"action": "skip", "reason": f"session_max_fires_reached ({cur_fires}>={max_fires})", "key": key}

        # 3. 时间窗口检查
        in_win, loc_hr = self.is_in_time_window(now_utc, city.get("timezone", "UTC"), direction)
        if not in_win:
            return {"action": "skip", "reason": f"outside_time_window (hr={loc_hr})", "key": key}

        # 4. METAR 观测与预计极值
        if not metar_obs or metar_obs.get("temp_c") is None:
            return {"action": "skip", "reason": "no_valid_metar_temp", "key": key}
        curr_temp = float(metar_obs["temp_c"])
        market_unit = (city.get("market_unit") or city.get("unit") or metar_obs.get("market_unit") or "C").upper()
        if market_unit == "F":
            # Polymarket official NWS rounding for Fahrenheit markets
            curr_temp = float(round(curr_temp * 9.0 / 5.0 + 32.0))

        if expected_extreme_temp is None:
            return {"action": "skip", "reason": "no_expected_extreme_reference", "key": key}

        # 防线 2: 气温变率停滞检查 (dT/dt <= 0 & Dwell Time 确认，防冲顶接飞刀)
        hist = self.record_metar_observation(key, curr_temp, metar_obs, now_utc)
        now_ts = now_utc.timestamp()

        dir_norm = direction.lower()
        min_dwell_s = float(self.cfg.get("min_dwell_seconds_if_rising", 1800))
        min_dwell_low_s = float(self.cfg.get("low_min_dwell_seconds", 900))
        if len(hist) >= 2:
            prev_temp = hist[-2]["temp"]
            dwell_s = now_ts - hist[-1]["recorded_at"]
            if dir_norm == "high":
                if curr_temp > prev_temp and dwell_s < min_dwell_s:
                    return {
                        "action": "skip",
                        "reason": f"temperature_rising_velocity_active (jumped {prev_temp}C -> {curr_temp}C, dwell {int(dwell_s)}s < {int(min_dwell_s)}s)",
                        "key": key,
                    }
            else:
                if curr_temp < prev_temp and dwell_s < min_dwell_low_s:
                    return {
                        "action": "skip",
                        "reason": f"temperature_falling_velocity_active (dropped {prev_temp}C -> {curr_temp}C, dwell {int(dwell_s)}s < {int(min_dwell_low_s)}s)",
                        "key": key,
                    }

        # 5. 是否站上预计极值桶
        curr_bucket = find_bucket(buckets, curr_temp)
        target_bucket = find_bucket(buckets, float(expected_extreme_temp))
        if not curr_bucket or not target_bucket:
            return {"action": "skip", "reason": "bucket_not_found", "key": key}

        t_id = str(target_bucket.get("bucket_id") or target_bucket.get("id") or "")

        ordered = ordered_buckets(buckets)
        c_idx = ordered.index(curr_bucket)
        t_idx = ordered.index(target_bucket)

        if dir_norm == "high":
            if c_idx < t_idx:
                return {"action": "skip", "reason": f"not_reached_expected_high ({curr_temp} < expected {expected_extreme_temp})", "key": key}
            elif c_idx > t_idx:
                return {"action": "skip", "reason": f"already_exceeded_expected_high ({curr_temp} > expected {expected_extreme_temp})", "key": key}
        else:
            if c_idx > t_idx:
                return {"action": "skip", "reason": f"not_reached_expected_low ({curr_temp} > expected {expected_extreme_temp})", "key": key}
            elif c_idx < t_idx:
                return {"action": "skip", "reason": f"already_exceeded_expected_low ({curr_temp} < expected {expected_extreme_temp})", "key": key}

        # 5b. 并行通道：下一档桶廉价入场（`next_entry_enabled` 默认 False ⇒ 本块是 no-op，
        #     既有目标桶路径逐行不变）。非价格门由上面 1–5 门逐字保证；本通道只换入场对象
        #     （下一档桶 YES）与其**自有**价格窗口。命中 ⇒ 直接返回（既有通道本次不下单）；
        #     未命中/区间外 ⇒ 只记下原因，继续走下方既有逻辑（绝不降级、绝不挂被动单）。
        next_res = self.evaluate_next_bucket_entry(
            city, market_local_date, direction, ordered, target_bucket, tracker,
            now_utc, books_by_token, cur_fires, expected_extreme_temp,
            metar_obs=metar_obs,
        )
        self.last_next_entry_skip = (None if next_res.get("reason") == "next_entry_disabled"
                                     else next_res.get("reason"))
        if next_res.get("action") == "execute_taker_fire":
            return next_res

        # 6. 检查共识第一 + 下一档桶价格与瞬时盘口 (防线 3: 瞬时盘口校验)
        ok_cons, reason_cons, meta_cons = self.check_consensus_and_next_bucket(
            city_id, market_local_date, direction, ordered, target_bucket, tracker, now_utc, books_by_token
        )
        if not ok_cons:
            return {"action": "skip", "reason": f"consensus_check_failed: {reason_cons}", "meta": meta_cons, "key": key}

        # 7. 盘口与执行方案 (Capped Taker 优先)
        tok_yes = target_bucket.get("yes_token_id") or target_bucket.get("_yes_token_id")
        if not tok_yes:
            return {"action": "skip", "reason": "no_yes_token_for_target_bucket", "key": key}
        book = books_by_token.get(str(tok_yes))
        if not book:
            return {"action": "skip", "reason": "target_book_missing", "key": key}

        ask = get_best_ask(book)
        bid = get_best_bid(book)
        if ask is None:
            return {"action": "skip", "reason": "no_active_ask_in_book", "key": key}

        entry_mode = self.cfg.get("entry_mode", "capped_taker")
        # F-A（2026-09-12）：既有目标桶通道同样走**唯一基数解析器**（生效 fire 预算）。
        # 无 fire_budget_usdc 时回退旧键 order_budget_usdc（缺省 15.0）⇒ 旧调用方/旧用例逐字不变。
        budget = self.order_budget()
        min_ask = _dec(self.cfg["yes_min_ask"], "0.45")
        max_ask = _dec(self.cfg["yes_max_ask"], "0.75")

        if entry_mode == "capped_taker":
            # 模式 A: Capped Taker (主动吃单，带顶价 0.75，不错失高胜率确定性机会)
            if ask < min_ask:
                return {"action": "skip", "reason": f"ask_below_confirmation_floor ({ask} < {min_ask})", "key": key}
            if ask > max_ask:
                return {"action": "skip", "reason": f"ask_above_safety_cap ({ask} > {max_ask})", "key": key}

            shares = (budget / ask).to_integral_value(rounding=ROUND_DOWN)
            if shares <= ZERO:
                return {"action": "skip", "reason": "zero_shares_calculated", "key": key}
            cost = ask * shares

            # 记录开仓并推进计数
            pos = PositionRecord(
                session_key=key,
                bucket_id=t_id,
                yes_token_id=str(tok_yes),
                shares=shares,
                cost_usdc=cost,
                avg_price=ask,
                entry_ts_utc=now_utc.isoformat(),
                liquidated=False,
            )
            self.state.open_positions[key] = pos
            self.state.locked_sessions[key] = {
                "locked_at": now_utc.isoformat(),
                "target_bucket_id": t_id,
                "expected_temp": expected_extreme_temp,
                "consensus_meta": meta_cons,
            }
            self.state.session_fires_count[key] = cur_fires + 1

            return {
                "action": "execute_taker_fire",
                "key": key,
                # 通道标识（加法式，2026-09-12）：既有目标桶通道；引擎据此写审计字段
                "entry_channel": CHANNEL_TARGET,
                "entry_mode": "capped_taker",
                "fire_no": self.state.session_fires_count[key],
                "bucket_id": t_id,
                "token_id": str(tok_yes),
                "side": "BUY",
                "outcome": "YES",
                "fill_price": str(ask),
                "shares": str(shares),
                "cost_usdc": str(cost),
                "cap": str(max_ask),
                "consensus_meta": meta_cons,
            }
        else:
            # 模式 B: Peg 挂单模式 (带有超时自动撤销)
            mid = get_book_mid(book) or ask
            discount = _dec(self.cfg["mid_discount"], "0.90")
            tick = _dec(book.get("tick_size") if isinstance(book, dict) else getattr(book, "tick_size", None), "0.01")
            raw_price = mid * discount
            limit_price = (raw_price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            if limit_price < min_ask:
                limit_price = min_ask
            if limit_price > max_ask:
                return {"action": "skip", "reason": f"peg_price_above_cap ({limit_price} > {max_ask})", "key": key}

            shares = (budget / limit_price).to_integral_value(rounding=ROUND_DOWN)
            if shares <= ZERO:
                return {"action": "skip", "reason": "zero_shares_calculated", "key": key}
            cost = limit_price * shares

            order = RestingOrder(
                order_id=f"ord_{key}_{int(time.time())}",
                session_key=key,
                city_id=city_id,
                bucket_id=t_id,
                token_id=str(tok_yes),
                side="BUY",
                limit_price=limit_price,
                shares=shares,
                cost_usdc=cost,
                created_at_epoch=time.time(),
                timeout_seconds=float(self.cfg.get("resting_order_timeout_s", 180)),
                status="OPEN",
            )
            self.state.active_orders[order.order_id] = order
            self.state.session_fires_count[key] = cur_fires + 1
            return {
                "action": "place_resting_order",
                "key": key,
                "entry_mode": "best_bid_peg",
                "fire_no": self.state.session_fires_count[key],
                "order_id": order.order_id,
                "bucket_id": t_id,
                "token_id": str(tok_yes),
                "limit_price": str(limit_price),
                "shares": str(shares),
                "cost_usdc": str(cost),
                "timeout_seconds": order.timeout_seconds,
            }

    # -------------------------------------------------------------- ① 止损门限按通道解耦
    def stop_loss_channel(self, session_key: str) -> str:
        """该仓位所属的入场通道（① 的唯一来源）。

        取自 ``self.state.locked_sessions[session_key]["entry_channel"]``；**缺失/未知一律视为
        目标桶通道**（既有行为逐字兼容：target 通道的 locked_sessions 本就不带 ``entry_channel``）。
        """
        locked = self.state.locked_sessions.get(session_key) or {}
        channel = locked.get("entry_channel")
        if not channel:
            pos = self.state.open_positions.get(session_key)
            if pos and getattr(pos, "entry_channel", ""):
                channel = pos.entry_channel
        return CHANNEL_NEXT if str(channel) == CHANNEL_NEXT else CHANNEL_TARGET

    def stop_loss_bid_floor(self, channel: str, entry_avg_price: Decimal) -> Decimal:
        """该通道的止损触发线（①，纯函数）。

        * 目标桶通道：``early_stop_bid_floor``（0.45）—— **逐字不变**，既有测试必须全绿；
        * next_bucket 通道：``max(入场实际成交均价 × next_bucket_stop_loss_pct,
          next_bucket_bid_floor)``（例：0.32 入场 ⇒ max(0.16, 0.12) = 0.16）。

        必须以**实际成交均价**（③ 修好后的 ``pos.avg_price``）为基准，绝不能用限价。
        """
        if channel != CHANNEL_NEXT:
            return _dec(self.cfg["early_stop_bid_floor"], "0.65")
        pct = _dec(self.cfg.get("next_bucket_stop_loss_pct",
                                DEFAULT_CONFIG["next_bucket_stop_loss_pct"]), "0.50")
        abs_floor = _dec(self.cfg.get("next_bucket_bid_floor",
                                      DEFAULT_CONFIG["next_bucket_bid_floor"]), "0.12")
        rel = _dec(entry_avg_price, "0") * pct
        return max(rel, abs_floor).quantize(Decimal("0.0001"))

    def stop_loss_reason(self, channel: str, entry_avg_price: Decimal, bid: Decimal,
                         floor: Decimal) -> str:
        """止损 reason 串 —— **自证来源**（① 要求：写清通道、入场基准与 pct）。

        * next_bucket：``bid_floor_broken (chan=next_bucket, bid=0.14 < floor 0.16 = entry 0.32 x 0.50)``
        * 目标桶通道：``bid_floor_broken (bid=0.29 < 0.45)`` —— 逐字保持既有格式/既有测试。
        """
        if channel != CHANNEL_NEXT:
            return f"bid_floor_broken (bid={bid} < {floor})"
        pct = _dec(self.cfg.get("next_bucket_stop_loss_pct",
                                DEFAULT_CONFIG["next_bucket_stop_loss_pct"]), "0.50")
        return (f"bid_floor_broken (chan=next_bucket, bid={bid} < floor {_fmt_dec(floor)} "
                f"= entry {_fmt_dec(_dec(entry_avg_price, '0'))} x {pct})")

    def _grace_seconds(self) -> int:
        """② 开仓冷静期秒数（整数；不可解析/负数 ⇒ 0，即不抑制）。"""
        raw = self.cfg.get("early_stop_grace_seconds", DEFAULT_CONFIG["early_stop_grace_seconds"])
        try:
            value = int(raw)
        except Exception:  # noqa: BLE001 - 坏配置绝不抑制风控
            return 0
        return value if value > 0 else 0

    @staticmethod
    def _position_age_seconds(entry_ts_utc: str, now_utc: datetime) -> float | None:
        """距入场的秒数；时间戳缺失/不可解析 ⇒ ``None``（调用方据此**不抑制**，fail-open 到风控）。"""
        text = str(entry_ts_utc or "").strip()
        if not text:
            return None
        try:
            ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now_utc - ts).total_seconds()

    def evaluate_early_stop_loss(
        self,
        session_key: str,
        direction: str,
        buckets: list[dict[str, Any]],
        books_by_token: dict[str, Any],
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        """核心风控 1: Pre-METAR 盘口先导抢跑止损。

        在官方 METAR 出炉前 30~60 秒，通过盘口异动提前感知破位迹象：
        - 触发条件 A (下一档异动): 下一档危险桶 YES 卖价暴涨突破 0.35 (买家凶猛)；
        - 触发条件 B (买盘撤单崩溃): 当前持仓桶 YES 的最佳买价跌破**该仓位的止损门限**。
        立即以盘口最佳买价卖出割肉，保全 50%+ 资金！

        ① 门限按**通道解耦**（2026-09-13）：目标桶通道 = 0.45（逐字不变）；next_bucket 通道 =
        ``max(入场实际成交均价 × next_bucket_stop_loss_pct, next_bucket_bid_floor)``。

        ② 开仓冷静期（2026-09-13）：距 ``pos.entry_ts_utc`` 不足 ``early_stop_grace_seconds`` 时，
        **仅条件 B** 不执行 —— 返回一条 ``early_stop_suppressed`` 审计事件（含 would-be reason /
        bid / floor / 剩余冷静期 / session_key / entry_channel），调用方只记审计、不平仓。
        条件 A 不受冷静期影响。同一仓位同一轮只记一次（签名去重）。
        """
        if not self.cfg.get("early_stop_enabled", True):
            return None

        pos = self.state.open_positions.get(session_key)
        if not pos or pos.liquidated:
            return None

        ordered = ordered_buckets(buckets)
        curr_b = next((b for b in ordered if str(b.get("bucket_id") or b.get("id") or "") == pos.bucket_id), None)
        if not curr_b:
            return None
        c_idx = ordered.index(curr_b)

        # 定位下一档桶
        next_idx = c_idx + 1 if direction.lower() == "high" else c_idx - 1
        next_b = ordered[next_idx] if 0 <= next_idx < len(ordered) else None

        # 检查触发条件
        trigger_reason = None
        trigger_channel = None
        book_curr = books_by_token.get(pos.yes_token_id)
        bid_curr = get_best_bid(book_curr) or ZERO

        # 条件 B: 持仓买盘跌破该通道的止损门限（① 门限按通道解耦；基准 = 实际成交均价）
        # 胜单盘口空洞哨兵：若持仓桶卖价 ask >= 0.70 说明标的确定性极高、买盘稀疏仅为做市真空，不得误杀割肉
        channel = self.stop_loss_channel(session_key)
        floor_bid = self.stop_loss_bid_floor(channel, pos.avg_price)
        ask_curr = get_best_ask(book_curr)
        winning_book = ask_curr is not None and ask_curr >= Decimal("0.70")
        if not winning_book and bid_curr < floor_bid and bid_curr > ZERO:
            trigger_reason = self.stop_loss_reason(channel, pos.avg_price, bid_curr, floor_bid)
            trigger_channel = channel

        # 条件 A: 下一档暴涨 (e.g. 0.35) —— 门限不随通道变化，且**不受冷静期影响**
        # 胜单保护与真实买盘哨兵：若持仓已是 winning_book (ask >= 0.70) 或下一档买盘无真实支撑 (bid < 0.15 幽灵盘口)，不得误杀
        if not trigger_reason and not winning_book and next_b:
            next_yes_tok = next_b.get("yes_token_id") or next_b.get("_yes_token_id")
            book_next = books_by_token.get(str(next_yes_tok)) if next_yes_tok else None
            ask_next = get_best_ask(book_next)
            bid_next = get_best_bid(book_next) or ZERO
            surge_thresh = _dec(self.cfg["early_stop_next_bucket_surge"], "0.35")
            if ask_next is not None and ask_next >= surge_thresh and bid_next >= Decimal("0.15"):
                trigger_reason = f"next_bucket_surge (next_ask={ask_next} >= {surge_thresh})"

        if not trigger_reason:
            return None

        # ② 开仓冷静期：只抑制条件 B；被抑制必须落审计（同一仓位/同一 reason 只记一次）
        if trigger_channel is not None:
            grace = self._grace_seconds()
            age = self._position_age_seconds(pos.entry_ts_utc, now_utc)
            if grace > 0 and age is not None and age < grace:
                return self._suppressed_early_stop(session_key, trigger_channel, trigger_reason,
                                                   bid_curr, floor_bid, grace, age, now_utc)

        # 抢跑割肉平仓
        recovered_usdc = (pos.shares * bid_curr).quantize(Decimal("0.0001"))
        loss_usdc = (pos.cost_usdc - recovered_usdc).quantize(Decimal("0.0001"))
        pos.liquidated = True
        pos.liquidation_type = trigger_reason
        pos.recovered_usdc = recovered_usdc
        pos.realized_pnl_usdc = -loss_usdc

        # 防线 1: 止损后单向熔断，当日禁止再次对该标的开仓！
        self.state.stopped_out_sessions.add(session_key)
        self.state.breached_sessions.add(session_key)

        # 撤销所有本 session 未成交挂单
        for ord_item in self.state.active_orders.values():
            if ord_item.session_key == session_key and ord_item.status == "OPEN":
                ord_item.status = "CANCELLED_BREACH"

        return {
            "action": "early_stop_loss_executed",
            "session_key": session_key,
            "reason": trigger_reason,
            "entry_channel": channel,
            "liquidated_token": pos.yes_token_id,
            "shares": str(pos.shares),
            "sell_price": str(bid_curr),
            "recovered_usdc": str(recovered_usdc),
            "loss_usdc": str(loss_usdc),
            "recovery_ratio": f"{float(recovered_usdc / pos.cost_usdc * 100):.1f}%" if pos.cost_usdc > 0 else "0%",
            "at_utc": now_utc.isoformat(),
        }

    def _suppressed_early_stop(self, session_key: str, channel: str, would_be_reason: str,
                               bid: Decimal, floor: Decimal, grace: int, age: float,
                               now_utc: datetime) -> dict[str, Any]:
        """② 被冷静期抑制的止损审计事件（非平仓）。

        **不得静默**：返回一条 ``action == "early_stop_suppressed"`` 的事件，含 would-be reason /
        bid / floor / 剩余冷静期秒数 / session_key / entry_channel。签名 ``(entry_ts, reason)`` 去重
        ⇒ 同一仓位同一轮/同一形态只记一次，避免刷屏。调用方（引擎）只落审计、**不平仓、不熔断**。
        """
        pos = self.state.open_positions.get(session_key)
        remaining = int(max(grace - (age or 0.0), 0))
        signature = f"{getattr(pos, 'entry_ts_utc', '')}|{would_be_reason}"
        duplicate = self.state.early_stop_suppressed.get(session_key) == signature
        if not duplicate:
            self.state.early_stop_suppressed[session_key] = signature
        return {
            "action": "early_stop_suppressed",
            "session_key": session_key,
            "entry_channel": channel,
            "would_be_reason": would_be_reason,
            "bid": str(bid),
            "floor": _fmt_dec(floor),
            "grace_seconds": grace,
            "age_seconds": int(age or 0),
            "remaining_grace_seconds": remaining,
            "duplicate": duplicate,
            "at_utc": now_utc.isoformat(),
        }

    def handle_breach_risk_control(
        self,
        session_key: str,
        city: dict[str, Any],
        direction: str,
        buckets: list[dict[str, Any]],
        new_metar_temp: float,
        books_by_token: dict[str, Any],
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        """核心风控 2: METAR 实测硬破位风控与反手追火。
        
        若前置抢跑未触发，官方报文最终确认破位时执行保底风控：
        1. 立即清空持仓与挂单；
        2. 反手买 NO (严格校验 no_ask <= 0.85，若扫空至 0.99 则安全跳过)；
        3. 顺势买新 YES (限价 <= 0.75，严格受 max_fires_per_session <= 2 约束)。
        """
        lock_info = self.state.locked_sessions.get(session_key)
        pos = self.state.open_positions.get(session_key)
        if not lock_info and not pos:
            return None

        orig_bucket_id = (lock_info or {}).get("target_bucket_id") or (pos.bucket_id if pos else None)
        ordered = ordered_buckets(buckets)
        new_b = find_bucket(buckets, new_metar_temp)
        if not new_b:
            return None
        new_b_id = str(new_b.get("bucket_id") or new_b.get("id") or "")

        orig_b = next((b for b in ordered if str(b.get("bucket_id") or b.get("id") or "") == str(orig_bucket_id)), None)
        if not orig_b:
            return None
        o_idx = ordered.index(orig_b)
        n_idx = ordered.index(new_b)

        dir_norm = direction.lower()
        is_breach = (dir_norm == "high" and n_idx > o_idx) or (dir_norm == "low" and n_idx < o_idx)
        if not is_breach:
            return None

        self.state.breached_sessions.add(session_key)

        # 1. 撤销挂单
        for ord_item in self.state.active_orders.values():
            if ord_item.session_key == session_key and ord_item.status == "OPEN":
                ord_item.status = "CANCELLED_BREACH"

        # 2. 保底清仓 (若尚未割肉)
        liq_result = None
        if pos and not pos.liquidated:
            book_curr = books_by_token.get(pos.yes_token_id)
            bid_px = get_best_bid(book_curr) or ZERO
            recovered = (pos.shares * bid_px).quantize(Decimal("0.0001"))
            loss = (pos.cost_usdc - recovered).quantize(Decimal("0.0001"))
            pos.liquidated = True
            pos.liquidation_type = "METAR_BREACH"
            pos.recovered_usdc = recovered
            pos.realized_pnl_usdc = -loss
            liq_result = {
                "token_id": pos.yes_token_id,
                "shares": str(pos.shares),
                "sell_price": str(bid_px),
                "recovered_usdc": str(recovered),
                "loss_usdc": str(loss),
            }

        # 3. 检查单日追火上限 (防止连跳双杀)
        cur_fires = self.state.session_fires_count.get(session_key, 1)
        max_fires = int(self.cfg.get("max_fires_per_session", 2))
        if cur_fires >= max_fires:
            return {
                "action": "breach_risk_control_closed_only",
                "reason": f"max_fires_reached ({cur_fires}>={max_fires}), no_further_hedge",
                "session_key": session_key,
                "liquidation": liq_result,
            }

        # 4. 反手腿构建 (包含微观结构 NO 扫空防御)
        hedge_legs = []
        broken_no_tok = orig_b.get("no_token_id") or orig_b.get("_no_token_id")
        book_no = books_by_token.get(str(broken_no_tok)) if broken_no_tok else None
        ask_no = get_best_ask(book_no)
        no_cap = _dec(self.cfg["risk_control_no_cap"], "0.85")

        if ask_no is not None and ask_no <= no_cap:
            hedge_legs.append({
                "leg": "buy_no_broken",
                "token_id": str(broken_no_tok),
                "side": "BUY",
                "outcome": "NO",
                "cap": str(no_cap),
                "budget_usdc": "10.0",
            })
        else:
            # 真实微观结构记录：破位瞬间 NO ask 往往已 >=0.99 或空盘，主动安全跳过！
            pass

        new_yes_tok = new_b.get("yes_token_id") or new_b.get("_yes_token_id")
        yes_cap = _dec(self.cfg["risk_control_yes_cap"], "0.75")
        hedge_legs.append({
            "leg": "buy_yes_new",
            "token_id": str(new_yes_tok),
            "side": "BUY",
            "outcome": "YES",
            "cap": str(yes_cap),
            "budget_usdc": "15.0",
        })

        self.state.session_fires_count[session_key] = cur_fires + 1

        return {
            "action": "breach_reverse_executed",
            "session_key": session_key,
            "fire_no": self.state.session_fires_count[session_key],
            "breach_temp": new_metar_temp,
            "orig_bucket_id": orig_bucket_id,
            "new_bucket_id": new_b_id,
            "liquidation": liq_result,
            "hedge_legs": hedge_legs,
            "at_utc": now_utc.isoformat(),
        }


    def evaluate_take_profit(
        self,
        session_key: str,
        pos_data: dict[str, Any],
        books_by_token: dict[str, Any],
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        """检查是否触发 50% 动态阶梯止盈 (Bid >= entry_avg_price * 1.50)."""
        if not self.cfg.get("take_profit_enabled", True):
            return None
        if pos_data.get("take_profit_done"):
            return None
        legs = pos_data.get("legs", [])
        yes_leg = next((l for l in legs if l.get("outcome") == "YES" and not l.get("settled")), None)
        if not yes_leg:
            return None
        tok = str(yes_leg.get("token_id") or "")
        book = (books_by_token or {}).get(tok)
        if not book:
            return None
        best_bid = get_best_bid(book)
        if not best_bid or best_bid <= ZERO:
            return None

        avg_price = _dec_or_none(yes_leg.get("avg_price")) or _dec_or_none(pos_data.get("avg_price")) or ZERO
        if avg_price <= ZERO:
            return None

        tp_mult = Decimal(str(self.cfg.get("take_profit_multiplier", "1.50")))
        tp_target = (avg_price * tp_mult).quantize(Decimal("0.0001"))
        if best_bid < tp_target:
            return None

        current_shares = _dec_or_zero(yes_leg.get("shares"))
        if current_shares < Decimal("5.0"):
            return None

        # 仓位 >= 10 股时卖出一半；若 < 10 股则整仓止盈
        if current_shares >= Decimal("10.0"):
            sell_shares = (current_shares * Decimal("0.5")).quantize(Decimal("0.0001"))
        else:
            sell_shares = current_shares

        return {
            "action": "take_profit",
            "session_key": session_key,
            "token_id": tok,
            "shares_to_sell": sell_shares,
            "current_shares": current_shares,
            "best_bid": best_bid,
            "entry_avg_price": avg_price,
            "tp_target": tp_target,
        }

    def handle_weather_abortion_risk_control(
        self,
        session_key: str,
        city: dict[str, Any],
        direction: str,
        curr_temp: float,
        metar_obs: dict[str, Any] | None,
        now_utc: datetime,
    ) -> dict[str, Any] | None:
        """气温不可逆夭折证伪判定 (最高温市场 15:00 后日内高点暴跌 >= 2.5C 且 METAR 出现 TS/RA/CB)."""
        if not self.cfg.get("weather_abortion_enabled", True):
            return None
        if direction.lower() != "high":
            return None
        try:
            from zoneinfo import ZoneInfo
            tz_name = city.get("timezone") or "UTC"
            loc_hr = now_utc.astimezone(ZoneInfo(tz_name)).hour
        except Exception:
            loc_hr = now_utc.hour
        cutoff_hr = int(self.cfg.get("weather_abortion_cutoff_hour", 15))
        if loc_hr < cutoff_hr:
            return None

        hist = self._metar_history.get(session_key, [])
        if not hist:
            return None
        peak_temp = max(h["temp"] for h in hist)
        temp_drop = peak_temp - curr_temp
        threshold = float(self.cfg.get("weather_abortion_temp_drop", 2.5))
        if temp_drop < threshold:
            return None

        if not metar_obs:
            return None
        raw_ob = str(metar_obs.get("rawOb") or metar_obs.get("raw_text") or metar_obs.get("raw") or "").upper()
        wx_str = str(metar_obs.get("wxString") or metar_obs.get("weather") or "").upper()
        clouds = str(metar_obs.get("clouds") or "").upper()
        has_convective = any(kw in raw_ob for kw in ("CB", "TS", "RA")) or                          any(kw in wx_str for kw in ("CB", "TS", "RA")) or                          ("CB" in clouds)
        if not has_convective:
            return None

        return {
            "action": "weather_abortion",
            "session_key": session_key,
            "peak_temp": peak_temp,
            "curr_temp": curr_temp,
            "temp_drop": temp_drop,
            "raw_ob": raw_ob[:40],
        }

    def process_order_lifecycle(self, now_epoch: float, books_by_token: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        for oid, order in list(self.state.active_orders.items()):
            if order.status != "OPEN":
                continue
            if order.is_expired(now_epoch):
                order.status = "CANCELLED_TIMEOUT"
                events.append({
                    "event": "order_timeout_cancelled",
                    "order_id": oid,
                    "session_key": order.session_key,
                    "shares_unfilled": str(order.shares),
                    "cost_refunded": str(order.cost_usdc),
                    "alive_seconds": round(now_epoch - order.created_at_epoch, 1),
                })
                continue

            book = books_by_token.get(order.token_id)
            if not book:
                continue
            ask = get_best_ask(book)
            if ask is not None and ask <= order.limit_price:
                order.status = "FILLED"
                pos = PositionRecord(
                    session_key=order.session_key,
                    bucket_id=order.bucket_id,
                    yes_token_id=order.token_id,
                    shares=order.shares,
                    cost_usdc=order.cost_usdc,
                    avg_price=ask,
                    entry_ts_utc=datetime.now(timezone.utc).isoformat(),
                    liquidated=False,
                )
                self.state.open_positions[order.session_key] = pos
                events.append({
                    "event": "resting_order_filled",
                    "order_id": oid,
                    "session_key": order.session_key,
                    "shares": str(order.shares),
                    "fill_price": str(ask),
                    "limit_price": str(order.limit_price),
                    "cost_usdc": str(order.cost_usdc),
                })
        return events
