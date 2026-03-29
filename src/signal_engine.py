from __future__ import annotations

"""
Production-grade signal engine for a 5-minute EMA + Price Action Trap strategy
adapted to binary markets (e.g., Polymarket Up/Down).

Includes:
- Regime classifier (trend vs range) with confidence
- Location filter
- 5 trap detectors (T1..T5)
- Confirmation engine (2-of-4)
- Binary edge model (p_fair vs p_market)
- Single should_trade(...) orchestration

Dependencies:
- pandas
- numpy
- pandas_ta

Install:
    pip install pandas numpy pandas-ta
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta as ta


# =========================
# Enums / Dataclasses
# =========================


class Direction(str, Enum):
    LONG = "LONG"   # bullish underlying move, e.g. YES on Up
    SHORT = "SHORT" # bearish underlying move, e.g. YES on Down
    FLAT = "FLAT"


class Regime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


class TrapType(str, Enum):
    T1_FAILED_BREAKOUT = "T1_FAILED_BREAKOUT"
    T2_STOP_SWEEP = "T2_STOP_SWEEP"
    T3_GIANT_EXHAUSTION = "T3_GIANT_EXHAUSTION"
    T4_OUTSIDE_DOUBLE_TRAP = "T4_OUTSIDE_DOUBLE_TRAP"
    T5_FIRST_DEEP_PULLBACK = "T5_FIRST_DEEP_PULLBACK"


@dataclass
class TrapSignal:
    trap_type: TrapType
    fired: bool
    direction: Direction = Direction.FLAT
    score: float = 0.0
    reason: str = ""
    metadata: Dict[str, float] = field(default_factory=dict)


@dataclass
class RegimeResult:
    regime: Regime
    confidence: float
    trend_score: float
    range_score: float
    reason: str


@dataclass
class LocationResult:
    valid: bool
    score: float
    tags: List[str]
    reason: str


@dataclass
class ConfirmationResult:
    passed: bool
    count_passed: int
    details: Dict[str, bool]
    score: float
    reason: str


@dataclass
class EdgeResult:
    passed: bool
    p_fair: float
    p_market: float
    edge: float
    edge_min: float
    reason: str


@dataclass
class TradeSignal:
    should_trade: bool
    direction: Direction
    confidence: float
    edge: float
    entry_price: float
    trap: Optional[TrapType]
    reasoning: List[str]
    p_fair: float
    p_market: float
    regime: Regime


@dataclass
class EngineConfig:
    # indicator windows
    ema_len: int = 20
    atr_len: int = 14
    regime_lookback: int = 36
    swing_lookback: int = 20

    # regime thresholds
    trend_conf_min: float = 0.58
    range_conf_min: float = 0.58

    # location
    level_tolerance_atr: float = 1.5

    # trap parameters
    failed_breakout_margin_atr: float = 0.10
    stop_sweep_margin_atr: float = 0.08
    giant_bar_atr_mult: float = 1.8
    giant_followthrough_weak_ratio: float = 0.55
    deep_pullback_atr_mult: float = 1.1

    # confirmation
    confirmation_min_count: int = 2

    # edge
    edge_min: float = 0.03

    # scoring
    min_total_score: float = 0.62


# =========================
# Utility helpers
# =========================


def _clamp01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b == 0:
        return default
    return a / b


def _last(df: pd.DataFrame, col: str, n: int = 1) -> pd.Series:
    return df[col].iloc[-n]


def _body(row: pd.Series) -> float:
    return abs(float(row["close"] - row["open"]))


def _range(row: pd.Series) -> float:
    return float(max(1e-12, row["high"] - row["low"]))


def _upper_wick(row: pd.Series) -> float:
    return float(row["high"] - max(row["open"], row["close"]))


def _lower_wick(row: pd.Series) -> float:
    return float(min(row["open"], row["close"]) - row["low"])


def _close_pos_in_bar(row: pd.Series) -> float:
    # 0 at low, 1 at high
    return _safe_div(float(row["close"] - row["low"]), _range(row), 0.5)


def _is_bull(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bear(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


# =========================
# Main engine
# =========================


class SignalEngine:
    """
    PSEUDOCODE OVERVIEW
    -------------------
    should_trade(candles, market_price):
      1) preprocess -> compute EMA/ATR/features
      2) Gate 1 Regime:
         regime = classify_regime(df)
         if low confidence -> no trade
      3) Gate 2 Location:
         loc = location_filter(df, regime)
         if not valid -> no trade
      4) Gate 3 Trap:
         run all T1..T5 detectors
         choose best fired trap by score
         if none -> no trade
      5) Gate 4 Confirmation:
         confirm = confirmation_engine(df, trap)
         if <2/4 -> no trade
      6) Gate 5 Edge:
         p_fair = map(signal_strength -> probability)
         p_market = market odds
         edge = p_fair - p_market (LONG)
                (1-p_fair) - p_market (SHORT if market_price is YES-on-DOWN
                 then pass correct market stream accordingly)
         if edge <= edge_min -> no trade
      7) return TradeSignal
    """

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.cfg = config or EngineConfig()

    # ---------- public API ----------

    def should_trade(self, candles: pd.DataFrame, market_price: float) -> TradeSignal:
        """
        Gate pipeline:
          Gate 1 Regime -> Gate 2 Location -> Gate 3 Trap -> Gate 4 Confirmation -> Gate 5 Edge

        Parameters
        ----------
        candles : pd.DataFrame
            Must contain columns: [open, high, low, close, volume]
            5-minute bars expected.
        market_price : float
            Current binary market implied probability in [0,1]
            for the instrument direction stream this engine is trading.

        Returns
        -------
        TradeSignal
        """
        reasoning: List[str] = []

        df = self._prepare(candles)
        if len(df) < max(self.cfg.regime_lookback + 5, 60):
            return TradeSignal(
                should_trade=False,
                direction=Direction.FLAT,
                confidence=0.0,
                edge=0.0,
                entry_price=float(df["close"].iloc[-1]),
                trap=None,
                reasoning=["Not enough candle history"],
                p_fair=0.5,
                p_market=float(market_price),
                regime=Regime.UNKNOWN,
            )

        # Gate 1: Regime
        regime = self.classify_regime(df)
        reasoning.append(f"Gate1 Regime={regime.regime.value} conf={regime.confidence:.2f}")
        if regime.regime == Regime.UNKNOWN:
            return self._reject(df, market_price, regime, reasoning + ["Regime uncertain"])

        # Gate 2: Location
        loc = self.location_filter(df, regime)
        reasoning.append(f"Gate2 Location valid={loc.valid} score={loc.score:.2f} tags={loc.tags}")
        if not loc.valid:
            return self._reject(df, market_price, regime, reasoning + [loc.reason])

        # Gate 3: Trap
        traps = self.detect_all_traps(df, regime)
        fired = [t for t in traps if t.fired]
        if not fired:
            return self._reject(df, market_price, regime, reasoning + ["No trap fired"])
        best_trap = sorted(fired, key=lambda x: x.score, reverse=True)[0]
        reasoning.append(f"Gate3 Trap={best_trap.trap_type.value} dir={best_trap.direction.value} score={best_trap.score:.2f}")

        # Gate 4: Confirmation
        conf = self.confirmation_engine(df, best_trap)
        reasoning.append(f"Gate4 Confirmation passed={conf.passed} count={conf.count_passed}/4")
        if not conf.passed:
            return self._reject(df, market_price, regime, reasoning + [conf.reason], trap=best_trap)

        # Aggregate confidence / signal strength
        signal_strength = _clamp01(
            0.30 * regime.confidence +
            0.20 * loc.score +
            0.30 * best_trap.score +
            0.20 * conf.score
        )

        # Gate 5: Edge
        edge_res = self.edge_filter(signal_strength, best_trap.direction, market_price)
        reasoning.append(
            f"Gate5 Edge passed={edge_res.passed} p_fair={edge_res.p_fair:.3f} "
            f"p_market={edge_res.p_market:.3f} edge={edge_res.edge:.3f}"
        )
        if not edge_res.passed:
            return self._reject(
                df,
                market_price,
                regime,
                reasoning + [edge_res.reason],
                trap=best_trap,
                p_fair=edge_res.p_fair,
                edge=edge_res.edge,
                direction=best_trap.direction,
            )

        entry_price = float(df["close"].iloc[-1])
        return TradeSignal(
            should_trade=True,
            direction=best_trap.direction,
            confidence=signal_strength,
            edge=edge_res.edge,
            entry_price=entry_price,
            trap=best_trap.trap_type,
            reasoning=reasoning + [best_trap.reason, conf.reason, edge_res.reason],
            p_fair=edge_res.p_fair,
            p_market=edge_res.p_market,
            regime=regime.regime,
        )

    # ---------- preprocessing ----------

    def _prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(candles.columns)
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

        df = candles.copy().reset_index(drop=True)

        # indicators
        df["ema20"] = ta.ema(df["close"], length=self.cfg.ema_len)
        df["atr14"] = ta.atr(df["high"], df["low"], df["close"], length=self.cfg.atr_len)

        # price-action features
        df["body"] = (df["close"] - df["open"]).abs()
        df["bar_range"] = (df["high"] - df["low"]).replace(0, np.nan)
        df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
        df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
        df["close_pos"] = (df["close"] - df["low"]) / (df["bar_range"].replace(0, np.nan))
        df["close_pos"] = df["close_pos"].fillna(0.5).clip(0, 1)
        df["ret"] = df["close"].pct_change().fillna(0.0)

        # structural helpers
        lb = self.cfg.swing_lookback
        df["swing_high"] = df["high"].rolling(lb).max()
        df["swing_low"] = df["low"].rolling(lb).min()

        df = df.dropna().reset_index(drop=True)
        return df

    # ---------- Gate 1: regime ----------

    def classify_regime(self, df: pd.DataFrame) -> RegimeResult:
        """
        PSEUDOCODE:
          lookback window W
          trend evidence:
            - ema slope magnitude
            - closes one-sided vs ema
            - directional persistence
            - wick asymmetry consistent with direction
          range evidence:
            - frequent ema crossings
            - high body overlap / alternation
            - compressed rolling range
          produce trend_score, range_score in [0,1]
          regime = argmax(score) if confidence threshold met else UNKNOWN
        """
        w = min(self.cfg.regime_lookback, len(df) - 1)
        x = df.iloc[-w:].copy()

        atr = float(x["atr14"].iloc[-1])
        atr = max(atr, 1e-9)

        # Trend features
        ema_slope = float((x["ema20"].iloc[-1] - x["ema20"].iloc[0]) / (w * atr))
        ema_slope_score = _clamp01(abs(ema_slope) / 0.25)

        above_ema = (x["close"] > x["ema20"]).mean()
        one_side_score = _clamp01(abs(above_ema - 0.5) * 2.0)

        sign = np.sign(x["close"].diff().fillna(0.0))
        persistence = (sign.eq(sign.shift(1)) & sign.ne(0)).mean()
        persistence_score = _clamp01(float(persistence) / 0.75)

        bull_wick_dom = (x["lower_wick"].mean() - x["upper_wick"].mean()) / atr
        wick_score = _clamp01(abs(float(bull_wick_dom)) / 0.5)

        trend_score = _clamp01(0.25 * ema_slope_score + 0.25 * one_side_score + 0.35 * persistence_score + 0.15 * wick_score)

        # Range features
        cross = ((x["close"] > x["ema20"]).astype(int).diff().abs() == 1).mean()
        cross_score = _clamp01(float(cross) / 0.45)

        alternation = (np.sign(x["close"] - x["open"]).diff().abs() == 2).mean()
        alternation_score = _clamp01(float(alternation) / 0.55)

        rolling_rng = (x["high"].rolling(8).max() - x["low"].rolling(8).min()).dropna()
        compression = 1.0 - _clamp01(float(rolling_rng.iloc[-1] / (x["atr14"].iloc[-1] * 4.0 + 1e-9)))
        compression_score = _clamp01(compression)

        overlap = ((x["high"].shift(1) >= x["low"]) & (x["low"].shift(1) <= x["high"]))
        overlap_score = _clamp01(float(overlap.mean()) / 0.85)

        range_score = _clamp01(0.20 * cross_score + 0.35 * alternation_score + 0.10 * compression_score + 0.35 * overlap_score)

        # Regime decision
        if trend_score > range_score and trend_score >= self.cfg.trend_conf_min:
            regime = Regime.TREND
            conf = trend_score
        elif range_score > trend_score and range_score >= self.cfg.range_conf_min:
            regime = Regime.RANGE
            conf = range_score
        else:
            regime = Regime.UNKNOWN
            conf = max(trend_score, range_score)

        reason = (
            f"trend_score={trend_score:.2f}, range_score={range_score:.2f}, "
            f"ema_slope={ema_slope:.3f}, crosses={cross:.2f}"
        )

        return RegimeResult(
            regime=regime,
            confidence=_clamp01(conf),
            trend_score=trend_score,
            range_score=range_score,
            reason=reason,
        )

    # ---------- Gate 2: location ----------

    def location_filter(self, df: pd.DataFrame, regime: RegimeResult) -> LocationResult:
        """
        PSEUDOCODE:
          meaningful levels:
            - ema20
            - recent swing high/low
            - recent local range high/low (last N)
            - session open proxy (first bar open in frame)
          distance to nearest level normalized by ATR
          valid if near one or more key levels within tolerance
        """
        last = df.iloc[-1]
        px = float(last["close"])
        atr = max(float(last["atr14"]), 1e-9)
        tol = self.cfg.level_tolerance_atr * atr

        n = min(36, len(df))
        recent = df.iloc[-n:]

        levels = {
            "ema20": float(last["ema20"]),
            "recent_high": float(recent["high"].max()),
            "recent_low": float(recent["low"].min()),
            "swing_high": float(last["swing_high"]),
            "swing_low": float(last["swing_low"]),
            "session_open_proxy": float(df.iloc[0]["open"]),
        }

        tags: List[str] = []
        dists = {}
        for name, lvl in levels.items():
            d = abs(px - lvl)
            dists[name] = d
            if d <= tol:
                tags.append(name)

        nearest = min(dists.values()) if dists else 1e9
        proximity_score = _clamp01(1.0 - nearest / (2.5 * atr))

        # Regime-specific soft requirement
        if regime.regime == Regime.TREND:
            trend_bonus = 0.15 if "ema20" in tags else 0.0
            score = _clamp01(0.75 * proximity_score + trend_bonus)
        elif regime.regime == Regime.RANGE:
            extreme_tag = any(t in tags for t in ["recent_high", "recent_low", "swing_high", "swing_low"])
            range_bonus = 0.2 if extreme_tag else 0.0
            score = _clamp01(0.70 * proximity_score + range_bonus)
        else:
            score = 0.0

        valid = (len(tags) > 0) and (score >= 0.40)
        reason = f"near_levels={tags}, nearest_dist_atr={nearest/atr:.2f}, score={score:.2f}"

        return LocationResult(valid=valid, score=score, tags=tags, reason=reason)

    # ---------- Gate 3: traps ----------

    def detect_all_traps(self, df: pd.DataFrame, regime: RegimeResult) -> List[TrapSignal]:
        return [
            self.detect_t1_failed_breakout(df, regime),
            self.detect_t2_stop_sweep(df, regime),
            self.detect_t3_giant_exhaustion(df, regime),
            self.detect_t4_outside_double_trap(df, regime),
            self.detect_t5_first_deep_pullback(df, regime),
        ]

    def detect_t1_failed_breakout(self, df: pd.DataFrame, regime: RegimeResult) -> TrapSignal:
        """
        T1 Failed Breakout Trap
        PSEUDOCODE:
          define recent box high/low over N bars
          if prior bar breaks above box_high + eps and closes back inside OR with large upper wick
             and latest bar confirms down -> SHORT
          mirror for breakdown failure -> LONG
        """
        n = min(24, len(df) - 2)
        box = df.iloc[-(n + 2):-2]
        b1 = df.iloc[-2]  # breakout attempt bar
        b0 = df.iloc[-1]  # confirmation bar
        atr = max(float(b0["atr14"]), 1e-9)
        eps = self.cfg.failed_breakout_margin_atr * atr

        box_high = float(box["high"].max())
        box_low = float(box["low"].min())

        # Upside failed breakout -> SHORT
        up_break = float(b1["high"]) > box_high + eps
        up_fail_close_inside = float(b1["close"]) < box_high
        up_fail_wick = _upper_wick(b1) > _body(b1)
        up_confirm = _is_bear(b0) and float(b0["close"]) < float(b1["close"])

        if up_break and (up_fail_close_inside or up_fail_wick) and up_confirm:
            score = _clamp01(0.55 + 0.20 * float(regime.range_score) + 0.10 * float(up_fail_wick) + 0.15 * float(up_confirm))
            return TrapSignal(
                trap_type=TrapType.T1_FAILED_BREAKOUT,
                fired=True,
                direction=Direction.SHORT,
                score=score,
                reason="T1: Upside breakout failed and confirmed bearish",
                metadata={"box_high": box_high, "box_low": box_low},
            )

        # Downside failed breakout -> LONG
        dn_break = float(b1["low"]) < box_low - eps
        dn_fail_close_inside = float(b1["close"]) > box_low
        dn_fail_wick = _lower_wick(b1) > _body(b1)
        dn_confirm = _is_bull(b0) and float(b0["close"]) > float(b1["close"])

        if dn_break and (dn_fail_close_inside or dn_fail_wick) and dn_confirm:
            score = _clamp01(0.55 + 0.20 * float(regime.range_score) + 0.10 * float(dn_fail_wick) + 0.15 * float(dn_confirm))
            return TrapSignal(
                trap_type=TrapType.T1_FAILED_BREAKOUT,
                fired=True,
                direction=Direction.LONG,
                score=score,
                reason="T1: Downside breakout failed and confirmed bullish",
                metadata={"box_high": box_high, "box_low": box_low},
            )

        return TrapSignal(TrapType.T1_FAILED_BREAKOUT, fired=False)

    def detect_t2_stop_sweep(self, df: pd.DataFrame, regime: RegimeResult) -> TrapSignal:
        """
        T2 Stop-Loss Sweep / 1-tick trap
        PSEUDOCODE:
          identify recent swing high/low
          if latest-1 bar pokes above swing_high by tiny margin and closes back below,
          then latest bar confirms opposite => SHORT (and mirror for LONG)
        """
        if len(df) < 25:
            return TrapSignal(TrapType.T2_STOP_SWEEP, fired=False)

        b1 = df.iloc[-2]
        b0 = df.iloc[-1]
        look = df.iloc[-22:-2]
        atr = max(float(b0["atr14"]), 1e-9)
        eps = self.cfg.stop_sweep_margin_atr * atr

        swing_high = float(look["high"].max())
        swing_low = float(look["low"].min())

        # sweep highs then reject => SHORT
        high_sweep = float(b1["high"]) > swing_high + eps
        high_reject = float(b1["close"]) < swing_high
        high_confirm = _is_bear(b0) and float(b0["close"]) < float(b1["low"])

        if high_sweep and high_reject and high_confirm:
            score = _clamp01(0.60 + 0.15 * float(_upper_wick(b1) > _body(b1)) + 0.15 * float(high_confirm) + 0.10 * float(regime.range_score))
            return TrapSignal(
                trap_type=TrapType.T2_STOP_SWEEP,
                fired=True,
                direction=Direction.SHORT,
                score=score,
                reason="T2: High sweep/stop-hunt rejected and confirmed down",
                metadata={"swing_high": swing_high},
            )

        # sweep lows then reject => LONG
        low_sweep = float(b1["low"]) < swing_low - eps
        low_reject = float(b1["close"]) > swing_low
        low_confirm = _is_bull(b0) and float(b0["close"]) > float(b1["high"])

        if low_sweep and low_reject and low_confirm:
            score = _clamp01(0.60 + 0.15 * float(_lower_wick(b1) > _body(b1)) + 0.15 * float(low_confirm) + 0.10 * float(regime.range_score))
            return TrapSignal(
                trap_type=TrapType.T2_STOP_SWEEP,
                fired=True,
                direction=Direction.LONG,
                score=score,
                reason="T2: Low sweep/stop-hunt rejected and confirmed up",
                metadata={"swing_low": swing_low},
            )

        return TrapSignal(TrapType.T2_STOP_SWEEP, fired=False)

    def detect_t3_giant_exhaustion(self, df: pd.DataFrame, regime: RegimeResult) -> TrapSignal:
        """
        T3 Giant Exhaustion Trap
        PSEUDOCODE:
          giant bar = body > k * ATR
          if appears after directional run and follow-through weak / opposite,
          fade it (contrarian)
        """
        if len(df) < 15:
            return TrapSignal(TrapType.T3_GIANT_EXHAUSTION, fired=False)

        b1 = df.iloc[-2]  # giant candidate
        b0 = df.iloc[-1]  # follow-through bar
        prev = df.iloc[-8:-2]

        atr = max(float(b1["atr14"]), 1e-9)
        giant = _body(b1) > self.cfg.giant_bar_atr_mult * atr

        if not giant:
            return TrapSignal(TrapType.T3_GIANT_EXHAUSTION, fired=False)

        # Extended run context
        run_dir = np.sign((prev["close"] - prev["open"]).sum())
        b1_dir = 1 if _is_bull(b1) else (-1 if _is_bear(b1) else 0)
        extended = (run_dir == b1_dir) and (abs(prev["close"].iloc[-1] - prev["close"].iloc[0]) > 1.2 * atr)

        # weak follow-through
        follow_ratio = _safe_div(_body(b0), _body(b1), 0.0)
        weak_follow = follow_ratio < self.cfg.giant_followthrough_weak_ratio
        opposite_follow = (b1_dir == 1 and _is_bear(b0)) or (b1_dir == -1 and _is_bull(b0))

        if extended and (weak_follow or opposite_follow):
            direction = Direction.SHORT if b1_dir == 1 else Direction.LONG
            score = _clamp01(0.58 + 0.20 * float(opposite_follow) + 0.12 * float(weak_follow) + 0.10 * float(regime.range_score))
            return TrapSignal(
                trap_type=TrapType.T3_GIANT_EXHAUSTION,
                fired=True,
                direction=direction,
                score=score,
                reason="T3: Giant late-run bar exhausted; follow-through weak/opposite",
                metadata={"follow_ratio": float(follow_ratio)},
            )

        return TrapSignal(TrapType.T3_GIANT_EXHAUSTION, fired=False)

    def detect_t4_outside_double_trap(self, df: pd.DataFrame, regime: RegimeResult) -> TrapSignal:
        """
        T4 Back-to-Back Outside Bar Double Trap
        PSEUDOCODE:
          outside bar engulfs previous high+low (traps both sides)
          do not enter outside bar itself
          enter only when next bar confirms one direction by close strength / break
        """
        if len(df) < 4:
            return TrapSignal(TrapType.T4_OUTSIDE_DOUBLE_TRAP, fired=False)

        b2 = df.iloc[-3]  # prior bar
        b1 = df.iloc[-2]  # outside trap bar
        b0 = df.iloc[-1]  # confirmation

        outside = (float(b1["high"]) > float(b2["high"])) and (float(b1["low"]) < float(b2["low"]))
        if not outside:
            return TrapSignal(TrapType.T4_OUTSIDE_DOUBLE_TRAP, fired=False)

        # confirmation of resolution direction
        bull_confirm = _is_bull(b0) and float(b0["close"]) > float(b1["high"])
        bear_confirm = _is_bear(b0) and float(b0["close"]) < float(b1["low"])

        if bull_confirm:
            score = _clamp01(0.60 + 0.15 * float(_close_pos_in_bar(b0) > 0.7) + 0.15 * float(regime.trend_score))
            return TrapSignal(
                trap_type=TrapType.T4_OUTSIDE_DOUBLE_TRAP,
                fired=True,
                direction=Direction.LONG,
                score=score,
                reason="T4: Outside bar double-trap resolved bullish",
            )

        if bear_confirm:
            score = _clamp01(0.60 + 0.15 * float(_close_pos_in_bar(b0) < 0.3) + 0.15 * float(regime.trend_score))
            return TrapSignal(
                trap_type=TrapType.T4_OUTSIDE_DOUBLE_TRAP,
                fired=True,
                direction=Direction.SHORT,
                score=score,
                reason="T4: Outside bar double-trap resolved bearish",
            )

        return TrapSignal(TrapType.T4_OUTSIDE_DOUBLE_TRAP, fired=False)

    def detect_t5_first_deep_pullback(self, df: pd.DataFrame, regime: RegimeResult) -> TrapSignal:
        """
        T5 First Deep Pullback Continuation (trend context)
        PSEUDOCODE:
          require trend regime
          detect extended move away from EMA
          first meaningful touch/pullback into EMA zone
          rejection + confirmation in original trend direction
        """
        if regime.regime != Regime.TREND or len(df) < 30:
            return TrapSignal(TrapType.T5_FIRST_DEEP_PULLBACK, fired=False)

        x = df.iloc[-20:]
        b1 = df.iloc[-2]
        b0 = df.iloc[-1]

        atr = max(float(b0["atr14"]), 1e-9)

        # infer trend direction from EMA slope and closes vs EMA
        ema_up = float(x["ema20"].iloc[-1] - x["ema20"].iloc[0]) > 0
        side_ratio = (x["close"] > x["ema20"]).mean()
        trend_up = ema_up and side_ratio > 0.62
        trend_down = (not ema_up) and side_ratio < 0.38

        if not (trend_up or trend_down):
            return TrapSignal(TrapType.T5_FIRST_DEEP_PULLBACK, fired=False)

        # extension prior to pullback
        if trend_up:
            extension = float((x["high"].max() - x["ema20"].iloc[-5:-1].mean()) / atr)
            touched_ema_recently = ((x.iloc[:-4]["low"] - x.iloc[:-4]["ema20"]).abs() < 0.25 * atr).any()
            first_pullback = not bool(touched_ema_recently)

            b1_touch = abs(float(b1["low"] - b1["ema20"])) < self.cfg.deep_pullback_atr_mult * 0.45 * atr
            reject = _lower_wick(b1) > _body(b1)
            confirm = _is_bull(b0) and float(b0["close"]) > float(b1["high"])

            if extension > self.cfg.deep_pullback_atr_mult and first_pullback and b1_touch and reject and confirm:
                score = _clamp01(0.62 + 0.12 * float(extension > 1.5) + 0.14 * float(confirm) + 0.08 * float(regime.trend_score))
                return TrapSignal(
                    trap_type=TrapType.T5_FIRST_DEEP_PULLBACK,
                    fired=True,
                    direction=Direction.LONG,
                    score=score,
                    reason="T5: First deep pullback to EMA in uptrend confirmed long",
                )

        if trend_down:
            extension = float((x.iloc[-5:-1]["ema20"].mean() - x["low"].min()) / atr)
            touched_ema_recently = ((x.iloc[:-4]["high"] - x.iloc[:-4]["ema20"]).abs() < 0.25 * atr).any()
            first_pullback = not bool(touched_ema_recently)

            b1_touch = abs(float(b1["high"] - b1["ema20"])) < self.cfg.deep_pullback_atr_mult * 0.45 * atr
            reject = _upper_wick(b1) > _body(b1)
            confirm = _is_bear(b0) and float(b0["close"]) < float(b1["low"])

            if extension > self.cfg.deep_pullback_atr_mult and first_pullback and b1_touch and reject and confirm:
                score = _clamp01(0.62 + 0.12 * float(extension > 1.5) + 0.14 * float(confirm) + 0.08 * float(regime.trend_score))
                return TrapSignal(
                    trap_type=TrapType.T5_FIRST_DEEP_PULLBACK,
                    fired=True,
                    direction=Direction.SHORT,
                    score=score,
                    reason="T5: First deep pullback to EMA in downtrend confirmed short",
                )

        return TrapSignal(TrapType.T5_FIRST_DEEP_PULLBACK, fired=False)

    # ---------- Gate 4: confirmation ----------

    def confirmation_engine(self, df: pd.DataFrame, trap: TrapSignal) -> ConfirmationResult:
        """
        2-of-4 confirmers:
          C1 close quality
          C2 follow-through direction
          C3 wick rejection alignment
          C4 micro-structure shift (HH/HL or LH/LL proxy)
        """
        b1 = df.iloc[-2]
        b0 = df.iloc[-1]

        # C1 close quality
        cp = _close_pos_in_bar(b0)
        if trap.direction == Direction.LONG:
            c1 = cp > 0.65
        else:
            c1 = cp < 0.35

        # C2 follow-through
        if trap.direction == Direction.LONG:
            c2 = _is_bull(b0) and float(b0["close"]) > float(b1["high"])
        else:
            c2 = _is_bear(b0) and float(b0["close"]) < float(b1["low"])

        # C3 wick rejection
        if trap.direction == Direction.LONG:
            c3 = _lower_wick(b1) > _body(b1) * 0.8
        else:
            c3 = _upper_wick(b1) > _body(b1) * 0.8

        # C4 micro-structure shift proxy over last few bars
        x = df.iloc[-6:]
        if trap.direction == Direction.LONG:
            c4 = float(x["low"].iloc[-1]) > float(x["low"].iloc[-3]) and float(x["close"].iloc[-1]) > float(x["close"].iloc[-2])
        else:
            c4 = float(x["high"].iloc[-1]) < float(x["high"].iloc[-3]) and float(x["close"].iloc[-1]) < float(x["close"].iloc[-2])

        details = {"C1_close_quality": c1, "C2_followthrough": c2, "C3_wick_rejection": c3, "C4_micro_shift": c4}
        cnt = sum(bool(v) for v in details.values())
        passed = cnt >= self.cfg.confirmation_min_count
        score = _clamp01(cnt / 4.0)

        reason = f"confirmers={details}, passed={cnt}/4"
        return ConfirmationResult(passed=passed, count_passed=cnt, details=details, score=score, reason=reason)

    # ---------- Gate 5: edge ----------

    def edge_filter(self, signal_strength: float, direction: Direction, market_price: float) -> EdgeResult:
        """
        Binary-specific edge model.

        p_fair mapping:
          p_fair = 0.5 + alpha*(signal_strength-0.5)
          alpha>1 increases separation but clips to [0.02,0.98].

        edge:
          LONG  -> p_fair - p_market
          SHORT -> (1-p_fair) - p_market

        NOTE:
          Ensure market_price corresponds to the traded contract stream.
          If you pass YES-on-UP market for LONG and YES-on-DOWN market for SHORT,
          adapt caller or this function accordingly.
        """
        p_market = float(np.clip(market_price, 0.001, 0.999))

        alpha = 1.35
        p_fair = float(np.clip(0.5 + alpha * (signal_strength - 0.5), 0.02, 0.98))

        if direction == Direction.LONG:
            edge = p_fair - p_market
        elif direction == Direction.SHORT:
            edge = (1.0 - p_fair) - p_market
        else:
            edge = -1.0

        passed = edge > self.cfg.edge_min
        reason = f"edge={edge:.4f} vs min={self.cfg.edge_min:.4f}"

        return EdgeResult(
            passed=passed,
            p_fair=p_fair,
            p_market=p_market,
            edge=float(edge),
            edge_min=self.cfg.edge_min,
            reason=reason,
        )

    # ---------- helpers ----------

    def _reject(
        self,
        df: pd.DataFrame,
        market_price: float,
        regime: RegimeResult,
        reasoning: List[str],
        trap: Optional[TrapSignal] = None,
        p_fair: float = 0.5,
        edge: float = 0.0,
        direction: Direction = Direction.FLAT,
    ) -> TradeSignal:
        return TradeSignal(
            should_trade=False,
            direction=direction,
            confidence=0.0,
            edge=edge,
            entry_price=float(df["close"].iloc[-1]),
            trap=trap.trap_type if trap and trap.fired else None,
            reasoning=reasoning,
            p_fair=p_fair,
            p_market=float(market_price),
            regime=regime.regime,
        )


# =========================
# Example usage
# =========================

if __name__ == "__main__":
    # Demo synthetic usage.
    # Replace with real 5m candles (OHLCV DataFrame).
    np.random.seed(7)
    n = 220
    px = 100 + np.cumsum(np.random.normal(0, 0.45, n))
    op = px + np.random.normal(0, 0.10, n)
    cl = px + np.random.normal(0, 0.10, n)
    hi = np.maximum(op, cl) + np.abs(np.random.normal(0.20, 0.08, n))
    lo = np.minimum(op, cl) - np.abs(np.random.normal(0.20, 0.08, n))
    vol = np.random.randint(100, 500, n)

    candles = pd.DataFrame({
        "open": op,
        "high": hi,
        "low": lo,
        "close": cl,
        "volume": vol,
    })

    engine = SignalEngine(EngineConfig())
    market_price = 0.53
    sig = engine.should_trade(candles, market_price)

    print(sig)
