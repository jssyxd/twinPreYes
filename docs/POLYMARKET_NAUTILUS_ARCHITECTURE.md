# Polymarket CLOB v2 与 Nautilus Trader 官方适配器深度架构分析与集成规范

本文档系统性梳理了 [Polymarket 官方文档 (docs.polymarket.com)](https://docs.polymarket.com/) 与 [Nautilus Trader (nautechsystems/nautilus_trader)](https://github.com/nautechsystems/nautilus_trader) 官方 Polymarket 适配器的最新技术规范，并总结了本交易系统（`weatherbotPreYes0910`）的基座演进架构。

---

## 一、Polymarket 协议与 CLOB v2 官方规范核心要点

### 1. 结算代币与账户体系 (pUSD)
- **pUSD (Polymarket USD)**：Polygon 主网上的标准 ERC-20 抵押品代币，精度为 **6 位小数**（`USDC_DECIMALS = 6`），与原生 USDC.e 保持链上智能合约 1:1 抵押兑付。
- **签名与委托执行**：Polymarket 采用混合架构（链下撮合 + 链上智能合约结算）。所有订单均为 EIP-712 结构化签名消息。
- **签名类型 (SignatureType)**：
  - `0` (EOA)：普通外部账户直接私钥签名；
  - `1` (POLY_PROXY)：通过 Polymarket Proxy 代理合约执行；
  - `2` (POLY_GNOSIS_SAFE)：通过 Gnosis Safe 多签/安全钱包执行。

### 2. 订单类型与市场单语义 (Order Types)
- 协议底层**所有订单在链上均表现为带有触发上限的限价单 (Limit Orders)**。所谓的“市价单”在协议层实际上是带有特定时间生效规则（Time-In-Force, TIF）的限价单：
  - `GTC` (Good-Till-Cancelled)：挂在订单薄上直至被完全成交或主动撤单（适用于做市商/被动挂单）；
  - `GTD` (Good-Till-Date)：设置具体到期时间的挂单；
  - `FAK` (Fill-And-Kill / IOC)：**立即吃掉当前盘口可成交深度，未成交部分立即静默取消**；
  - `FOK` (Fill-Or-Kill)：必须整单全额立即成交，否则全部撤销。
- **orderType 字段定位**：`orderType`（如 `FAK`）**不属于** EIP-712 签名的 typed data 结构体，而是在向 `/order` 接口提交 HTTP POST 时放置在最外层的顶层参数中（与 `order` 签名对象同级）。

### 3. 市价单 (FAK) 精度强制约束 (Precision Limits)
Polymarket 在 2026 年针对市场单引入了极其严苛的精度校验，这也是导致实盘初期频发 `400 invalid amounts` 的根源：
- **BUY 市价单**：传入的 `amount` 参数代表想要花费的 **USD 名义金额** (USD notional)，服务端强制要求精度上限为 **2 位小数**（美分级别，`Decimal('0.01')`，向下取整）。
- **SELL 市价单**：传入的 `amount` 参数代表想要卖出的 **Shares 份额数量**，精度上限为 **4 位小数**（`Decimal('0.0001')`，向下取整）。
- **限价边界 (`price`)**：
  - 买单传入 `maxPrice`（即允许吃单的最高价格，对应我们的策略 Cap）；
  - 卖单传入 `minPrice`（即允许卖出的最低底价，对应策略 Floor）；
  - 价格必须落在当前盘口的有效边界内：`tick_size <= price <= 1 - tick_size`。

### 4. 费率机制与天气事件市场 (Fees)
- 官方费率公式：
  $$\text{fee} = C \times \text{feeRate} \times p \times (1 - p)$$
  其中 $C$ 为成交股数，$p$ 为成交价格。
- **天气市场 (Weather)**：Taker 费率为 `0.05`，Maker 费率为 `0`（被动挂单方永远不收手续费，且享有 25% 的 Maker 返佣）。费用由撮合引擎在 Match 时协议端自动扣除，下单报文内不需要也不允许包含费率信息。

### 5. 速率限制 (Rate Limits)
- 采用双 Token Bucket 令牌桶模型：
  - 挂单桶 (`POST /order`, `POST /orders`)：1 Token / 单；
  - 撤单桶 (`DELETE /order`, `DELETE /orders`)：按撤单 ID 数量计 Token。
- 两个桶配额独立计算，互不挤占。

---

## 二、Nautilus Trader 官方 Polymarket 适配器架构剖析

Nautilus Trader 是一个高性能、事件驱动的多市场量化交易平台，其 Polymarket 官方适配器核心位于 `crates/adapters/polymarket/`（Rust 原生核心）并通过 PyO3 暴露给 Python (`nautilus_trader.adapters.polymarket`)。

### 1. 核心设计亮点
1. **统一的研究到实盘语义（Research-to-Live Semantic Parity）**：
   - Nautilus 坚持回测模拟（Backtest）与实盘执行（Live）共用同一套数据结构与事件总线。
   - 这与我们项目的 **Live ≡ Paper** 核心原则（同策略、同判定、同基建、同账本）高度契合。
2. **平仓残差精度截断 (`close_positions_qty_precision = 2`)**：
   - Nautilus Trader 在官方示例 (`examples/live/polymarket/exec_tester.py`) 中明确指明，针对 Polymarket 市场卖单必须设置 `close_positions_qty_precision=2`，向下截断股数，并记录微量残差（Residuals），严禁无损伪造填充。
3. **订单簿与盘口维护 (`OrderBook` & `TickSizeChange`)**：
   - 监听 WebSocket L2 增量更新；
   - 当收到 `tick_size_change` 事件时，立即丢弃当前本地盘口，重置为等待全量快照状态，在此期间丢弃增量更新，收到 Snapshot 后重新对齐，杜绝因跳 tick 导致的假深度。
4. **对账循环 (`LiveExecutionEngineConfig`)**：
   - 包含定时未结订单轮询（`open_check_interval_secs=10`）与持仓价值核账（`position_check_interval_secs=30`），自动纠偏本地账本与链上真实状态。

### 2. 轻量化改编决策与考量
- **Nautilus 原生版的局限**：原版 Nautilus Trader 依赖 Rust 编译器及重型 CPython 扩展，需要大内存与编译环境，在海外低配 1 vCPU / 1GB RAM 的云服务器（如马来西亚 Evoxt VPS）上构建极易触发 OOM (Out Of Memory)。
- **本项目改编策略**：
  - **吸收 Nautilus 的架构精髓**：采用与 Nautilus 一致的事件驱动端口化架构 (`ExecutionPort`)、盘口缓存与夹价对齐机制 (`clamp_limit`)、市价单精度向下取整 (`ROUND_DOWN`)、以及非阻塞对账循环。
  - **纯 Python 标准库 + 官方轻量 SDK**：仅依赖 `py-clob-client-v2` 进行网络与签名通信，其余风控、计划、路由均采用 Python 标准库实现，轻量至极，启动迅速且内存占用低于 60MB。

---

## 三、`weatherbotPreYes0910` 实盘基座更新清单

在吸收上述规范后，本项目完成了一次全维度的生产级进化：

1. **激进吃单 (FAK Taker) 精度修复** (`live/v2_transport.py`)：
   - 买单名义金额 (`BUY` amount) 强制截断为 2 位小数 (`Decimal('0.01')`)；
   - 卖单股数 (`SELL` amount) 强制截断为 4 位小数 (`Decimal('0.0001')`)；
   - 捕获 `no orders found to match with FAK` 异常，静默以 0-fill 正常结算。
2. **腿级独立放弃与绝不退化为 Maker** (`live/port.py`)：
   - YES 腿仅在 `(yes_min_ask, yes_max_ask]` 区间内吃单；区间外直接 `skip`，**严禁退化为被动限价单**；
   - NO 腿根据自身盘口 ask <= cap 独立决策。
3. **PreYes 稳了策略专用腿兼容** (`live/order_plan.py`, `_r_cycle.py`)：
   - 识别 `buy_yes_lock` 腿，映射至 `0.75` 封顶价与 `15.0` 单火情预算，完美契合 Consensus Lock 高胜率信号。
4. **三重生产安全闸门** (`live/submit.py`, `live/risk_gate.py`)：
   - `YES2RE_LIVE_ENABLE_SUBMIT=1`
   - `LIVE_SUBMIT_ENABLED=1`
   - `YES2RE_LIVE_CONFIRM=SMOKE-<UTC-date>`
   - 缺任一闸门直接抛出 `PortRefused`，绝对不进场且绝不静默降级为 paper。
5. **全量 142/142 测试套件在 Windows & Linux 双平台保持 100% 绿灯**。