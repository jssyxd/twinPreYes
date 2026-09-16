# Changelog — weatherbotPreYes0910

## 2026-09-13 (r104) — live 退场余量守卫：股数 < venue `min_order_size` ⇒ 本地弃单（零发单）

**未改动任何策略阈值 / 门序 / 窗口 / 闸门 / 腿配置**；paper 回归逐字不变（`tests_live_exit` 的
paper golden sha `0b75662d02a2f990…` + `paper_reversal_sim.py --scenarios-only` sha `3d16632c458bb969…`
仍一致）；11 套件全绿（`tests_live_exit.py` 17 → **18** 项）。

### 症状（实盘，真实资金）

`data/runner.log` + `data/live_events.jsonl` 每 ~23 s 出现一对
`intent(execute_leg, side=SELL, size=0.0041)` / `exception(submit_failed, 400 invalid maker amount)`：
11 分钟内 **30 次**真实提交、30 次被交易所拒，全部打在 `buenos-aires|2026-09-13|high`
（token `1081859363…922`）同一条腿上，**永不停止**（`pending_exit` 续卖扫每轮重试）。

### 根因（三层，全部有实盘/链上证据）

1. **venue 侧截断**：该腿入场成交 15.814063 股（data-api `/trades` BUY），早停 SELL 提交 15.814063 股，
   但 CLOB `get_order` 的 `original_size` / `size_matched` 均 = **15.81** ⇒ 余下
   **0.004063 股真实留在链上**（CTF `balanceOf` = raw `4063` / 1e6，对照 paris 15.92 / taipei 5.0 逐位吻合）。
2. **venue 最小下单量是硬约束**：该盘口 `min_order_size = 5`（股）⇒ 0.0041 股的单**必然**被拒
   （`invalid maker amount`），**卖不掉、也永远卖不掉**；但续卖扫把它当普通部分成交余量无限重试。
3. **缺守卫**：入场侧早已有 F-D 守卫（`live/port.py::match`：计划股数 < `min_order_size` ⇒ 本地弃单），
   **退场侧（`live/exit.py`）没有** ⇒ 与 F-D 同根因的缺陷在新通道里复现。

### 修复（`live/exit.py`，纯加守卫：fail-closed，绝不降级、绝不虚拟平仓）

- 新增 `REASON_BELOW_MIN = "below_min_order_size"`（与入场侧同一字面量）与纯函数
  `book_min_order_size(book)`（缺失 / 非法 / `<= 0` ⇒ `None` ⇒ **守卫不生效**，与 F-D 同口径）。
- `plan_live_exit(..., min_order_size=)`：判定序插入第 6 条 —— 余量 < venue 最小量 ⇒
  `defer:below_min_order_size`（零发单、零尝试计数、仓位保持 open）。
- `live_exit_leg` 从当前盘口取 `min_order_size` 传入；`LiveExitChannel.sell_leg` 同一条边界再挡一次
  （防御纵深：零签名、零发单）；`_NO_FILL_STATUSES` 收录该 status。
- 余量**一分不丢**：`pending_exit` 保持原样，等既有 `settle_markets` 兜底（losing/winning 均按链上 1/0）。

### 测试

`tests_live_exit.py::check_below_venue_min_order_size`：解析器 8 组非法输入 ⇒ `None`；边界
`0.0041 < 5 ⇒ defer`、`4.9999 < 5 ⇒ defer`、`5 / 5.0001 / 25 ⇒ sell`、`min 未知 ⇒ sell`（原行为）；
端到端（真 `execute_leg` + 桩交易所）余量 < min ⇒ **零 execute_leg / 零 post_order / 零尝试计数**，
仓保持 open 且 `pending_exit` 不丢，`exit_deferred` 事件带机器可读 reason；对照组 25 股照常真实卖出。

## 2026-09-13 — live 真实 SELL 出场通道（消除 live 下的「虚拟平仓」）+ SELL 地板语义

依据 `/tmp/preyes_live_exit_spec.md`。**未改动任何策略阈值 / 门序 / 窗口语义**；**paper 回归逐字不变**
（`paper_reversal_sim.py --scenarios-only` stdout sha `3d16632c458bb969…`；4 条退场通道的 paper
行为 golden sha `0b75662d02a2f990…`）；11 套件全绿（含新增 `tests_live_exit.py` 15/15）；零真实订单
（全部桩 transport / 桩 SDK，`data/live_events.jsonl` 不存在）。

### 1) 问题（生产隐患，账实背离）

`_r_cycle.py` 的 **4 处**腿退场原先一律调用 `paper_capital.close_leg_at_best_bid`：按 `best_bid`
记账、把腿标 `settled`、**不下真实卖单、不撤真实挂单**。在 **live** 下 ⇒ 真币仍留在钱包、链上之后
按 1/0 结算，而引擎账本以为已在 bid 出场 ⇒ **账本与真实账户背离**（与「权益一律以真实账户为准」冲突）。

| # | 调用点 | 业务含义 | `exit_channel` |
|---|--------|----------|----------------|
| 1 | `_expire_stale_sleeves` | 未破位 sleeve 超时退场 | `sleeve_timeout` |
| 2 | `record_refire` | 追火旧桶 YES 腿清算 | `refire_liq` |
| 3 | `run_cycle` 早停块 | 抢先止损（`evaluate_early_stop_loss`） | `early_stop` |
| 4 | `run_cycle` 破位块 | METAR 破位风控清算 | `breach_rc` |

### 2) 实现

- **新增 `live/exit.py`**（决策与网络分离）：
  - 纯函数：`exit_settings`（解析 `live_sell_enabled`/`live_sell_floor`/`live_sell_max_attempts_per_cycle`
    + `base_fee_rate`）、`parse_floor`、`best_bid_of`、`plan_live_exit`（卖/弃决策 + 端点语义）、
    `apply_live_exit_fill`（按**真实成交**记账）、`fill_failed_reason`。
  - 网络：`LiveExitChannel`（三闸门 + 真实客户端 + 先撤真实挂单 + SELL FAK + 卖后真实账户）、
    `get_channel`、`live_exit_leg`（一腿一次尝试的完整编排）、`describe`。
- **`live/v2_transport.execute_leg` 增加 SELL 侧价格护栏**：新增入参 `floor` / `exit_channel`；
  `BUY` 仍用 `cap`（`≤ cap` 才发），`SELL` 用 `floor`（`≥ floor` 才发，`cap` 不参与）。
  `floor` 缺失 ⇒ 拒单 `exit_floor_required`；不可解析 / `≤ 0` / `> 1` ⇒ `exit_floor_invalid`；
  `want < floor` ⇒ `below_exit_floor`；**三条都是拒单，绝不回退成无保护卖单**。
  tick 对齐 / market `amount` 精度（SELL = 股数 ≤ 4 dp）沿用既有硬化。
- **`_r_cycle.py` 4 个调用点改为 `if _cycle_is_live(cfg): <真实卖> else: <原 paper 逐字保留>`**；
  paper 分支源码逐字保留（`tests_live_exit.py` 逐行比对 + AST 断言）。
- **账本按真实成交更新**：部分成交只减 `leg["shares"]`（余量留仓、下轮续卖）；
  `leg.settled` 仅在股数清零时置位；`pos.liquidated` 仅在该仓**所有 YES 腿**了结时置位；
  taker 费（默认 2%，`base_fee_rate`）显式记账 `exit_fee_usdc`，净回收 `exit_net_usdc` 记入现金池，
  并写 `payout_credit_usdc`（settle/equity 的既有字段语义不变）。
- **续卖扫 `_retry_pending_exits`**（live-only，新增第 5 个「真实卖」入口，**不是**虚拟平仓）：
  延期/部分成交的腿登记在该仓 `pending_exit` 上，下一轮统一续卖（追火旧桶清算是一次性事件，
  没有这条队列就没人兜余量）。
- **`live_sell_max_attempts_per_cycle` 是**真**上限**：`run_cycle` 仅 live 每轮
  `live_exit.bump_cycle(state)`，`live_exit_leg` 以「本轮该 token 真实下单次数」判定
  （卖出已发出才计数）⇒ 同一轮同腿第二次真实下单被 `defer: attempts_exhausted` 挡住（零发单）；
  计数键 (`live_exit_cycle`/`live_exit_attempts`) **只在 live 下写** ⇒ paper state/golden 一个键都不多。
- **不变式（live 下 `close_leg_at_best_bid` 不可达）**：① 静态 —— 4 个调用点全部位于 live 分支的
  `else` 里（AST 断言）；② 运行时 —— `_r_cycle.close_leg_at_best_bid` 本身是**守卫包装**，在 live
  轮次遇到仍有股数的腿 ⇒ 记 `virtual_close_refused` 事件 + 抛 `LiveVirtualCloseRefused`，
  绝不静默按 bid 记账（paper 模式逐字透传 `paper_capital.close_leg_at_best_bid`）。
- **`live/port.py::LivePort`** 新增 `ensure_client()`（出场专用：凭据 + 网络 + 客户端，
  **不跑** `risk_gate` / `check_limits` ⇒ 卖出不被入场型上限阻挡）与 `read_account_now()`
  （卖后真实账户，供审计）；`preflight` 顺带缓存 `_credentials`。
- **审计（不可伪造）**：`SPOOFABLE_AUDIT_KEYS` 9 → **12**（新增 `side` / `exit_channel` / `sell_floor`；
  此前 `side` 会被 `params.update(extra_params)` 覆盖 ⇒ 一条卖单能被记成买单）；`execute_leg` 由
  专用入参盖章 `exit_channel`（白名单 4 通道）/ `sell_floor` / `leg_window` / `side`；
  成功卖后读**真实账户**（余额/持仓）写进 `live_exit` 事件；成交明细（股数/均价/手续费/订单 id）入事件。
- **config**：`config/yes2re_reversal.json` 的 `consensus_lock` 块 + `strategy_consensus_lock.DEFAULT_CONFIG`
  + `live/exit.py` 代码默认三处一致：`live_sell_enabled=true` / `live_sell_floor="0.05"` /
  `live_sell_max_attempts_per_cycle=1`；`tests_port.GOLDEN_CONFIG` 同步（`load_config` 快照）。
  置 `live_sell_enabled=false` 只是**回退开关**：退场一律延期（fail-closed、由 settle 兜底），
  **依旧不存在虚拟平仓**。

### 3) 测试（`tests_live_exit.py`，15/15）

地板矩阵（`{0.049,0.050,0.051}`×`floor=0.05` ⇒ 弃/卖/卖；`bid<floor` 弃、`bid==floor` 卖，决策层与
下单层同一条边界）；floor 缺失/0/负/1.5/abc ⇒ 拒单零发单（合法地板对照组发单）；部分成交 60% ⇒
留 40% + 两轮清空（leg settled + pos liquidated，NO 腿仍待 settle）；被拒/no_bid/无深度/离价 ⇒
`exit_deferred` + 仓位 open + 无 liquidated + 可重试；三闸门任一缺失 ⇒ `defer gate_*`，而持仓数满 /
资金上限 / 预算（`preflight` 会拒）**不阻挡**卖出（并证明出场路径从不调用 `risk_gate.evaluate` /
`check_limits`）；撤单先于卖单（`list_open_orders → cancel_with_retry → execute_leg` 顺序断言，
撤单失败 ⇒ 零发单）；2% 费与净回收记账；neg_risk 重取/缓存/未知拒单；审计字段不可被 `audit_extra`
伪造；paper 分支逐字 + AST 不变式；paper 行为 golden + sim sha；4 条通道在 live 下各发 1 笔真实 FAK
SELL（合计恰 4）；虚拟平仓哨兵 + 续卖扫（live 卖、paper 不动）；每轮尝试上限（默认 1，可配 2）；
config/GOLDEN 一致 + 零真实订单。

**证据**：`tests_port.py` 35/35、`tests_live.py` 51/51、`tests_cycle_consensus_lock.py`、
`tests_consensus_lock.py` 16/16、`tests_reversal.py`、`tests_sleeve_wiring.py` 4/4、
`tests_breach_hedge.py`、`tests_fill_gate.py` 6/6、`tests_sleeve_signal.py` 13/13、
`tests_market_adapter.py` 4/4、`tests_live_exit.py` 15/15 —— 全绿（`python3.13` 与 `python3` 均通过）。

**未做（按操作者口径）**：不 commit / 不 push / 不下真实订单 / 不改策略阈值与门序。

## 2026-09-12 — F-A 预算基数统一 + 审计字段（无 TAF 暴露面可量化）+ 5 条 LOW 清扫

依据 `/tmp/preyes_next_bucket_audit_report.md` §5（F-A MEDIUM / F-B..F-F LOW / F-G..F-I INFO）与
`/tmp/preyes_fa_low_spec.md`。**未改动任何策略阈值 / 门序 / 窗口语义**；**paper 回归逐字不变**
（`3d16632c458bb969…`）；10 套件全绿；零真实订单（全部 paper/stub）。

### 1) F-A（MEDIUM）统一预算基数 —— 唯一基数 = **生效 fire 预算**

- **根因**：新通道把"已占用额度"记成 `order_budget_usdc × pct`（config 15.0 / 现 10.0），
  而引擎按 `fire_budget_usdc × pct`（可被 `YES2RE_FIRE_BUDGET_USDC` 覆盖）实际发单 ⇒ 基数不等时
  (i) fire < 旧基数 ⇒ 既有通道被静默少给 `(旧基数 − fire) × pct`；(ii) fire > 旧基数 ⇒
  `新通道 + 既有通道` 可越界（审计实测 15.0 + 22.5 = 37.5 > 30）。
- **修法**：`_r_cycle._get_consensus_lock_strat` 把**生效的 fire 预算**注入策略 cfg
  （取数写成与 `_paper_fire` 逐字同源的 `cfg.get("fire_budget_usdc", DEFAULTS[...])`，位于两次 merge
  之后 ⇒ 旧配置无法覆盖）；策略新增唯一基数解析器 `ConsensusLockStrategy.order_budget()`
  （优先 `fire_budget_usdc`；缺失时回退**已废弃**键 `order_budget_usdc`，再缺省 15.0），
  `strategy_consensus_lock.py` 的两处 sizing 改用该解析器（`:496` `(基数 × pct).quantize(0.01)`、
  `:688` 基数本身，数学逐字保留）。`DEFAULT_CONFIG.order_budget_usdc` 与 `DEPLOY_RUNBOOK.md` §7.3.1
  已标注**废弃**，"部署时必须手工对齐两个基数"的前置条件**从此取消**。
- **证据**：`tests_consensus_lock.py::test_budget_base_unified_fire_budget` 对
  fire ∈ {5,10,12,15,30,50} × pct ∈ {0.5,0.0,1.0}（18 组）断言
  `新通道预算 + 既有通道剩余 ≤ fire`、两者 ≥ 0、**计划名义额合计 ≤ fire**，并实证旧基数在
  fire>15 时必然越界、fire<15 时静默少给；
  `tests_cycle_consensus_lock.py::test_engine_budget_base_injected_from_effective_fire_budget`
  以真实 `consensus_entry_fire` 断言两通道预算**合计 == fire**（策略侧记账 == 引擎侧预算）。
- **行为影响**：现行 config（fire=10、order=10）下**数值零变化**；仅当 env 覆盖 fire 预算时才与
  旧行为不同（且新行为正是操作者意图：两通道随 fire 一起缩放）。

### 2) 审计字段：让"无 TAF 暴露面"可量化（纯追加，行为零变化）

- 入口 fire / 入口 refire / 破位反手·追火的 `fire`、`fire_attempt`、`breach_risk_control` 事件行
  追加 **`taf_present`(bool) / `taf_source`("taf"|"market_rank1") / `fire_path`("entry"|"hedge"|"refire")
  / `fire_key`(str)**。实现为 `_r_cycle.taf_audit_fields()`（纯函数、**绝不抛异常**，异常兜底返回保守值），
  `_record_fire_event` / `record_refire` 新增**关键字可选**入参（省略时从 fire 自身推导）
  ⇒ 既有调用方（paper sim、反转车道）逐字不变，`hedge_fire` 字典本身**一个键都没加**
  （F2 的 golden 用例继续逐字通过）。
- **查询模板**：`scripts/taf_exposure_stats.py`（stdlib）按 `(fire_path × taf_present)` 计数，
  直接输出"对冲路径中 `taf_present=False` 的次数"；对旧行（无字段）与撕裂行容错。
  实测：入口/对冲 × 有/无 TAF 四组合日志 → `fire_path=hedge 总=6 taf_present=False=3`，
  旧格式行被计为"缺追加字段"而不报错。
- **测试**：`tests_breach_hedge.py` 新增两例（对冲路径用真实 `run_cycle` + 真实 paper 填充与
  `record_refire`；入口路径用真实 `evaluate_entry`/`consensus_entry_fire`/`_record_fire_event`
  + 预置 rank-1 tracker），断言四组合下 4 字段存在且取值正确（无 TAF ⇒ `taf_present=False`、
  `taf_source="market_rank1"`；入口行 `taf_source` 与既有 `ref_source` 逐字一致）。

### 3) LOW 清扫

- **F-B/F-C（审计字段不可伪造）**：`live/v2_transport.execute_leg` 在函数入口把 9 个"不可伪造"键
  （`order_api` / `amount` / `amount_unit` / `entry_channel` / `leg_window` / `window_source` /
  `next_entry_window` / `neg_risk` / `neg_risk_source`）从 `audit_extra` **统一剔除**，
  再由本层计算值或专用入参盖章（新增 `leg_window_source` / `leg_next_entry_window` / `neg_risk_source`
  专用入参；缺失 ⇒ 该键不出现，绝不落调用方的值）；`live/port.py::match` 改走专用入参。
  ⇒ **5 条 early-deny 路径的 deny 行不再带走注入值**（此前"哪条通道被拒"可被伪造）。
  测试：`tests_port.py::test_v2_transport_unforgeable_audit_keys`（taker/maker/白名单外 + 5 条 deny）。
- **F-D（`min_order_size`）—— 结论：引擎 fire 路径**未检查**（全仓唯一强制点 `live/order_plan.py:225`
  只被 smoke/sign_dryrun 调用）⇒ 按 SPEC 加 **fail-closed 本地守卫**：新增
  `LivePort.match` 在计划股数 < 该腿 book 的 `min_order_size` 时**本地弃单**
  （`order_mode=skip`、`status=below_min_order_size`、**零发单**、reason 写进 `detail`，
  book 未给出该字段 ⇒ 不新增拒单，行为与以前逐字相同；`describe()["local_refusals"]` 广告该拒单）。
  测试：`tests_port.py::test_live_match_below_min_order_size_local_refusal`（4.9 弃 / 5.0 放 / 5.1 放）。
- **F-E（文档不变量）**：`live/port.py::fill_mode` docstring 与模块 docstring 按实现写实 ——
  "坏 cfg 窗口 ⇒ 整笔 fire 停摆"对**带自有窗口的新通道腿已不成立**（该腿只受自身 fail-closed
  窗口约束，绝不回退）；只有"无腿窗口"的既有腿仍由 cfg band 管辖（非法 ⇒ `yes_band_unparsed` 拒）。
- **F-F（腿→桶映射契约）**：`tests_cycle_consensus_lock.py::test_leg_to_bucket_mapping_contract`
  把该映射行为固定下来：fire 侧键优先（`broken_bucket_id` / `target_bucket_id` / `new_bucket_id`）、
  `new_bucket_id` 为空串/缺失 ⇒ 退回腿自带 `bucket_id`（审计回归实证的 `'' → 'B2'`）、其它腿名保持原值。

### 4) 已知影响面 / 风险（如实记录）

- **审计行**：既有腿（不带通道字段）的 intent 行现在**不再出现** `entry_channel: None` /
  `next_entry_window: None`（改为该键缺席）；非法/缺失值不再可能出现在 deny 行。属审计字段收紧。
- **live 小额腿**：F-D 守卫会让"计划股数 < venue 最小下单量"的腿在本地弃单（最可能是
  `sleeve_notional_pct` 很小的小单，生产 config `sleeve_enabled=false`）。paper 路径不受影响
  （paper 无 venue 最小量，且必须逐字不变）。若 book 未提供 `min_order_size`，守卫不生效
  （残留风险：venue 不报该字段时仍可能发小单被拒 —— 已在 docstring/报告标注）。
- **未修（保持原状）**：F-G/F-H/F-I（INFO）；F-H（破位反手分支无 TAF 的行为）已由 HEAD 4ca3397 修复。
- 不 commit / 不 push（本次仅工作区改动）。

### 验证

```
10 套件（全部 exit 0）: tests_consensus_lock 16/16 · tests_cycle_consensus_lock 6 用例 ·
tests_breach_hedge 5 用例 · tests_port 35/35 · tests_live 51/51 · tests_fill_gate 6 ·
tests_reversal 24 · tests_sleeve_signal 13 · tests_sleeve_wiring 4 · tests_market_adapter 4
paper 回归: python3 paper_reversal_sim.py --scenarios-only | sha256sum
           = 3d16632c458bb969dd9dd379fd8e3d03df767e9d1827e1c05ba20e03945b0c2c   （与基线逐字一致）
```

## 2026-09-12 — 修复 F2：破位反手/追火分支的 `ZoneInfo` 未绑定（`UnboundLocalError` 中断整轮）

**缺陷（代码 + 实证双确认）**：`_r_cycle.py::run_cycle()` 的破位反手/追火分支构造 `hedge_fire` 时使用
`ZoneInfo(city.get("timezone", "UTC"))`（修复前 L1710）。而该名字在**本函数作用域内的唯一绑定**位于 TAF 块内部
（`if taf_rec is not None:` → `if valid_iso and taf_c is not None:` → `try: from zoneinfo import ZoneInfo`，L1650）
⇒ 站点**无 TAF**（或 TAF 与当日 local date 不符，走 `market_rank1` 回退）时该块根本不执行、名字未绑定，
**恰在持仓已被风控割肉清仓之后、正要发反手 hedge fire 的那一刻**抛 `UnboundLocalError`，整轮被中断（其余腿顺延）。
属**潜伏缺陷**：生产日志 `UnboundLocalError=0`／`cycle_error=0`／`breach_detected=0` ⇒ 从未触发；
**首次出现"追火/反手 + 该站无 TAF"必然炸**，且时序最坏（浮亏已实现、对冲腿未发出）。

**修法（最小、纯加法，+5 行 / −0 行）**：在 `run_cycle()` docstring 之后、函数顶部自绑
`from zoneinfo import ZoneInfo`（含成因注释），风格与 F1（`consensus_entry_fire`）及 sleeve 调用点热修一致。
**不触碰**任何判定 / 阈值 / 门序 / 窗口逻辑，**不触碰**"下一档桶 `buy_yes_next`"通道代码。
有 TAF 时同名解析、`hedge_fire` 逐字不变；无 TAF 时不再炸。

**证据（原始输出见 `/tmp/preyes_f2_fix_report.md`）**：
- **基线复现**：worktree `ccbd0c8` 上构造"破位 + 无 TAF（`market_rank1` 回退）"场景 ⇒
  `_r_cycle.py:1602` 抛 `UnboundLocalError: cannot access local variable 'ZoneInfo'`；修复前工作区同场景在
  L1710 抛同一异常（原文 traceback 留档）。修复后同场景正常产出 `hedge_fire`，`local_fire_time`
  可被 `datetime.fromisoformat` 解析且时区正确（Paris `+02:00` / Tokyo `+09:00`），`fire_attempt` 审计行落盘。
- **行为不变**：有 TAF 的同一场景，`hedge_fire` 与基线 `ccbd0c8` **逐字相同**（JSON 字节级一致，已冻结为
  回归 golden `tests_breach_hedge.py::TAF_GOLDEN`，并与 worktree 原始输出程序化比对通过）。
- **穷举绑定核查**（AST + 分支守卫精确判定）：`_r_cycle.py` 共 **5** 个 `ZoneInfo` 使用点；修复前
  **4/5 SAFE、1 处 UNSAFE（=L1710）**；修复后 **5/5 SAFE**。另两处（`target_dates_by_icao` L129 /
  `_rule_is_local_today` L156）的绑定紧邻使用点、同一 block 恒先于使用执行 ⇒ 无需改动。
- **测试**：新增 `tests_breach_hedge.py`（3 例：无 TAF 不抛且产 fire、有 TAF 逐字等于基线 golden、
  两路径仅差 `ref_extreme`：TAF 33.0 vs METAR 32.5）。全套件 PASS：
  `tests_consensus_lock` **15/15**、`tests_cycle_consensus_lock` **4/4**、`tests_breach_hedge` **3/3**、
  `tests_port` **33/33**、`tests_live` **51/51**、`tests_fill_gate` **6 gate scenarios**、
  `tests_reversal` **24/24**、`tests_sleeve_signal` **13/13**、`tests_sleeve_wiring` **4/4**、
  `tests_market_adapter` **4/4**（全部 exit 0）。
- **paper 回归**：`paper_reversal_sim.py --scenarios-only` 输出 sha256 在
  `ccbd0c8` / 修复前工作区 / 修复后工作区 **三者完全一致**（`3d16632c458bb969…`）⇒ 该路径不经过本分支，
  **无行为差异**（无需归因）。
- **变异测试**：删掉新增绑定 ⇒ `tests_breach_hedge.py` 必失败
  （`UnboundLocalError: cannot access local variable 'ZoneInfo'`，probe traceback 落在 L1710）；
  被删文件 blob 与 HEAD 完全一致（`c881ec4e…`）证明只删了本修复、未误伤他处；
  **逐字还原**后 sha256 回到 `a2f9dae4…`、`git diff` 仍为 `+5 / -0`、回归用例重新全绿。

**行为影响 / 风险**：唯一行为变化 = "无 TAF 站点的首次追火/反手不再中断整轮"。
不修等价于该站点当天破位后**永久失去对冲腿**且每轮 `cycle_error`。
未引入任何策略/阈值/预算变更（`fire_budget_usdc` = 10.0 与 `consensus_lock.order_budget_usdc` = 10.0 的既有
对齐保持不变）。

## 2026-09-12 — 新增并行通道 `buy_yes_next`「下一档桶廉价入场」（**代码默认关闭，config 显式开启**）

**决策依据（实测，非推断）**：`/home/da/桌面/poly-yes2/preyes_param_sim_20260912.md` §4/§5/§6。
既有目标桶通道的非价格门**全部通过**时，目标桶 ask 已被市场定价到 **0.81–0.99**，被最后一门
`yes_max_ask=0.75` 挡死 ⇒ 8 小时 **零成交**（`entry_count=0`）；时间对齐证明 TWAP/instant 放宽
的**边际新增机会 = 0**（几十秒自解），唯一能多出机会的参数是 cap，但那条路是"追高"、两种口径
EV 皆负（−10%~−25%）。结论：要「极高胜率 + 廉价入场」必须**改入场对象** ⇒ 在"下一档桶"报价
低廉时直接建仓该桶。

- **新通道 `buy_yes_next`**（并行、优先评估）：触发前置 = 与既有通道**逐字一致的非价格门**
  （站点频次 → 时间窗 → 速度/变率 → 预期极值桶位已确认 → 共识 **rank1**），价格约束换成该通道
  **自有且独立**的窗口 `(next_entry_min_ask, next_entry_max_ask]`（默认 **0.20 不含 / 0.32 含**）。
  **区间外 ⇒ 彻底弃单**：不降级、不挂被动单、不回退既有窗口。
  > 语义替代（有意为之）：下一档桶的 **twap/instant 价格子门**（0.26/0.25/0.15）是既有目标桶
  > 通道的过滤器，新通道用自有窗口替代它 —— 目标桶通道的窗口/门序/判定价逻辑**一字未改**。
- **与既有通道的关系**：新通道命中 ⇒ 既有通道该 tick 不下单；新通道弃单 ⇒ 既有目标桶通道按原
  逻辑（窗口仍为 (0.45, 0.75]）独立判定。**预算隔离**：新通道用
  `next_entry_budget_pct`（默认 0.5）× fire 预算；既有通道只用剩余额度（`15 − 7.5 = 7.5`）。
  会话上限沿用既有 `max_fires_per_session` 语义（不新增超出既有上限的并发）。
- **腿级窗口贯通到 live 端口 taker 带门**：新通道腿自带 `floor`/`cap`，`LivePort.fill_mode`
  以**腿自带窗口**判定（`entry_channel == "next_bucket"`），cfg 的 `yes_min_ask/yes_max_ask`
  动不了它；腿窗口缺失/非法 ⇒ `leg_window_unusable` **fail-closed**（绝不回退成 cfg 窗口）；
  仅"完全无腿窗口"才按规范回退 cfg。ladder intent 现在透传 `floor`/`entry_channel`（加法式）。
- **审计**：`intent`/`submit` 与 `data/live_events.jsonl` 新增 `entry_channel`
  (`next_bucket`/`target_bucket`)、`leg_window`、`window_source`、`next_entry_window`；
  通道字段由**专用入参**盖章（合并 `audit_extra` 之后），外部 `audit_extra` 同名值先被剔除
  ⇒ **不可伪造/翻转**。`data/yes2re_events.jsonl` 的 `fire` 行同样带通道字段。
- **配置**（`consensus_lock` 块）：`next_entry_enabled` / `next_entry_min_ask` / `next_entry_max_ask` /
  `next_entry_budget_pct`。`strategy_consensus_lock.py::DEFAULT_CONFIG` 同名键但
  **`next_entry_enabled: false`**（代码默认保守）；本仓 `config/yes2re_reversal.json` 显式开启。
- **引擎入口 fire 构造提取为 `_r_cycle.consensus_entry_fire(...)`**（可测的生产分支）：两通道的
  腿/窗口/预算/审计字段在此一处组装；既有目标桶通道的 fire 内容逐字不变（仅新增 `entry_channel` /
  `next_entry_window` / 预算量化）。顺带修一处**绑定缺陷**：HEAD 的入口 fire 分支引用 `ZoneInfo`，
  而该名字只由 TAF 块内的一处 local import 绑定（同一函数作用域）⇒ **有 TAF 时正常、无 TAF
  （`market_rank1` 回退）时 `UnboundLocalError` 并中断整轮**（同类缺陷在本仓已有先例：sleeve 调用点
  的热修注释）。提取后函数自行绑定 `ZoneInfo` ⇒ 常规行为不变、无 TAF 时不再炸。**未动**：破位反手
  fire 分支（`_r_cycle` 内同一 `ZoneInfo` 形状）保持原样，属既有缺陷，需操作者另案决定。
- **验证**（原始输出见 `/tmp/preyes_next_bucket_impl_report.md`）：`tests_consensus_lock` 15/15、
  `tests_cycle_consensus_lock` 4/4、`tests_port` **33/33**、`tests_live` 51/51、`tests_reversal` 24、
  `tests_fill_gate` 6、`tests_sleeve_signal` 13、`tests_sleeve_wiring` 4、`tests_market_adapter` 4；
  边界矩阵 0.199/0.200/0.201/0.319/0.320/0.321 ⇒ 弃/弃/入/入/入/弃（策略层 + 端口层各一套）；
  带外 `execute_leg` 调用数 = 0；既有通道 33 场景投影与 `ccbd0c8` **逐场景一致**（默认关闭 0 差异；
  开启时 5 处差异全部是"新通道成交"）；`paper_reversal_sim.py --scenarios-only` sha256
  `3d16632c…` **与基线逐字一致**；3 组变异（区间外降级 / 闭区间 / 忽略预算隔离）全部被对应用例捕获
  并逐字还原（sha256 复原）。
- **风险（如实记录）**：EV 前提「市场系统性低估该桶」**未经结算验证**（8h 样本无法证实，见报告
  §5.3）⇒ 通道默认关闭、部署保持小额（本仓 = 0.5 × 15 USDC = 7.5 USDC 名义/次，且受 live 端
  `LIVE_FIRE_BUDGET_USDC`/`LIVE_MAX_CAPITAL_USDC` 约束）；单笔最大损失 = 该通道预算；一键关闭 =
  `next_entry_enabled: false`（立即回到今日行为，既有通道逐行不变）。

## 2026-09-12 — 修 settle_failed 根因（裸 socket 读超时中断整轮结算；同步自共享引擎）

- **同缺陷**：`market_adapter._fetch_json` 未处理裸 `TimeoutError`（socket 读超时不包成 `URLError`）→ 穿透 `fetch_market_resolution` 的窄捕获 → `_r_cycle` 记 `settle_failed` 并**中止整轮结算**。两仓 `market_adapter.py` md5 相同，属共享引擎缺陷。
- **修复（操作者选 A）**：`_fetch_json` 增 `except TimeoutError → RuntimeError` ⇒ 超时按 unresolved 返回 `None`，由 `settle_poll_seconds` 下轮重试，不再中断整轮。
- **验证**：新增 `tests_market_adapter.py` 4/4；本仓全绿（`tests_port` 31/31、`tests_live` 51/51、`tests_reversal`、`tests_fill_gate`、`tests_consensus_lock` 11、`tests_cycle_consensus_lock`、`tests_sleeve_*` 13/4）。

## 2026-09-12 — CRITICAL: 修 neg_risk 签名域丢失（实盘 fire 100% 下不出去）+ 引擎预算与 live 上限对齐

**背景（真实链路演练实测暴露，非推断）**：用引擎自身链路 `_r_cycle._paper_fire` 在真实市场下单，连续 3 次被 CLOB 拒绝：
```
400 {"error":"invalid POLY_PROXY signature"}
```
链路本身正常（`plan_fire_cycle` → `send_fak limit=0.72 shares=10` → 端口 taker 决策正确），
失败发生在签名域：**天气桶市场是 neg-risk 市场**，而引擎的梯子缓存里没有 `neg_risk` 字段。

- **根因**：`_r_cycle._normalize_snapshot()` 只透传 `best_ask/best_bid/tick_size/asks/bids`，
  丢掉了 `execution/market.py` `BookView` 里已有的 `neg_risk`（和 `min_order_size`）。
  `live/port.py` 于是 `bool(book.get("neg_risk"))` = False ⇒ 按**非** neg-risk 交易所签名
  ⇒ CLOB 判签名无效。**只要市场是 neg-risk，实盘 fire 就永远发不出去**（静默失败）。
- **修复 1（根因）** `_r_cycle._normalize_snapshot`：透传 `neg_risk` / `min_order_size`。
  这两项纯元数据，`paper_match_fak` 不读 ⇒ paper 行为不变（`paper_reversal_sim --scenarios-only`
  输出 sha256 `3d16632c…` 与基线逐字一致，已实测）。
- **修复 2（纵深防御）** `live/port.py LivePort.resolve_neg_risk()`：盘口缺该字段时用 live 客户端
  重取一次盘口（CLOB `/book` 带 `neg_risk`，与 `refetch_book` 同源）并按 token 缓存；
  **仍取不到 ⇒ 拒单（`neg_risk_unknown`，fail-closed）**，绝不按错误签名域硬发。
  审计行新增 `neg_risk` / `neg_risk_source`。
- **修复 3（配置对齐，服务器 .env）**：引擎 fire 预算来自 `cfg.fire_budget_usdc`（config=**15**），
  而 live 端口用 `check_limits(notional=15, fire_budget=LIVE_FIRE_BUDGET_USDC=10)` ⇒
  `15 > 10` ⇒ **每次 fire 被 `limit_fire_budget` 拒绝**。故在服务器 `.env` 增加引擎级覆盖
  `YES2RE_FIRE_BUDGET_USDC=10`（尊重操作者设定的单笔实盘上限 10，且不改共享 config）。
- **验证（真实资金，磨损可接受）**：修复后重跑引擎链路演练 ⇒ **真实成交**
  `Shanghai 28°C`：`buy_yes_new send_fak limit=0.66 shares=10.00 → filled 10.153845 @0.66`
  （账本记账 cost 6.701538 USDC），随后 FAK 卖回 `10.15 @0.56`（`0xc84665f7…`），
  账户回零（balance 51.375982→50.219453，positions 0，open_orders 0），净磨损 1.156529 USDC。
- 测试：`tests_port` 31/31、`tests_live` 51/51、`tests_consensus_lock` 11/11、
  `tests_cycle_consensus_lock` 集成全绿、`tests_fill_gate` 6 场景、`tests_reversal`、
  `tests_sleeve_signal` 13/13、`tests_sleeve_wiring` 4/4。
- 说明：演练写入生产 `data/live_events.jsonl` 的 4 行（2 笔订单，00:41:31 BUY / 00:41:39 SELL）
  是**演练单**，非策略 fire。

## 2026-09-12 — 统一 YES 吃单带为 0.75（fire 范围 ≡ live taker 带门）
- `config/yes2re_reversal.json`：`consensus_lock.yes_max_ask` **0.80 → 0.75**；`strategy_consensus_lock.py` 内置换默认同步 0.75。
- 原因（操作者拍板）：live 端口 taker 带门读 `strategy.yes_max_ask`（= 0.75），而 PreYes 入场顶价原为 0.80 ⇒ fire 限价落在 **(0.75, 0.80]** 时实盘会 `yes_price_above_band` 跳过（"fire 却不成交"），纸面/实盘同区间分歧。统一后三处一致：`consensus_lock.yes_max_ask` = `strategy.yes_max_ask` = `risk_control_yes_cap` = **0.75**。
- `tests_port.py`：配置防漂移 GOLDEN 快照同步更新。
- 验证：`tests_port` 31/31、`tests_live` 51/51、`tests_consensus_lock` 11/11、`tests_cycle_consensus_lock` PASS、`tests_fill_gate` 6 场景、`tests_reversal` PASS、sleeve 13/13 + 4/4。

## 2026-09-12 — 激进吃单 (FAK Taker) 精度修复 + Nautilus Trader v2.0 规范对齐 + 全量 142 单测通过

- **激进吃单 (FAK Taker) 生产精度对齐 (`live/v2_transport.py`)**：
  - 同步 Nautilus Trader v2.0 Polymarket 官方适配器规范与 Polymarket CLOB v2 最新限额：
    - `OrderType.FAK` 市场买单 (`BUY`) 的 `maker_amount`（USDC 名义金额）严格保留 2 位小数（`Decimal('0.01')`，向下取整）。
    - 市场卖单 (`SELL`) 的数量严格保留 4 位小数（`Decimal('0.0001')`，向下取整）。
    - 彻底修复 `400 invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals` 报错。
  - **FAK 无对手盘静默撤单**：捕获 `400 "no orders found to match with FAK"` 响应并作为 0-fill 正常结算，消除残留风险虚假报警。
  - **腿级独立决策与严格放弃 (Take-or-Nothing)**：YES 腿在价格区间 `(0.45, 0.75]` 内执行 FAK 吃单，区间外严格放弃（`status="skip"`），**绝对不降级为被动挂单 (never passive fallback)**；NO 腿独立依据自身盘口 ask <= cap 决定是否吃单。
- **PreYes 稳了参数基线与回归测试套件全面对齐 (`tests_port.py`, `tests_live.py`)**：
  - 更新 `tests_port.py` 的基准黄金配置与策略期望，使其与 PreYes 实际配置（`fire_budget_usdc=15.0`, `paper_initial_capital_usdc=700.0`, `yes_min_ask=0.45`, `yes_max_ask=0.75`）完全一致。
  - 优化 `tests_live.py` 中的限价与可成交性测试，防止由于 PreYes 的 0.75 上限导致 0.80 价格覆盖误触发价格超限。
  - **测试全绿**：`tests_port.py` (31/31 PASS)、`tests_live.py` (51/51 PASS)、`tests_consensus_lock.py` (11/11 PASS)、`tests_cycle_consensus_lock.py` (2/2 PASS)、其余核心回测套件 (47/47 PASS)。总计 142 个测试用例在 Windows 与 WSL Linux 双环境下保持 100% 通过。

## 2026-09-11 — LIVE 执行层 Phase 1-3b 完整迁移 + CLOB v2 适配 + PreYes 稳了策略兼容

- **执行端口化架构 (`live/port.py`, `_r_cycle.py`)**：
  - 遵循 **live ≡ paper** 核心原则（同策略、同判定、同基建、同账本），执行差异封装于成交通道：`PaperPort` 走内存 FAK 撮合，`LivePort` 走 CLOB v2 真实下单与对账。
  - 在 `_r_cycle._paper_fire` 接入 `get_port(cfg)`。在 `mode=live` 且未满足三重闸门时抛出 `PortRefused` 并记录 `fire_port_refused` 审计事件，**绝对不开仓、绝不静默降级为 paper**。
- **PreYes 稳了策略专用腿兼容 (`live/order_plan.py`)**：
  - 适配 `strategy_consensus_lock.py` 独有的 `buy_yes_lock` 腿，将其纳入 `BUY_DIRECTIONS`，并映射上限至 `cfg['yes_max_ask']`（0.75），完美兼容稳了策略的高胜率锁定信号。
- **CLOB v2 全面迁移 (`live/v2_transport.py`, `live/clob_client.py`)**：
  - 淘汰已失效的 CLOB v1，全面采用 `py-clob-client-v2`。
  - 凭据显式传入，撤单统一调用 `cancel_orders([id])`，查单使用 `get_open_orders()`。
  - 下单前必须重新获取盘口并夹紧限价（`clamp_limit` + `refetch_book`），避免 `order crosses book`。
  - 成交对账轮询获取真实成交量和均价。撤单失败重试 3 次，仍失败标记 `residual_risk=True`。
  - 25 个写操作方法默认全部装载运行时抛异常哨兵，仅最小权限开放 `post_order` 与 `cancel_orders`。
- **生产级三重安全闸门 (`live/risk_gate.py`, `live/submit.py`)**：
  - 1) CLI / 环境变量 `YES2RE_LIVE_ENABLE_SUBMIT=1`
  - 2) 机器级标志 `LIVE_SUBMIT_ENABLED=1`（不写进 `.env`，防止误起）
  - 3) 当日 UTC 动态短语 `YES2RE_LIVE_CONFIRM=SMOKE-<YYYY-MM-DD>`（跨日自动失效，防止无人值守放量）
- **单配置双实例环境覆盖 (`_r_state.py`)**：
  - `config/yes2re_reversal.json` 保持 `mode: "paper"` 防止误触。
  - 支持 4 个环境变量覆盖：`YES2RE_MODE`、`YES2RE_FIRE_BUDGET_USDC`、`YES2RE_MAX_OPEN_POSITIONS`、`YES2RE_INITIAL_CAPITAL_USDC`。非法值严格 fail-closed。
- **网络与海外 VPS 直连兼容 (`market_ws_transport.py`)**：
  - 移植 `resolve_default_proxy()` 及直连 TLS，自动识别海外无代理直连环境与本地开发代理环境。
- **测试套件与运维工具**：
  - 新增 `tests_port.py` (16/16 PASS) 与 `tests_live.py` (50/50 PASS)。原有 6 个策略与流程测试套件 100% 保持通过。
  - 引入 `DEPLOY_RUNBOOK.md`、`ops/PENDING.md`、`ops/repair_log_live.md`、`run_live.sh`、`scripts/analyze_events.py`、`scripts/monitor_live.py`。


- **`reversal_strategy.py` fire window switched from single-edge bounds to
  inclusive local hour intervals.** One-bucket reversal fires now gate on:
  - HIGH: local `13 <= hour <= 17` (was `hour >= 14`, no upper bound)
  - LOW: local `1 <= hour <= 9` (was `hour <= 10`, no lower bound)
- Constants: `HIGH_FIRE_LOCAL_HOUR` / `LOW_FIRE_LOCAL_HOUR_END` removed →
  `HIGH_FIRE_LOCAL_START=13` / `HIGH_FIRE_LOCAL_END=17` /
  `LOW_FIRE_LOCAL_START=1` / `LOW_FIRE_LOCAL_END=9`. `hour_ok` now takes the
  four window bounds and enforces `start <= h <= end` per direction. Both `arm`
  and the pre-fire `hour_not_in_window` gate use the same window. `prune`
  low-zombie sweep follows the new low end (9).
- Config keys: `high_fire_local_hour` / `low_fire_local_hour_end` →
  `high_fire_local_start` / `high_fire_local_end` /
  `low_fire_local_start` / `low_fire_local_end` (`config/yes2re_reversal.json`
  updated; old keys removed).
- **Rationale:** the daily extreme (and the capped peak-tick reversal this
  strategy sells) forms inside the window, not outside it. Real losses from
  out-of-window fires: mexico-city low 02:02 (LOST), SF 9/7 01:00 local fire
  (open, floating underwater) — both broke the reference at hours the peak
  window does not span. Fires observed off-window are now suppressed.
  Direction-specific windows also stop one city's LOW-break drift from firing
  into the afternoon or a HIGH from firing predawn. On-window losses of the
  chengdu class are a separate open question (bucket-break confirmation) and
  are not addressed by this change.
- Verified: `python3 tests_reversal.py` 16/16 PASS before and after (all 16
  scenarios keep passing under the interval semantics); window-boundary
  assertion (high 12/18 rejected, 13..17 accepted; low 0/10 rejected, 1..9
  accepted) green.

## 2026-09-07 — F-market unit audit + boundary-confirmation margin

- **Polymarket unit rules audited & documented** (`research/common.py`
  `c_to_market_unit` docstring):
  - Buckets: US cities 1-2°F integer buckets; EU/Asia cities 1°C buckets.
  - Resolution: Wunderground station "Daily Observations" — finalized daily
    extreme at whole degrees, post-QC (NOT intraday METAR, NOT the NWS CLI
    summary, NOT the WU "Day High & Low" box). Stated precision rule is
    truncation for °C buckets (23.9°C → 23).
  - METAR has NO native °F anywhere (global °C, incl. US ASOS). US ASOS
    displays whole °F via rounding — our °C→°F round matches that display
    convention; the Polymarket truncation rule applies to the °C-bucket side
    where whole-degree METAR already aligns naturally.
- **F-market break-confirmation margin** (`reversal_strategy.py`,
  `break_confirm_margin_f` default 1.0, config key added): a °F-market fire
  requires the whole-degree converted extreme to clear the broken-bucket
  boundary by ≥1°F. Motivation: SF 9/4 low misfire — METAR 14°C converted to
  57.92°F < 58 (break), but Wunderground finalized 58.x°F (no break): METAR
  whole-°C granularity spans ±0.9°F after conversion and the finalized daily
  extreme can differ ~1°F from the intraday METAR extreme.
- **Back-test on real °F fills (2026-09-05→07, 5 trades)**: margin=1 would
  have kept SF 9/6 (YES@0.52 WON) and SF 9/7 (open), and filtered chicago
  9/5 low (NO@0.97 LOST — the SF-class false break) — but it would also have
  filtered atlanta 9/6 low (NO@0.92 WON + YES WON) and austin 9/5 high
  (YES@0.98 WON), both genuine near-boundary breaks. Trade-off is documented
  and tunable: 0.0 = legacy float behavior (fire all near-boundary breaks),
  1.0 = filter all <1°F-deep breaks (default, prevents SF-class false
  breaks at the cost of genuine near-boundary fills). C markets are exempt
  (whole-degree truncation aligns exactly).

## 2026-09-04 — Fire deadlock fix; WS live feed; paper-ledger fix (audited)

- **obs sanity window (was: absolute 180 s age gate → structurally zero fires).**
  METAR/SPECI obs_time age swings 0-60 min on hourly cadence (US AWS publish
  ~7 min early); `require_fresh_obs_seconds=180` made `stale_obs` block every
  fire. Replaced with sanity window `max_obs_lookback_seconds=5400` /
  `max_obs_future_seconds=900`: any NEW observation (deduped by
  `is_new_obs_time`) may fire unless the feed is >90 min behind or the stamp
  is >15 min in the future. First live fire within 27 min of deploy.
- **Full skip audit.** `_r_cycle` no longer silently drops skips: every
  re_skip / re_skip_yes / re_disarm is logged with reason/jump/consensus
  (silent skips previously hid the 0-fire deadlock).
- **NO cap 0.65 → 0.85** (broken-bucket NO redeems ~1.0; wider cap = fills);
  YES leg cap unchanged 0.48.
- **Universe: 10 → all 49 cities** (drop `active_icaos` allowlist; both high
  & low directions). `idle_metar_interval_seconds` 45 → 60 (49 cities = 3
  CheckWX batches; 4320 req/day < 5000 paid cap).
- **Market WebSocket live** (`market_ws_transport.py` stdlib-only WS client
  through the CONNECT proxy + `ws_bridge.py` daemon thread). 2000+ tokens
  subscribed; fresh (<5 s) WS LocalOrderBook snapshots overlay the ladder
  cache (epoch-guarded, never clobbers newer REST data); auto-reconnect
  5/10/30 s; REST /books remains the correctness backbone (the public market
  channel is near-frozen per py-clob-client #292 — WS is an accelerator).
- **Paper ledger fix.** `release()` no longer clamps total debit to zero —
  a negative debit is realized profit (equity = initial − debit). The clamp
  had silently discarded +52.80 USDC of paper profit (cost 49.06 vs payout
  101.86). `total_debit_usdc()` now reads negative values directly instead of
  through the `parsed >= 0` filter.
- **Audit hardening (pi + omp cross-review 2026-09-04):** `ensure_tokens`
  and `mark_disconnected` thread-safety (dict-size-change race during
  reconnect); `_ws_pump` epoch comparison made real (docstring now honest).
- Verified: 7/7 scenario tests; equity 1000 → 1052.79 after first US market
  settlements (NO legs 4/4 wins; one YES lottery leg lost).

## 2026-09-03 — Dual-rate paper runner; no σ; real-API soak

- **Zero σ / bias / fade-NO / dead-NO / BUY-YES** on the run path.
- Dual-rate METAR/books (ARM ~8s; idle METAR ~45s + consensus books ~30s).
- Dual-source METAR (CheckWX + AWC); C/F via `c_to_market_unit`; Gamma rules cache 20min.
- Modules: `runner_impl.py`, `_r_globals.py`, `_r_state.py`, `_r_data.py`, `_r_cycle.py`, `_r_exec.py`.
- **10-min paper soak (real CheckWX+Gamma+CLOB):** 49/49 METAR, 98 rules, Atlanta+Denver ARMed, 0 FIRE, capital 1000 USDC, no cycle_error.

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Not merged: TAF/σ arms. This repo is strategy + paper runtime.
