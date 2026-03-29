import requests
import json
from datetime import datetime, timezone, timedelta
from .config import GAMMA_API

CLOB_API = "https://clob.polymarket.com"

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


ASSET_SLUGS = {
    "BTC": "btc-updown-5m",
    "ETH": "eth-updown-5m",
    "SOL": "sol-updown-5m",
    "XRP": "xrp-updown-5m",
}

def get_5m_slug_candidates(asset: str = "BTC"):
    prefix = ASSET_SLUGS.get(asset, "btc-updown-5m")
    now = datetime.now(timezone.utc)
    minutes = (now.minute // 5) * 5
    base = now.replace(minute=minutes, second=0, microsecond=0)
    slugs = []
    for offset in [0, 5, 10, -5]:
        ts = int((base + timedelta(minutes=offset)).timestamp())
        slugs.append(f"{prefix}-{ts}")
    return slugs

def get_btc_5m_slug_candidates():
    return get_5m_slug_candidates("BTC")


def get_clob_price(token_id: str) -> float:
    try:
        resp = SESSION.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=3
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("mid", 0))
    except Exception as e:
        if "404" in str(e):
            return None  # Market closed/delisted
        print(f"CLOB price error: {e}")
        return 0.0


def get_clob_market(token_id: str) -> dict:
    """Fetch market data directly from CLOB"""
    try:
        resp = SESSION.get(
            f"{CLOB_API}/markets/{token_id}",
            timeout=3
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"CLOB market error: {e}")
        return {}


def get_market_status(slug: str) -> dict:
    try:
        resp = SESSION.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            timeout=5
        )
        resp.raise_for_status()
        m = resp.json()
        if isinstance(m, list):
            m = m[0]
        return {
            'active': m.get('active', True),
            'closed': m.get('closed', False),
        }
    except Exception as e:
        print(f"Market status error: {e}")
        return {'active': True, 'closed': False}


def find_nearest_active_market(asset: str = "BTC"):
    """Find nearest active 5m market for any asset."""
    slugs = get_5m_slug_candidates(asset)
    for slug in slugs:
        try:
            resp = SESSION.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=5)
            resp.raise_for_status()
            markets = resp.json()
            if not markets: continue
            m = markets[0] if isinstance(markets, list) else markets

            active = m.get("active", True)
            closed = m.get("closed", False)
            if closed or not active: continue

            # Get token IDs from clobTokenIds
            import json as _json
            clob_ids = m.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str):
                clob_ids = _json.loads(clob_ids)
            if len(clob_ids) < 2: continue

            up_token   = clob_ids[0]
            down_token = clob_ids[1]

            # Get prices from CLOB
            up_price   = get_clob_price(up_token) or 0.5
            down_price = get_clob_price(down_token) or 0.5

            if up_price >= 0.99 or down_price >= 0.99: continue
            if up_price <= 0.01 or down_price <= 0.01: continue

            print(f"Found market: {m.get('question', slug)} | Up: {up_price:.3f} Down: {down_price:.3f}")
            return {
                "slug":       slug,
                "market_id":  m.get("conditionId", slug),
                "up_token":   up_token,
                "down_token": down_token,
                "up_price":   up_price,
                "down_price": down_price,
                "question":   m.get("question", ""),
            }
        except Exception as e:
            continue
    return None

def find_nearest_active_btc_5m_market():
    """
    Try CLOB first for speed, fall back to Gamma if needed
    """
    slugs = get_btc_5m_slug_candidates()

    for slug in slugs:
        try:
            resp = SESSION.get(
                f"{CLOB_API}/markets",
                params={"slug": slug},
                timeout=3
            )
            if resp.status_code == 200:
                data = resp.json()
                markets = data if isinstance(data, list) else data.get('data', [])

                if not markets:
                    continue

                m = markets[0]
                if m.get('closed'):
                    continue

                tokens = m.get('tokens', [])
                if len(tokens) < 2:
                    continue

                up_token   = tokens[0].get('token_id')
                down_token = tokens[1].get('token_id')
                up_price   = float(tokens[0].get('price', 0))
                down_price = float(tokens[1].get('price', 0))

                live_up   = get_clob_price(up_token)
                live_down = get_clob_price(down_token)

                if live_up > 0:
                    up_price = live_up
                if live_down > 0:
                    down_price = live_down

                print(f"Found market: {m.get('question', slug)} | Up: {up_price:.3f} Down: {down_price:.3f}")
                return {
                    'market_id': m.get('condition_id', m.get('market_id', '')),
                    'slug': slug,
                    'up_price': up_price,
                    'down_price': down_price,
                    'up_token': up_token,
                    'down_token': down_token,
                }
        except Exception as e:
            pass 

    for slug in slugs:
        try:
            resp = SESSION.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                timeout=5
            )
            resp.raise_for_status()
            markets = resp.json()

            if isinstance(markets, dict):
                markets = [markets]
            elif isinstance(markets, str) or not markets:
                continue

            m = markets[0]
            # Don't skip closed markets — we need final prices for resolution

            outcome_prices = m.get('outcomePrices', '[]')
            clob_token_ids = m.get('clobTokenIds', '[]')

            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)

            if len(outcome_prices) >= 2 and len(clob_token_ids) >= 2:
                up_token   = clob_token_ids[0]
                down_token = clob_token_ids[1]

                up_price   = get_clob_price(up_token)
                down_price = get_clob_price(down_token)

                if up_price == 0.0:
                    up_price = float(outcome_prices[0])
                if down_price == 0.0:
                    down_price = float(outcome_prices[1])

                print(f"Found market: {m['question']} | Up: {up_price:.3f} Down: {down_price:.3f}")
                return {
                    'market_id': m['id'],
                    'slug': m.get('slug'),
                    'up_price': up_price,
                    'down_price': down_price,
                    'up_token': up_token,
                    'down_token': down_token,
                }

        except Exception as e:
            print(f"Gamma API error for slug {slug}: {e}")

    return None


def get_market_by_id(market_id: str, up_token: str = None, down_token: str = None, slug: str = None):
    status = get_market_status(slug if slug else market_id)

    up_price   = get_clob_price(up_token) if up_token else 0.0
    down_price = get_clob_price(down_token) if down_token else 0.0

    # If CLOB returns 0, fetch outcome prices from Gamma API
    if (up_price == 0.0 or down_price == 0.0) and (slug or market_id):
        try:
            import json as _json
            _slug = slug if slug else market_id
            _r = SESSION.get(f"{GAMMA_API}/markets", params={"slug": _slug}, timeout=5)
            _m = _r.json()
            if isinstance(_m, list) and _m:
                _m = _m[0]
            _prices = _m.get("outcomePrices", "[]")
            if isinstance(_prices, str):
                _prices = _json.loads(_prices)
            if len(_prices) >= 2:
                if up_price == 0.0:
                    up_price = float(_prices[0])
                if down_price == 0.0:
                    down_price = float(_prices[1])
        except Exception as _e:
            print(f"Gamma price fallback error: {_e}")

    return {
        'market_id': market_id,
        'up_price': up_price,
        'down_price': down_price,
        'active': status['active'],
        'closed': status['closed'],
    }


def place_polymarket_order(direction: str, size_usdc: float, price: float):
    print(f"WOULD PLACE ORDER → {direction.upper()} | size: {size_usdc:.2f} USDC | limit ≈ {price:.3f}")
    return {"status": "simulated", "entry_price": price}
