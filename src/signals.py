from .config import TREND_CHECK_BARS, MAX_TOUCHES_FOR_RANGE, NO_TOUCH_BARS


def is_touch(row, ema_value: float) -> bool:
    return row['low'] <= ema_value <= row['high']


def classify_trend(df) -> str:
    if len(df) < TREND_CHECK_BARS:
        return "unknown"

    recent = df.iloc[-TREND_CHECK_BARS:]
    touches = recent.apply(lambda row: is_touch(row, row['ema8']), axis=1).sum()

    if touches > MAX_TOUCHES_FOR_RANGE:
        return "ranging"
    return "trending"


def detect_bullish_pullback(df_1m, df_5m) -> bool:
    if len(df_1m) < 4:
        return False

    last_5m = df_5m.iloc[-1]
    if last_5m['close'] <= last_5m['ema20']:
        return False

    recent = df_1m.iloc[-4:]
    prev   = recent.iloc[:2]
    last2  = recent.iloc[2:4]
    curr   = recent.iloc[-1]

    no_touch_prev = not prev.apply(lambda r: is_touch(r, r['ema8']), axis=1).any()
    touch_recent  = last2.apply(lambda r: is_touch(r, r['ema8']), axis=1).any()
    bounce        = curr['close'] > curr['ema8']

    return no_touch_prev and touch_recent and bounce


def detect_bearish_pullback(df_1m, df_5m) -> bool:
    if len(df_1m) < 4:
        return False

    last_5m = df_5m.iloc[-1]
    if last_5m['close'] >= last_5m['ema20']:
        return False

    recent = df_1m.iloc[-4:]
    prev   = recent.iloc[:2]
    last2  = recent.iloc[2:4]
    curr   = recent.iloc[-1]

    no_touch_prev = not prev.apply(lambda r: is_touch(r, r['ema8']), axis=1).any()
    touch_recent  = last2.apply(lambda r: is_touch(r, r['ema8']), axis=1).any()
    bounce        = curr['close'] < curr['ema8']

    return no_touch_prev and touch_recent and bounce



def get_60m_regime(asset: str = "BTC") -> str:
    """Returns BULL, BEAR, or NEUTRAL based on 60m EMA20 vs EMA50"""
    from .data import fetch_candles
    import pandas as pd
    df = fetch_candles(300, asset)
    if df.empty or len(df) < 50:
        return "NEUTRAL"
    df_60m = df.resample('60min').agg({
        'open': 'first', 'high': 'max',
        'low': 'min', 'close': 'last'
    }).dropna()
    df_60m['ema20'] = df_60m['close'].ewm(span=20, adjust=False).mean()
    df_60m['ema50'] = df_60m['close'].ewm(span=50, adjust=False).mean()
    if len(df_60m) < 2:
        return "NEUTRAL"
    last = df_60m.iloc[-1]
    if last['ema20'] > last['ema50']:
        return "BULL"
    elif last['ema20'] < last['ema50']:
        return "BEAR"
    return "NEUTRAL"


def regime_allows(direction: str, regime: str) -> bool:
    """
    BEAR regime → allow UP only
    BULL regime → allow DOWN only
    NEUTRAL → allow both
    """
    if regime == "BEAR" and direction == "down":
        return False
    if regime == "BULL" and direction == "up":
        return False
    return True
