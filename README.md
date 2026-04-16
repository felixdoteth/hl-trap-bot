# hl-trap-bot

A production-grade signal engine for a 5-minute EMA + Price Action Trap strategy, designed for binary markets (Polymarket Up/Down). Connects to Hyperliquid for perp execution via the [CoreWriter SDK](https://github.com/felixdoteth/corewriter-sdk).

---

## Overview

`hl-trap-bot` runs a multi-gate pipeline on 5-minute OHLCV candles to identify high-confidence price action trap setups. Each trade signal passes through five sequential gates — regime classification, location filter, trap detection, confirmation, and edge model — before execution is authorized.

**Stack:** Python · pandas · pandas-ta · Hyperliquid (CoreWriter SDK)

---

## Signal Pipeline

```
Candles → Gate 1: Regime → Gate 2: Location → Gate 3: Trap → Gate 4: Confirmation → Gate 5: Edge → TradeSignal
```

Any gate failure returns `should_trade=False`. All gates must pass.

### Gate 1 — Regime Classifier

Classifies market conditions as `TREND`, `RANGE`, or `UNKNOWN` over a configurable lookback window using:

- EMA slope magnitude
- Close position relative to EMA (one-sidedness)
- Directional persistence
- Wick asymmetry
- EMA crossing frequency
- Bar alternation rate
- Rolling range compression
- High/low body overlap

A minimum confidence threshold (`trend_conf_min` / `range_conf_min`, default `0.58`) is required to proceed.

### Gate 2 — Location Filter

Validates that price is near a meaningful structural level:

- EMA20
- Recent swing high/low
- Session open proxy (first bar open)
- Rolling range extremes

Proximity is measured in ATR units (`level_tolerance_atr`, default `1.5`). Regime-specific bonuses apply (EMA proximity in trend, range extremes in range).

### Gate 3 — Trap Detectors

Five trap types are evaluated simultaneously. The highest-scoring fired trap is selected.

| Trap | Description |
|------|-------------|
| **T1 — Failed Breakout** | Price breaks above/below a recent range boundary, fails to hold, and confirms reversal. Fades the breakout direction. |
| **T2 — Stop-Loss Sweep** | Price pokes just beyond a recent swing high/low (stop-hunt), closes back inside, and confirms opposite. |
| **T3 — Giant Exhaustion** | An oversized candle (body > `k×ATR`) appears at the end of an extended directional run with weak or opposite follow-through. Fade the giant bar direction. |
| **T4 — Outside Bar Double Trap** | An outside bar engulfs the prior bar (trapping longs and shorts). Entry only after the next bar confirms a resolution direction by closing beyond the outside bar's high or low. |
| **T5 — First Deep Pullback** | Trend regime only. After an extended move away from EMA, the first meaningful pullback to the EMA zone with wick rejection and confirmation continues in the trend direction. |

### Gate 4 — Confirmation Engine

Requires at least 2 of 4 independent confirmers to pass:

| Confirmer | Description |
|-----------|-------------|
| **C1 — Close Quality** | Latest bar closes in the upper/lower 35% of its range (directional). |
| **C2 — Follow-through** | Latest bar closes beyond the prior bar's high/low in signal direction. |
| **C3 — Wick Rejection** | Prior bar has a rejection wick > 80% of its body size. |
| **C4 — Micro-structure Shift** | HH/HL or LH/LL pattern over the last few bars confirms momentum. |

### Gate 5 — Edge Model

Converts signal strength to a fair probability and computes edge vs. the binary market price:

```
p_fair = clip(0.5 + 1.35 × (signal_strength − 0.5), 0.02, 0.98)

LONG  edge = p_fair − p_market
SHORT edge = (1 − p_fair) − p_market
```

Trade fires only if `edge > edge_min` (default `0.03`).

---

## Installation

**Requirements:** Python 3.10+

```bash
git clone https://github.com/felixdoteth/hl-trap-bot
cd hl-trap-bot
pip install pandas numpy pandas-ta
```

---

## Usage

```python
from signal_engine import SignalEngine, EngineConfig
import pandas as pd

# candles: DataFrame with columns [open, high, low, close, volume]
# 5-minute bars, minimum ~220 rows recommended
engine = SignalEngine(EngineConfig())

signal = engine.should_trade(candles, market_price=0.53)

print(signal.should_trade)   # bool
print(signal.direction)      # LONG / SHORT / FLAT
print(signal.confidence)     # float [0, 1]
print(signal.edge)           # float
print(signal.trap)           # TrapType or None
print(signal.reasoning)      # List[str] — full gate trace
```

### TradeSignal fields

| Field | Type | Description |
|-------|------|-------------|
| `should_trade` | `bool` | Whether to enter a position |
| `direction` | `Direction` | `LONG`, `SHORT`, or `FLAT` |
| `confidence` | `float` | Weighted signal strength [0, 1] |
| `edge` | `float` | Edge vs. market price |
| `entry_price` | `float` | Last close at signal time |
| `trap` | `TrapType \| None` | Which trap fired |
| `p_fair` | `float` | Model's fair probability |
| `p_market` | `float` | Observed market price |
| `regime` | `Regime` | `TREND`, `RANGE`, or `UNKNOWN` |
| `reasoning` | `List[str]` | Gate-by-gate trace log |

---

## Configuration

All parameters are set via `EngineConfig`. Defaults shown:

```python
from signal_engine import EngineConfig

cfg = EngineConfig(
    ema_len=20,                          # EMA period
    atr_len=14,                          # ATR period
    regime_lookback=36,                  # Bars for regime classification
    swing_lookback=20,                   # Bars for swing high/low
    trend_conf_min=0.58,                 # Min confidence to classify as TREND
    range_conf_min=0.58,                 # Min confidence to classify as RANGE
    level_tolerance_atr=1.5,            # ATR multiplier for location proximity
    failed_breakout_margin_atr=0.10,    # T1: breakout margin
    stop_sweep_margin_atr=0.08,         # T2: sweep margin
    giant_bar_atr_mult=1.8,             # T3: giant bar threshold
    giant_followthrough_weak_ratio=0.55, # T3: weak follow-through ratio
    deep_pullback_atr_mult=1.1,         # T5: pullback depth threshold
    confirmation_min_count=2,           # Gate 4: min confirmers required
    edge_min=0.03,                      # Gate 5: minimum edge to trade
    min_total_score=0.62,               # Global minimum signal score
)

engine = SignalEngine(cfg)
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `pandas` | DataFrame/candle processing |
| `numpy` | Numerical ops |
| `pandas-ta` | EMA, ATR indicators |

Hyperliquid execution uses the [CoreWriter SDK](https://github.com/felixdoteth/corewriter-sdk) — see that repo for perp order management and connection setup.

---

## Notes

- **Market price convention:** `market_price` must correspond to the correct contract stream for the signal direction. Pass the YES-on-UP price for LONG signals and YES-on-DOWN price for SHORT signals, or adapt the edge model caller accordingly.
- **Minimum candle history:** At least 60 bars required; ~220 recommended for stable regime classification.
- **5-minute bars only:** Regime, location, and trap logic are calibrated for 5m timeframes.

---

## License

MIT
