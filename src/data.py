import pandas as pd
import requests
import time
import json
import os
import tempfile
from .config import EMA_PERIOD

# Shared price cache — all bot instances share one Pyth request per asset
CACHE_DIR = '/tmp/raptor_price_cache'
CACHE_TTL = 30  # seconds

os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_path(asset, minutes):
    return os.path.join(CACHE_DIR, f'{asset}_{minutes}.json')

def _read_cache(asset, minutes):
    path = _cache_path(asset, minutes)
    try:
        if not os.path.exists(path):
            return None
        age = time.time() - os.path.getmtime(path)
        if age > CACHE_TTL:
            return None
        with open(path, 'r') as f:
            data = json.load(f)
        return pd.DataFrame(data['records'], columns=data['columns']).pipe(
            lambda df: df.assign(timestamp=pd.to_datetime(df['timestamp'], utc=True))
        ).set_index('timestamp')
    except:
        return None

def _write_cache(asset, minutes, df):
    path = _cache_path(asset, minutes)
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({
                'columns': list(df.reset_index().columns),
                'records': df.reset_index().values.tolist()
            }, f, default=str)
        os.replace(tmp, path)
    except:
        pass

PYTH_HERMES = "https://hermes.pyth.network"
BTC_FEED_ID = "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"

PYTH_SYMBOLS = {
    "BTC": "Crypto.BTC/USD",
    "ETH": "Crypto.ETH/USD",
    "SOL": "Crypto.SOL/USD",
    "XRP": "Crypto.XRP/USD",
}


def fetch_pyth_prices(minutes: int = 100, asset: str = "BTC") -> pd.DataFrame:
    """Fetch price updates from Pyth for given asset (with shared cache)."""
    cached = _read_cache(asset, minutes)
    if cached is not None:
        return cached

    now = int(time.time())
    start = now - (minutes * 60)

    url = f"https://benchmarks.pyth.network/v1/shims/tradingview/history"
    params = {
        "symbol": PYTH_SYMBOLS.get(asset, "Crypto.BTC/USD"),
        "resolution": "1",  # 1 minute
        "from": start,
        "to": now,
    }

    try:
        # Retry up to 3 times
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=10)
                resp.raise_for_status()
                break
            except Exception as retry_err:
                if attempt == 2:
                    raise retry_err
                time.sleep(2)

        data = resp.json()

        if data.get("s") != "ok":
            raise ValueError(f"Pyth returned status: {data.get('s')}")

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(data["t"], unit="s", utc=True),
            "open":  data["o"],
            "high":  data["h"],
            "low":   data["l"],
            "close": data["c"],
            "volume": data.get("v", [0] * len(data["t"])),
        })
        df.set_index("timestamp", inplace=True)
        return df

    except Exception as e:
        print(f"Pyth fetch error: {e}")
        return pd.DataFrame()


def fetch_candles(n: int = 100, asset: str = "BTC") -> pd.DataFrame:
    """Fetch 1m candles from Pyth."""
    df = fetch_pyth_prices(minutes=n, asset=asset)
    if df.empty:
        return df
    return df.tail(n)


def calculate_ema(df: pd.DataFrame) -> pd.DataFrame:
    df['ema8'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    return df


def fetch_5m_candles(n: int = 50, asset: str = "BTC") -> pd.DataFrame:
    """Fetch 1m candles from Pyth and resample to 5m."""
    df = fetch_pyth_prices(minutes=n * 5, asset=asset)
    if df.empty:
        return df

    df_5m = df.resample('5min').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    }).dropna()

    return df_5m.tail(n)


def calculate_5m_ema(df: pd.DataFrame) -> pd.DataFrame:
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    return df
