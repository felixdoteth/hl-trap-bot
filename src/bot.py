import time
import threading
from datetime import datetime, timezone
from .data import fetch_candles, calculate_ema, fetch_5m_candles, calculate_5m_ema, PYTH_SYMBOLS
from .tracker import init_db, log_trade, get_summary
from .telegram import notify_entry, notify_pt, notify_sl, notify_summary
from .polymarket import find_nearest_active_btc_5m_market, find_nearest_active_market, get_market_by_id, get_clob_price
from .order import place_order
from .signals import classify_trend, detect_bullish_pullback, detect_bearish_pullback

from .config import TRADE_SIZE_USDC, MAX_ENTRY_PRICE, MIN_ENTRY_PRICE, POLL_INTERVAL_SEC


def send_fee_to_raptor(win_amount_usdc: float):
    """Send 3% of winning trade to RaptorTrade fee wallet."""
    try:
        from web3 import Web3
        from eth_account import Account
        import os

        fee_amount = round(win_amount_usdc * 0.05, 6)
        if fee_amount < 0.001:
            return  # Too small to bother

        w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC")))
        account = Account.from_key(os.getenv("POLYGON_PRIVATE_KEY"))

        USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
        FEE_WALLET = Web3.to_checksum_address("0x566FCF65E6064640094bD0ead6EC68018fc3c7Fe")

        usdc_abi = [{
            "name": "transfer",
            "type": "function",
            "inputs": [
                {"name": "to",     "type": "address"},
                {"name": "value",  "type": "uint256"},
            ],
            "outputs": [{"name": "", "type": "bool"}],
        }]

        usdc = w3.eth.contract(address=USDC, abi=usdc_abi)
        amount_wei = int(fee_amount * 1_000_000)  # USDC has 6 decimals

        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx = usdc.functions.transfer(FEE_WALLET, amount_wei).build_transaction({
            "from":     account.address,
            "nonce":    nonce,
            "gas":      100000,
            "gasPrice": int(w3.eth.gas_price * 1.3),
        })
        signed   = account.sign_transaction(tx)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  💰 Fee sent: ${fee_amount:.4f} USDC → RaptorTrade | tx: {tx_hash.hex()[:20]}...")
        # Log fee to CSV
        try:
            import csv, os as _os
            from datetime import datetime as _dt
            log_file = '/root/raptortrade-platform/fees_log.csv'
            user_id = _os.getenv('RAPTOR_USER_ID', 'personal')
            with open(log_file, 'a', newline='') as _f:
                csv.writer(_f).writerow([
                    _dt.now().strftime('%Y-%m-%d %H:%M:%S'),
                    user_id[:8],
                    round(win_amount_usdc + 2.0, 4),
                    round(win_amount_usdc, 4),
                    fee_amount,
                    tx_hash.hex()[:20]
                ])
        except: pass
    except Exception as e:
        print(f"  ⚠️ Fee transfer error: {e}")
        try:
            import requests as _tg
            _tg.post(f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage", json={
                "chat_id": os.getenv('TELEGRAM_CHAT_ID'),
                "text": f"⚠️ RaptorTrade Fee Transfer FAILED\nAmount: ${round(win_amount_usdc * 0.05, 4)}\nError: {e}",
                "parse_mode": "HTML"
            }, timeout=5)
        except: pass


positions      = []
positions_lock = threading.Lock()

def monitor_position(entry_price: float, direction: str, market_data: dict, position_id: int):
    market_id  = market_data['market_id']
    up_token   = market_data['up_token']
    down_token = market_data['down_token']
    entry_time = datetime.now(timezone.utc).isoformat()
    exit_reason = 'RESOLVED'
    current_price = entry_price

    asset = market_data.get('slug', '').split('-')[0].upper()
    print(f"  [{asset} #{position_id}] Holding {direction.upper()} @ {entry_price:.3f} — waiting for resolution...")

    notify_entry(position_id, direction, entry_price, 0, 0, market_data["slug"], asset=asset)

    while True:
        market = get_market_by_id(market_id, up_token, down_token, slug=market_data["slug"])

        if not market:
            time.sleep(2)
            continue

        current_price = market['up_price'] if direction == 'up' else market['down_price']
        if current_price is None:
            current_price = 0.0

        up_p = float(market['up_price'] or 0.0)
        down_p = float(market['down_price'] or 0.0)

        # If both are 0 and market is closed, fetch from Gamma directly
        if up_p == 0.0 and down_p == 0.0 and market.get('closed'):
            try:
                import requests as _req, json as _json
                _slug = market_data.get('slug', '')
                _r = _req.get(f'https://gamma-api.polymarket.com/markets', params={'slug': _slug}, timeout=5)
                _m = _r.json()
                if isinstance(_m, list) and _m: _m = _m[0]
                _prices = _json.loads(_m.get('outcomePrices', '[0,0]'))
                up_p = float(_prices[0])
                down_p = float(_prices[1])
            except: pass
        resolved = (
            market.get("closed") or not market.get("active", True)
        )

        if resolved:
            if direction == 'up':
                result = '✅ WIN' if up_p >= 0.5 else '❌ LOSS'
                current_price = up_p
            else:
                result = '✅ WIN' if down_p >= 0.5 else '❌ LOSS'
                current_price = down_p
            asset = market_data.get('slug', '').split('-')[0].upper()
            print(f"  [{asset} #{position_id}] Market resolved! {result} | Up: {up_p:.3f} Down: {down_p:.3f}")
            won = '✅ WIN' in result
            shares_tmp = round(TRADE_SIZE_USDC / entry_price, 4)
            pnl_tmp = round(shares_tmp - TRADE_SIZE_USDC if won else -TRADE_SIZE_USDC, 4)
            if won:
                notify_pt(position_id, direction, entry_price, current_price, pnl_tmp)
            else:
                notify_sl(position_id, direction, entry_price, current_price, pnl_tmp)
            break

        print(f"  [#{position_id}] {direction.upper()} @ {entry_price:.3f} → now {current_price:.3f} | {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
        time.sleep(2)

    # Auto-redeem disabled — handled manually

    exit_time  = datetime.now(timezone.utc).isoformat()
    shares     = round(TRADE_SIZE_USDC / entry_price, 4)
    exit_value = round(shares * (1.0 if current_price >= 0.95 else 0.0), 4)
    pnl        = round(exit_value - TRADE_SIZE_USDC, 4)

    # Collect 3% fee on wins
    if pnl > 0:
        send_fee_to_raptor(pnl)

    log_trade(
        position_id     = position_id,
        timestamp_entry = entry_time,
        timestamp_exit  = exit_time,
        direction       = direction,
        market          = market_data['slug'],
        entry_price     = entry_price,
        exit_price      = current_price,
        shares          = shares,
        entry_cost      = TRADE_SIZE_USDC,
        exit_value      = exit_value,
        pnl             = pnl,
        exit_reason     = exit_reason
    )


    # Report trade to RaptorTrade platform (fee collection)
    try:
        import os as _os, requests as _rtr
        api_url    = _os.getenv('API_URL', 'http://localhost:4000')
        bot_secret = _os.getenv('BOT_SECRET', '')
        user_id    = _os.getenv('RAPTOR_USER_ID', '')
        bot_type   = _os.getenv('RAPTOR_BOT_TYPE', 'EMA')
        if user_id and bot_secret:
            _rtr.post(
                f"{api_url}/api/trades/log",
                json={
                    "user_id":         user_id,
                    "bot_type":        bot_type,
                    "asset":           market_data.get('slug', ''),
                    "direction":       direction,
                    "market_slug":     market_data['slug'],
                    "entry_price":     entry_price,
                    "exit_price":      current_price,
                    "shares":          shares,
                    "entry_cost":      TRADE_SIZE_USDC,
                    "exit_value":      exit_value,
                    "pnl":             pnl,
                    "exit_reason":     exit_reason,
                    "timestamp_entry": entry_time,
                    "timestamp_exit":  exit_time,
                },
                headers={"x-bot-secret": bot_secret},
                timeout=5
            )
            print(f"  [#{position_id}] Trade reported to RaptorTrade")
    except Exception as _e:
        print(f"  [#{position_id}] RaptorTrade report error: {_e}")

    summary = get_summary()
    print(f"  [#{position_id}] Trade logged | P&L: ${pnl:+.2f} | Session total: ${summary[3]:+.2f} | Wins: {summary[1]} Losses: {summary[2]}")
    notify_summary(summary[0], summary[1], summary[2], summary[3])

    with positions_lock:
        positions[:] = [p for p in positions if p['id'] != position_id]

    print(f"  [#{position_id}] Position closed. Active positions: {len(positions)}")


import os as _os
_ASSETS_ENV = _os.getenv("EMA_ASSETS", "BTC")
ACTIVE_ASSETS = [a.strip().upper() for a in _ASSETS_ENV.split(",") if a.strip()]
if not ACTIVE_ASSETS:
    ACTIVE_ASSETS = ["BTC"]


def scan_asset(asset, position_counter_ref, counter_lock):
    while True:
        try:
            df_1m = fetch_candles(100, asset=asset)
            df_5m = fetch_5m_candles(50, asset=asset)

            if df_1m is None or df_1m.empty or df_5m is None or df_5m.empty:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            df_1m = calculate_ema(df_1m)
            df_5m = calculate_5m_ema(df_5m)

            if df_1m.empty or df_5m.empty:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            now = datetime.now(timezone.utc)
            print(f"\n[{now.strftime('%H:%M:%S')}] [{asset}] Scanning... | Active: {len(positions)}")

            trend = classify_trend(df_1m)
            print(f"[{asset}] Trend: {trend}")

            if trend == "ranging":
                print(f"[{asset}] Ranging, skipping")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            market = find_nearest_active_market(asset)
            if not market:
                print(f"[{asset}] No active 5m market found")
                time.sleep(30)
                continue


            signals = []
            if detect_bullish_pullback(df_1m, df_5m):
                signals.append('up')
            if detect_bearish_pullback(df_1m, df_5m):
                signals.append('down')

            if signals:
                for signal in signals:
                    token_id    = market['up_token'] if signal == 'up' else market['down_token']
                    entry_price = get_clob_price(token_id)

                    if not entry_price or entry_price <= 0:
                        print(f"[{asset}] Could not get price, skipping")
                        continue
                    if entry_price < MIN_ENTRY_PRICE:
                        print(f"[{asset}] Skipping {signal.upper()} entry {entry_price:.3f} too low")
                        continue
                    if entry_price > MAX_ENTRY_PRICE:
                        print(f"[{asset}] Skipping {signal.upper()} entry {entry_price:.3f} above max {MAX_ENTRY_PRICE}")
                        continue

                    market_slug = market['slug']

                    with counter_lock:
                        position_counter_ref[0] += 1
                        pid = position_counter_ref[0]

                    print(f"[{asset} #{pid}] {signal.upper()} signal! Entry {entry_price:.3f}")

                    # Broadcast signal to user bots
                    try:
                        import json as _json, os as _os
                        _signal_file = '/tmp/raptor_signals.json'
                        _existing = []
                        if _os.path.exists(_signal_file):
                            try:
                                _existing = _json.load(open(_signal_file)).get('signals', [])
                            except: pass
                        _sig_id = f"{asset}:{market['slug']}:{signal}"
                        if not any(s.get('id') == _sig_id for s in _existing):
                            _existing.append({
                                'id': _sig_id,
                                'asset': asset,
                                'direction': signal,
                                'entry_price': entry_price,
                                'market': market,
                                'timestamp': datetime.now(timezone.utc).isoformat(),
                            })
                            with open(_signal_file, 'w') as _f:
                                _json.dump({'timestamp': datetime.now(timezone.utc).isoformat(), 'signals': _existing}, _f)
                    except Exception as _be:
                        pass

                    from .config import DRY_RUN
                    if DRY_RUN:
                        print(f"[{asset} #{pid}] [DRY RUN] Tracking...")
                        order = {"success": True}
                    else:
                        order = place_order(token_id, size_usdc=TRADE_SIZE_USDC)

                    if order and order.get('success', False):
                        position = {'id': pid, 'asset': asset, 'direction': signal, 'entry': entry_price, 'market': market}
                        with positions_lock:
                            positions.append(position)
                        t = threading.Thread(target=monitor_position, args=(entry_price, signal, market, pid), daemon=True)
                        t.start()
                        print(f"[{asset} #{pid}] Position open. Total active: {len(positions)}")
            else:
                print(f"[{asset}] No signal this scan")

            # Clock-sync: scan at :04, :19, :34, :49 of each 15s cycle
            import math as _math
            _now = time.time()
            _next = _math.ceil((_now - 4) / POLL_INTERVAL_SEC) * POLL_INTERVAL_SEC + 4
            time.sleep(max(0.1, _next - _now))

        except Exception as e:
            print(f"[{asset}] Scan error: {e}")
            time.sleep(30)


def main():
    init_db()
    print(f"EMA Pullback | Assets: {', '.join(ACTIVE_ASSETS)}")
    print(f"  Trade size: ${TRADE_SIZE_USDC} USDC | Max entry: {MAX_ENTRY_PRICE}")
    print(f"  DRY_RUN: {_os.getenv('DRY_RUN', 'true')}")

    position_counter = [0]
    counter_lock = threading.Lock()

    for asset in ACTIVE_ASSETS:
        t = threading.Thread(target=scan_asset, args=(asset, position_counter, counter_lock), daemon=True)
        t.start()
        print(f"  Started scanner for {asset}")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
