# twinPreYes — Dual-Track Weather Trading Engine

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Polymarket%20CLOB%20v2-green.svg)](https://polymarket.com/)
[![Architecture](https://img.shields.io/badge/architecture-Dual--Track%20(Live%20%2B%20Paper)-orange.svg)](#architecture-overview)

`twinPreYes` is a production-grade automated trading system specifically engineered for Polymarket daily temperature prediction markets. It features a hardened **Dual-Track (双轨)** architecture running simultaneous **Live (Real Money)** and **Paper (Simulation)** instances to benchmark execution quality, fill slippage, and PnL deviation.

---

## Architecture Overview

The system runs two completely isolated execution tracks side-by-side on the same infrastructure:

```
                        +------------------------------+
                        |   METAR / TAF Weather Feed   |
                        |    & Polymarket CLOB Book    |
                        +--------------+---------------+
                                       |
                +----------------------+----------------------+
                |                                             |
                v                                             v
+------------------------------+              +------------------------------+
|       Paper Simulation       |              |      Live Real Trading       |
|    (preyes-paper.service)    |              |   (preyes-live0915.service)  |
+------------------------------+              +------------------------------+
| * Virtual 100 USDC ledger    |              | * Real USDC on Polygon       |
| * In-memory FAK book matcher |              | * CLOB v2 EIP-712 Signatures |
| * Zero wallet/key exposure   |              | * 3-Gate Safety Mechanism    |
| * Unconstrained test-bed     |              | * Real FAK Buy & Market Sell |
+--------------+---------------+              +--------------+---------------+
               |                                             |
               +----------------------+----------------------+
                                      |
                                      v
                       +------------------------------+
                       |  30-Min Dual-Track Monitor   |
                       |   (Cross-Track Reconcile)    |
                       +------------------------------+
```

### 1. Dual Execution Ports
- **LivePort (`live/port.py`)**: Places real orders on Polymarket CLOB using `py-clob-client-v2`. Enforces fail-closed safety gates, negative-risk signing domain resolution, live balance preflights, and real market SELL executions on exit.
- **PaperPort (`live/port.py`)**: Executes instant in-memory matching against cached order books with an isolated internal accounting ledger.

### 2. Two-Channel Entry Strategy
- **Channel A (Target Bucket Lock / 目标桶锁定)**: Identifies the high-probability target temperature bucket (0.42 to 0.86) once peak daytime solar radiation subsides and temperature velocity stagnates.
- **Channel B (Next Bucket Breakout / 下一档廉价突破)**: Takes low-cost asymmetric bets (0.27 to 0.40) on the next adjacent temperature bucket before midday heating completes.

### 3. Asymmetric Profit Taking & Defensive Exits
- **50% Dynamic Ladder Take-Profit (50% 阶梯止盈)**: When market bid reaches 1.50x the entry price (e.g. 0.28 to 0.42), the engine automatically sells 50% of the position with a market FAK order to recover 100% of initial principal, leaving the remaining 50% as a zero-risk free roll into final oracle settlement.
- **Early Stop-Loss & Floor Guards (破位与异动止损)**: Exits immediately if the market bid drops below 50% of entry price or if an unconfirmed temperature reversal is detected.

---

## Three-Gate Live Safety Lock

To prevent errant executions or unauthorized restarts, the Live trading engine requires three distinct gates to be satisfied simultaneously before any real order can be signed:

1. **CLI / Process Flag**: Explicit opt-in via environment `YES2RE_LIVE_ENABLE_SUBMIT=1` and `YES2RE_MODE=live`.
2. **Environment Flag**: `LIVE_SUBMIT_ENABLED=1`.
3. **UTC-Dated Confirmation Phrase**: `YES2RE_LIVE_CONFIRM="SMOKE-YYYY-MM-DD"`, derived dynamically from the current UTC date to prevent replay attacks across midnight boundaries.

---

## Repository Structure

```
|-- adapters/                  # Data adapters (Polymarket CLOB, AviationWeather, CheckWX)
|-- config/
|   |-- contract_cities.json   # Supported airport ICAO stations and market metadata
|   |-- yes2re_live.json       # Production Live configuration (5 USDC budget, live sell enabled)
|   +-- yes2re_reversal.json   # Simulation Paper configuration (10 USDC budget)
|-- live/                      # Production execution layer
|   |-- clob_client.py         # Polymarket CLOB API wrapper
|   |-- creds.py               # Credentials & environment validation
|   |-- exit.py                # Real market SELL / take-profit & stop-loss engine
|   |-- port.py                # Unified execution port contract (LivePort vs PaperPort)
|   |-- reconcile.py           # On-chain vs engine ledger reconciliation
|   |-- risk_gate.py           # Pre-trade capital, open order & position checks
|   |-- submit.py              # 3-gate validator and audit logging
|   +-- v2_transport.py        # EIP-712 order signing & CLOB v2 interaction
|-- ops/
|   +-- systemd/               # Linux systemd service units & daily rotation timer
|       |-- preyes-live0915.service
|       |-- preyes-live0915-daily.timer
|       +-- preyes-paper.service
|-- scripts/
|   |-- check_dual_track.py    # 30-minute dual-track cross-audit reconciliation tool
|   +-- chain_read.py          # Quick on-chain balance & position reader
|-- strategy_consensus_lock.py # Core temperature consensus lock strategy
|-- _r_cycle.py                # Main orchestrator cycle loop
|-- _r_state.py                # State persistence & config loader
|-- reversal_runner.py         # Entry point CLI
|-- run_live.sh                # Live runner wrapper script
|-- run_paper.sh               # Paper runner wrapper script
|-- .env.example               # Sanitized environment template
+-- README.md
```

---

## Deployment & Operations

### 1. Environment Setup

Copy the template configuration and supply your keys:

```bash
cp .env.example .env
chmod 600 .env
```

Required parameters in `.env`:
- `CHECKWX_API_KEY`: CheckWX API key for METAR/TAF weather observations.
- `POLY_PRIVATE_KEY`: L1 transaction signer private key (EOA).
- `POLY_FUNDER_ADDRESS`: Polymarket proxy or funder address holding USDC.
- `POLY_SIGNATURE_TYPE`: `1` for Polymarket Proxy (Magic/Email login), `0` for EOA.
- `LIVE_FIRE_BUDGET_USDC`: Per-bucket budget limit (e.g. `5`).
- `LIVE_MAX_OPEN_POSITIONS`: Concurrent position cap (e.g. `2` or `22`).

### 2. Running the Dual-Track Instances

#### Paper Simulation:
```bash
./run_paper.sh
```

#### Live Real Trading:
```bash
./run_live.sh
```

### 3. Systemd Service Deployment

```bash
sudo cp ops/systemd/*.service /etc/systemd/system/
sudo cp ops/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Start and enable Paper
sudo systemctl enable --now preyes-paper.service

# Start Live service & daily phrase rotation timer
sudo systemctl enable --now preyes-live0915-daily.timer
sudo systemctl start preyes-live0915.service
```

### 4. Automated Dual-Track Inspection (Cron)

Set up a 30-minute cron check to log dual-track metrics:

```bash
*/30 * * * * /path/to/venv/bin/python scripts/check_dual_track.py >> /var/log/dual_track.log 2>&1
```

---

## Risk Management & Safeguards

- **Strict Capital Clamping**: Hard limits on capital committed per fire and maximum simultaneous open markets.
- **Fail-Closed Floor Guards**: Any book quote below configured exit floors triggers local order suppression (`below_min_order_size` or `below_sell_floor`), preventing predatory fills.
- **Host OOM Mitigation**: Designed to operate within a 1GB VPS footprint with configured swap space (`vm.swappiness=10`) and systemd `MemoryMax` limits.

---

## License

MIT License.
