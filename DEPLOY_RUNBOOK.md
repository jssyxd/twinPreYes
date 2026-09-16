# Weatherbotyes2re — Paper Runner 部署 Runbook

Live real‑data paper‑trading deployment + 每30分钟自动化巡察 / Telegram 汇总 .

## 1. 概览

- **引擎**: 纯 Python 3.13 (stdlib only), Polymarket 日度天气市场的 METAR‑vs‑consensus 单桶 reversal 策略。
- **模式**: `paper` only — 无真实钱包/下单。`reversal_runner.py` 硬性拒绝非 paper 模式。
- **初始资金**: 1000 USDC (paper ledger). 单笔 fire 预算 ≤ 20 USDC.
- **活动城市 (10)**: amsterdam, atlanta, austin, london, madrid, mexico‑city, paris, sao‑paulo (SBGR), tokyo (RJTT), toronto (CYYZ).
- **数据源**: CheckWX(key, 从 .env) + AviationWeather (无key) 双源 METAR; Gamma REST 规则发现; CLOB REST 只读 books (WS 未启用, REST seed 模式).

## 2. 运行中的组件 (全部在 /home/da/桌面/poly-yes2/weatherbotyes2re)

| Component | 形态 | 状态 |
|---|---|---|
| paper runner | hub 常驻服务 `paper_runner` | running (pid 动态), 持久+重启 on‑failure |
| reversal‑watch | Hermes cron, 每30min, `pi`巡察 | active, `*/30 * * * *` |
| reversal‑report | Hermes cron, 每30min, Telegram 汇总 | active, `*/30 * * * *` |
| 观测脚本 | `~/.hermes/scripts/reversal_observe.py` | deterministic fact block |
| 巡察脚本 | `~/.hermes/scripts/reversal_watch.py` | observer + `pi` 反幻觉 verdict |

### 启动/重启 runner
```bash
# 通过 hub (本 omp 会话内)
# paper_runner 已由本部署启动；如需重启: hub restart paper_runner
# 手动前台冒烟:
cd /home/da/桌面/poly-yes2/weatherbotyes2re
python3 tests_reversal.py                                   # 7 场景 (离线合成)
python3 reversal_runner.py once --config config/yes2re_reversal.json   # 一个真实数据 cycle
python3 reversal_runner.py run  --config config/yes2re_reversal.json --max-seconds 600
```
日志/健康/状态输出:
- events: `data/yes2re_events.jsonl` (arm/disarm/fire/settle/rules_refresh/cycle_error…)
- health: `data/yes2re_health.json` (含 `feed.*` 观测面)
- state:  `data/yes2re_state.json`

## 3. 30‑min 自动化

- **reversal‑watch (巡察)**: 每30min. observer 读取 data/* → 确定性 fact block → `pi -p --no-tools` 基于 fact block 反幻觉审查, 输出 JSON verdict {status, verified, anomaly_flags, recommendation}. Deliver: local (日志). 任何 flag 都是带具体 field→value 的可查证据.
- **reversal‑report**: 每30min Hermes summarizer 读 observer 输出 → 简短中文 Telegram 到 `telegram:liudi`.

手动触发单次 (若想当下跑一次, 不一定等 tick):
```bash
hermes cron run reversal-report   # ~instant (observer only)
hermes cron run reversal-watch    # 注意: 包含 pi (~90–160s), cron run CLI 会阻塞到 local delivery ack;
                                  # 真正调度器在 :00/:30 独立执行, 无此阻塞.
python3 ~/.hermes/scripts/reversal_observe.py | python3 -m json.tool   # 只看 fact block
```
调度器状态: `hermes cron list` / `hermes cron status` / `hermes cron runs <job>`.
Telegram 通道连通自检: `hermes send --to telegram:liudi "hi"`.

## 4. telemetry / 观测面 (watcher 判据)

见 `reversal_observe.py` 输出的 JSON: `runner`(alive/file_age/mode), `feed_health`(armed/open_positions/capital/metar_cities_stale/books_max_age/websocket·clob·gamma/rules_failures), `anomalies`(event_anomalies/stale METAR/rules failures), `activity_30m`, `trades`.
- METAR obs age > 3600s → 标 stale (远端站正常可 1–2h; 若 ARM 且 stale 近数小时即 feed 死亡信号).
- rules discovery 间歇性 `TimeoutError` → 重试即可; 持续失败才是 Gamma 故障.

## 5. 运维命令

```bash
cd /home/da/桌面/poly-yes2/weatherbotyes2re
python3 reversal_runner.py status --config config/yes2re_reversal.json   # 打印 armed/fired/open/capital
tail -50 data/yes2re_events.jsonl
jq . data/yes2re_health.json
```
资金/持仓核实: 见 health 的 `capital_initial_usdc`/`debit_usdc`/`open_positions` 与 state 的 `positions{}`, `entry_count`.

## 6. 待办 / 已知点
- Gamma 日度契约的**今日 local date** 规则发现存在间歇性 TimeoutError — 已放宽到 12s/req, 150s deadline; 属重试性, 不由巡察硬判定为故障.
- `websocket_market_data` 已实现但本 paper 部署用 REST seed (health 内显式 `websocket.deployed=false`); 若需 WS book 推送, 在 `_r_cycle.refresh_books` 前置 MarketStream 接入.
- CheckWX key 在 `~/.env`; 轮换后更新该文件 (勿提交, gitignored).
- 巡察/汇总脚本存放于 `~/.hermes/scripts/` (Hermes cron 要求), 非仓库内; 改动后 `hermes cron` 引用同名仍生效.
- 若本 omp 会话/机器重启, `paper_runner` hub 服务与 hermes cron gateway 均需确认起来 (本轮已 persist runner; gateway pid 1596 为系统级).

## 7. LIVE (CLOB v2) 部署与观测镜像 — Phase 3b

> 状态：**已部署、观察模式（闸门未开，绝不下单）**。my155 上跑的是 `yes2re-live`
> （`YES2RE_MODE=live`，端口闸门全未设 → 每次 fire 记 `fire_port_refused` 不开仓）；
> 本机仍跑 `yes2re-paper` 作对照组。开启真实下单的精确步骤见 `ops/PENDING.md` §D-1。
> 观测镜像实际实现 = `~/.hermes/scripts/live_mirror_sync.py`（一次 tar 取回 + 远端只读 reconcile），
> 由 `live-observe.py`/`live_triage.py` 调用；`rsync` 仅为备选。本仓库不改任何服务/cron/`.env`。

### 7.1 前置事实

- Polymarket 已于 **2026-04-28** 迁 CLOB **v2**：旧 `py-clob-client`（v1）下单会被拒（`invalid order version`）。
  必须用 `py-clob-client-v2`（`import py_clob_client_v2`），venv 位于 my155 的 `/root/live-probe-v2/.venv`。
- 账户实测：`signature_type=1` (POLY_PROXY) + funder `0x6f7d…` + 现有 API 凭据可被接受（`status=live`）。
- 只读自检（零下单）：`/root/live-probe-v2/.venv/bin/python live/v2_transport.py --version` → 应打印
  `clob server version: 2`；`--open-orders` → `open orders: 0`。

### 7.2 切 live 的闸门（三者缺一：`fire_port_refused` 事件，不开仓，**不会降级成 paper**）

```bash
# ① 引擎模式 —— **不要**改 config 文件（两个实例共用同一份，防止策略漂移）
#    用环境变量：YES2RE_MODE=live        （config 文件本身永远无法选中 live，安全锁保留）
#    两个实例的额度也用 env 区分（策略参数完全相同）：
#      YES2RE_FIRE_BUDGET_USDC=<正数>        override cfg['fire_budget_usdc']
#      YES2RE_MAX_OPEN_POSITIONS=<正整数>     override cfg['max_open_positions']
#      YES2RE_INITIAL_CAPITAL_USDC=<正数>      override cfg['paper_initial_capital_usdc']
#        ↑ 实盘实例填**真实账户余额**（如 51.713622）：账本初始资金以真实资金起算，
#          镜像出的"当前权益"因此与真实余额同源（仅在新 state/无借记时用于初始化）
# ② 服务侧三闸门（写进 systemd 单元，绝不写进 .env）
#    Environment=YES2RE_LIVE_ENABLE_SUBMIT=1
#    Environment=LIVE_SUBMIT_ENABLED=1
#    Environment=YES2RE_LIVE_CONFIRM=SMOKE-$(date -u +%Y-%m-%d)   # 日期短语，过期即拒
# ③ 硬上限（同一单元内，按需放大）
#    Environment=LIVE_FIRE_BUDGET_USDC=12
#    Environment=LIVE_MAX_OPEN_POSITIONS=10
#    Environment=LIVE_MAX_CAPITAL_USDC=50
```
`YES2RE_LIVE_CONFIRM` 是**日期短语**：服务跨过 UTC 零点后闸门失效 → 需重启单元（或改用
`systemd` 的 `EnvironmentFile` + 每日重载）。这是刻意的：连续无人值守放量属 Phase 4。

### 7.3 systemd 单元（镜像 `yes2re-paper`）

```ini
# /etc/systemd/system/yes2re-live.service   (与 yes2re-paper 逐行同构，仅 mode/venv/闸门不同)
[Unit]
Description=weatherbotyes2re LIVE (CLOB v2)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/weatherbotyes2re
EnvironmentFile=/root/weatherbotyes2re/.env
# ⚠ 不要把 YES2RE_MODE / 额度这类“实例选择”变量放进 .env：paper 与 live 共用同一份 config，
#   模式与额度只由单元里的显式 Environment= 行给（.env 内历史遗留的 YES2RE_MODE=live 若被导出，
#   paper 实例会被环境选中 live）。三个 override 变量与合法性见 live/README.md。
Environment=YES2RE_MODE=live
Environment=YES2RE_FIRE_BUDGET_USDC=12
Environment=YES2RE_MAX_OPEN_POSITIONS=10
Environment=YES2RE_LIVE_ENABLE_SUBMIT=1
Environment=LIVE_SUBMIT_ENABLED=1
Environment=YES2RE_LIVE_CONFIRM=SMOKE-CHANGE_ME_DAILY
ExecStart=/root/live-probe-v2/.venv/bin/python reversal_runner.py run --config config/yes2re_reversal.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```
> 注意：`reversal_runner.py` 入口当前硬拒非 paper 模式（Phase 3 的安全约束）；切 live 前需操作者
> 明确解除该入口限制（本仓库未改）。`live/port.py` 已按 `cfg["mode"]` 就绪。

### 7.3.1 两个实例的差异面（只有这三个）

| 变量 | 覆盖 | 说明 |
|------|------|------|
| `YES2RE_MODE` | `cfg['mode']` | `paper` \| `live`；非法值 → 启动即 `SystemExit`（fail-closed，不静默忽略） |
| `YES2RE_FIRE_BUDGET_USDC` | `cfg['fire_budget_usdc']` | 每笔 fire 预算；有限正数。**这也是策略侧 sizing 的唯一基数**（F-A 2026-09-12，见下） |
| `YES2RE_MAX_OPEN_POSITIONS` | `cfg['max_open_positions']` | 同时持仓上限；正整数 |
| `YES2RE_INITIAL_CAPITAL_USDC` | `cfg['paper_initial_capital_usdc']` | **实盘实例填真实账户余额**，使账本/权益以真实资金起算；有限正数 |

未设置时 `load_config` 输出与改动前逐字段一致；覆盖后 `_validate_config`（间隔/金额/模式）照常校验。
策略参数（`strategy` 子字典）**永远**来自共用的 config 文件，环境变量无法触及。

> **F-A（2026-09-12，已修）：`YES2RE_FIRE_BUDGET_USDC` 是唯一预算基数。**
> 引擎在构造策略时把**生效的** fire 预算注入策略 cfg（`_r_cycle._get_consensus_lock_strat`），
> 策略侧 sizing（`ConsensusLockStrategy.order_budget()`）优先读它；config 里
> `consensus_lock.order_budget_usdc` 降级为**已废弃**的兼容回退（仅在 fire 预算缺失时生效）。
> 因此"下一档桶通道 + 既有目标桶通道 ≤ fire 预算"的不变量对**任意** env 取值都成立，
> 部署时**不再需要**手工把 `order_budget_usdc` 与 env 对齐（对齐仍然无害）。
> 若把 env 调小/调大，两通道额度会随它一起缩放，不再出现"既有通道被静默少给"或"合计越界"。

### 7.4 观测镜像（复用**同一套**观察脚本与 cron）

my155 的 `data/` 是唯一真源；**镜像回本机后用同一套** `~/.hermes/scripts/reversal_observe.py` /
`reversal_triage.py` 与本机 cron 观测，**频率不变：每 30 分钟**；汇报内容不变：**①开仓 ②当前权益**。

```bash
# my155 → 本机（只拉观测面，不拉密钥；data/ 已在 .gitignore）
rsync -az --partial my155:/root/weatherbotyes2re/data/ \
      /home/da/桌面/poly-yes2/weatherbotyes2re-mirror/data/
# 本机观察（脚本不变、cron 条目不变，仅指向镜像目录）
WBY2RE_ROOT=/home/da/桌面/poly-yes2/weatherbotyes2re-mirror \
  python3 ~/.hermes/scripts/reversal_observe.py | python3 -m json.tool
hermes cron list | grep -E "reversal-(report|watch)"     # 频率仍 30min
```
镜像语义：
- `data/yes2re_health.json` / `data/yes2re_events.jsonl` / `data/yes2re_state.json` 与 paper 完全同 schema
  （端口模型不改状态字段），所以既有 watcher 判据（`runner.alive`、`feed_health`、`anomalies`、
  `activity_30m`、`trades`）**无需改动**即可用于 live。
- live 额外可见：`data/live_events.jsonl`（intent/deny/submit/cancel/residual_risk）与
  `fire_port_refused` 事件（闸门缺失、风控拒绝时）。
- 对账口径：`capital_initial_usdc`/`debit_usdc`/`open_positions` 与真实 CLOB 挂单/持仓应逐值吻合；
  不一致即停（`live/reconcile.py --json` 是独立只读对账入口）。
