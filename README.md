# BTC ATM Short Straddle on Bybit with NautilusTrader

An event-driven quantitative trading strategy implementing an automated At-The-Money (ATM) Bitcoin Short Straddle on Bybit using the NautilusTrader framework.

The strategy executes across two operational modes:
- **Level 1 (Naked Straddle):** Identifies ATM Call and Put options sharing the same strike and expiry, submits simultaneous sell orders, and tracks order executions, lifecycle events, and position exposure.
- **Level 2 (Automated Risk Management):** Extends Level 1 with automated risk controls:
  - **Stop-Loss (1%):** Closes both positions if the combined ask-side buyback cost increases by >= 1% from the combined entry premium.
  - **Take-Profit (2%):** Closes both positions if the combined ask-side buyback cost decreases by >= 2% from the combined entry premium.

---

## Architecture & Execution Flow

```text
WAITING_FOR_REFERENCE ──(BTC index price)──> WAITING_FOR_QUOTES
WAITING_FOR_QUOTES    ──(two-sided quotes)─> ENTERING         (two IOC limit sells at bid)
ENTERING              ──(both legs filled)─> OPEN             (entry premium = call fill + put fill)
ENTERING              ──(partial / fail)───> EXITING          (unwind executed fills)
ENTERING              ──(zero fills)───────> DONE
OPEN                  ──(SL or TP breach)──> EXITING          (reduce-only market exits)
EXITING               ──(all flat)─────────> DONE
```

1. **Reference Price Feed:** Subscribes to `BTCUSDT-LINEAR.BYBIT` index prices, mirroring the underlying settlement price used by Bybit options.
2. **ATM Strike Selection:** Dynamically queries available options, filters for expiries at least 12 hours away, matches Call/Put pairs, and selects the strike closest to the index price. Throttled to 10-second re-evaluations during quote waiting to prevent subscription thrashing.
3. **Liquidity Extraction:** Requires valid bid/ask depth for both legs before dispatching simultaneous Limit IOC (Immediate-or-Cancel) sell orders at the current best bid.
4. **Lifecycle Tracking:** Ingests Bybit execution events, updating order and position states via NautilusTrader's `Cache` and `Portfolio`.
5. **Continuous Risk Evaluation:** Evaluates live ask-side buyback cost (`buyback = call_ask + put_ask`) on every quote tick, applying four layers of deduplication to prevent redundant exits.
6. **Iterative Exit Execution:** Cancels working orders and dispatches reduce-only market orders until all open positions are confirmed flat.

---

## Repository Layout

| File / Directory | Description |
|---|---|
| `run.py` | CLI entrypoint, environment resolution, and `TradingNode` lifecycle management. |
| `straddle/strategy.py` | Core event-driven `ShortStraddle` strategy actor and finite state machine. |
| `straddle/selection.py` | Pure functional ATM strike selection and expiration window filtering. |
| `tests/` | Automated test suite running inside NautilusTrader's `BacktestEngine`. |

---

## Setup & Installation

### Requirements
- Python 3.12 or newer

```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
```

Populate `.env` with your Bybit API key and secret. Two environments are supported:

| Environment | Flag | Description |
|---|---|---|
| **Demo Trading** | `--env demo` *(Recommended)* | Connects to `api-demo.bybit.com` with active simulated market makers and realistic two-sided order books. |
| **Testnet** | `--env testnet` *(Default)* | Connects to Bybit Testnet (`testnet.bybit.com`). |

> **Note on Testnet Liquidity:** Bybit Testnet option books typically lack quoting market makers. For live testing against populated order books, run against Bybit Demo (`--env demo`) or use local execution simulation (`--paper`).

---

## Running the Strategy

### Level 1: Naked Straddle
```bash
python run.py --level 1 --env demo
```

### Level 2: Automated Risk Management (1% SL / 2% TP)
```bash
python run.py --level 2 --env demo
```

### Local Simulation (Paper Execution)
Connects to live market data feeds while simulating fills locally via NautilusTrader's matching engine emulator (requires no API keys):
```bash
python run.py --level 2 --env demo --paper
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `--level {1, 2}` | `1` | Selects strategy mode (1 = Naked Straddle, 2 = Automated SL/TP). |
| `--env {demo, testnet}` | `testnet` | Selects Bybit API environment. |
| `--paper` | `False` | Enables local simulated matching engine. |
| `--quantity` | `0.01` | Order size per leg in BTC (must be multiple of 0.01 BTC lot size). |
| `--min-hours-to-expiry` | `12` | Filters out contracts expiring in under N hours to avoid expiration distortion. |
| `--keep-open` | `False` | Leaves positions open on process termination (`Ctrl-C`). Default gracefully closes open positions. |

---

## Automated Test Suite

The test suite contains 31 automated integration tests running inside NautilusTrader's `BacktestEngine`. The suite verifies the strategy against deterministic tick streams:

```bash
pytest -v
```

### Test Coverage Highlights
- **ATM Selection:** Expiry boundary conditions, strike distance tie-breaking, and missing twin handling.
- **Order Lifecycle:** Immediate fills, thin book rejections, and entry timeouts.
- **Asymmetric Execution:** Instant single-leg unwind when one order fails to fill.
- **Late Fills (Ghost Fills):** Verification that fills arriving after order cancellation are detected and closed.
- **Risk Triggers:** Precise boundary testing of the 1% Stop-Loss and 2% Take-Profit levels.

---

## Design Considerations

1. **Exact Decimal Accounting:** The strategy computes entry premiums directly from immutable `OrderFilled` execution events using Python's `Decimal` type to avoid binary floating-point rounding errors.
2. **Bid-Ask Spread Drag:** Selling at the bid and valuing buyback at the ask introduces an immediate spread friction upon fill (typically 0.5%–0.7% of entry premium on crypto options), naturally positioning the straddle closer to the 1% stop-loss threshold upon fill.
3. **Duplicate Exit Mitigation:** The state machine transitions to `EXITING` prior to dispatching closing orders, preventing subsequent incoming quote ticks from triggering concurrent exit attempts.
