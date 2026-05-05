#!/usr/bin/env python3
"""
Price Action Trap Bot — Hyperliquid Perps via CoreWriter
Uses signal_engine.py for trap detection + CoreWriter for execution
"""
import os, time, sys, requests, sqlite3, threading
from datetime import datetime, timezone
from dotenv import load_dotenv
import pandas as pd

sys.path.insert(0, '/root/btc-ema-polymarket-bot')
sys.path.insert(0, '/root/corewriter-sdk')

from signal_engine import SignalEngine, EngineConfig, Direction, TrapType
from corewriter import CoreWriter

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
PRIVATE_KEY  = os.getenv('HL_PRIVATE_KEY')
RISK_PCT     = float(os.getenv('RISK_PCT', '0.05'))       # 0.5% per trade
SL_ATR_MULT  = float(os.getenv('SL_ATR_MULT', '1.5'))
TP_ATR_MULT  = float(os.getenv('TP_ATR_MULT', '2.5'))
DRY_RUN      = os.getenv('DRY_RUN', 'true').lower() != 'false'
DISCORD_WEBHOOK = os.getenv('DISCORD_WEBHOOK', '')
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DB_PATH      = os.path.expanduser('~/perp_trades.db')

LEVERAGE = int(os.getenv('LEVERAGE', '50'))

ASSETS = {
    'BTC': {'symbol': 'BTCUSDT', 'asset_idx': 0,  'min_size': 0.001, 'leverage': 40},
    'ETH': {'symbol': 'ETHUSDT', 'asset_idx': 1,  'min_size': 0.01,  'leverage': 25},
}

# ── Init ──────────────────────────────────────────────────────────────────────
engine = SignalEngine(EngineConfig())
cw = CoreWriter(PRIVATE_KEY) if not DRY_RUN and PRIVATE_KEY else None

def notify(msg):
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK,
                json={'content': msg}, timeout=3)
        except: pass
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
                timeout=3)
        except: pass

def init_db():
    c = sqlite3.connect(DB_PATH)
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_open TEXT, ts_close TEXT,
        asset TEXT, direction TEXT,
        entry_price REAL, exit_price REAL,
        size REAL, pnl REAL,
        trap_type TEXT, confidence REAL, edge REAL,
        sl REAL, tp REAL,
        outcome TEXT, dry_run INTEGER
    )''')
    c.commit(); c.close()

def get_candles(symbol, limit=200):
    r = requests.get('https://api.binance.com/api/v3/klines',
        params={'symbol': symbol, 'interval': '1m', 'limit': limit}, timeout=10)
    data = r.json()
    df = pd.DataFrame(data, columns=['ts','open','high','low','close','volume','close_ts','qvol','trades','tb','tq','ignore'])
    df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].astype(float)
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    return df.set_index('ts')

def get_perp_price(asset_idx):
    r = requests.post('https://api.hyperliquid.xyz/info', 
        json={'type': 'metaAndAssetCtxs'}, timeout=5)
    return float(r.json()[1][asset_idx].get('midPx', 0))

def get_account_value():
    if not cw: return 100.0  # dry run default
    bal = cw.get_perp_balance()
    return float(bal.get('account_value', 1000))

def calc_size(asset_idx, entry_price, atr):
    account = get_account_value()
    risk_usd = account * RISK_PCT
    sl_dist = SL_ATR_MULT * atr
    # For perps: size = risk_usd / sl_dist
    size = risk_usd / sl_dist
    min_size = list(ASSETS.values())[asset_idx]['min_size'] if asset_idx < len(ASSETS) else 0.001
    return max(min_size, round(size, 4))

active_positions = {}

def monitor_position(asset, tid, direction, entry, sl, tp, asset_idx):
    def _run():
        print(f"  [{asset}] Monitoring {direction} @ {entry:.2f} SL={sl:.2f} TP={tp:.2f}")
        while True:
            try:
                cur = get_perp_price(asset_idx)
                if cur <= 0:
                    time.sleep(5); continue

                hit_sl = cur <= sl if direction == 'LONG' else cur >= sl
                hit_tp = cur >= tp if direction == 'LONG' else cur <= tp

                if hit_tp or hit_sl:
                    outcome = 'WIN' if hit_tp else 'LOSS'
                    pnl_dir = 1 if direction == 'LONG' else -1
                    size = active_positions.get(asset, {}).get('size', 0)
                    pnl = round((cur - entry) * pnl_dir * size, 4)

                    # Close position
                    if not DRY_RUN and cw:
                        cw.place_perp_order(
                            asset=asset_idx,
                            is_buy=(direction == 'SHORT'),  # close = opposite
                            limit_price=cur * (0.999 if direction == 'LONG' else 1.001),
                            size=size,
                            reduce_only=True,
                            tif=2
                        )

                    # Update DB
                    c = sqlite3.connect(DB_PATH)
                    c.execute('UPDATE trades SET ts_close=?,exit_price=?,pnl=?,outcome=? WHERE id=?',
                        (datetime.now(timezone.utc).isoformat(), cur, pnl, outcome, tid))
                    c.commit(); c.close()

                    icon = '💰' if outcome == 'WIN' else '❌'
                    msg = f'{icon} **{asset} {direction}**\nEntry: {entry:.2f} → Exit: {cur:.2f}\nP&L: **${pnl:+.2f}** | {outcome}'
                    print(f"  [{asset}] {outcome} @ {cur:.2f} P&L: ${pnl:+.2f}")
                    c2 = sqlite3.connect(DB_PATH)
                    total_pnl = c2.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts_close IS NOT NULL").fetchone()[0]
                    wins = c2.execute("SELECT COUNT(*) FROM trades WHERE outcome='WIN'").fetchone()[0]
                    losses = c2.execute("SELECT COUNT(*) FROM trades WHERE outcome='LOSS'").fetchone()[0]
                    c2.close()
                    msg += f'\nSession P&L: **${total_pnl:+.2f}** | W:{wins} L:{losses}'
                    notify(msg)
                    active_positions.pop(asset, None)
                    break

                move = cur - entry if direction == 'LONG' else entry - cur
                print(f"  [{asset}] {direction} @ {entry:.2f} → {cur:.2f} | move: {move:+.2f} | SL={sl:.2f} TP={tp:.2f}")
                time.sleep(15)

            except Exception as e:
                print(f"  [{asset}] Monitor error: {e}")
                time.sleep(5)

    threading.Thread(target=_run, daemon=True).start()


def reconcile_open_trades():
    """On startup, check open trades and close if TP/SL hit, or restart monitor."""
    import requests as _req
    c = sqlite3.connect(DB_PATH)
    rows = c.execute("SELECT id, asset, direction, entry_price, sl, tp, size FROM trades WHERE ts_close IS NULL").fetchall()
    if not rows:
        c.close()
        return
    print(f"[Reconcile] Found {len(rows)} open trades from previous session")
    from datetime import datetime, timezone
    for tid, asset, direction, entry, sl, tp, size in rows:
        try:
            cfg = next((v for k,v in ASSETS.items() if k==asset), None)
            if not cfg: continue
            r = _req.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": asset+"USDT"}, timeout=5)
            cur = float(r.json()["price"])
            hit_tp = cur >= tp if direction == "LONG" else cur <= tp
            hit_sl = cur <= sl if direction == "LONG" else cur >= sl
            if hit_tp:
                pnl = round((tp - entry) * size, 4) if direction == "LONG" else round((entry - tp) * size, 4)
                c.execute("UPDATE trades SET ts_close=?,exit_price=?,pnl=?,outcome=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), tp, pnl, "WIN", tid))
                print(f"[Reconcile] #{tid} {asset} TP hit → WIN ${pnl:+.2f}")
            elif hit_sl:
                pnl = round((sl - entry) * size, 4) if direction == "LONG" else round((entry - sl) * size, 4)
                c.execute("UPDATE trades SET ts_close=?,exit_price=?,pnl=?,outcome=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), sl, pnl, "LOSS", tid))
                print(f"[Reconcile] #{tid} {asset} SL hit → LOSS ${pnl:+.2f}")
            else:
                # Still open — restart monitor
                active_positions[asset] = {"size": size, "entry": entry, "direction": direction}
                df = get_candles(cfg["symbol"])
                df_prep = engine._prepare(df)
                atr = float(df_prep["atr14"].iloc[-1])
                monitor_position(asset, tid, direction, entry, sl, tp, cfg["asset_idx"])
                print(f"[Reconcile] #{tid} {asset} {direction} @ {entry:.2f} — monitor restarted")
        except Exception as e:
            print(f"[Reconcile] Error #{tid}: {e}")
    c.commit()
    c.close()

def run():
    init_db()
    reconcile_open_trades()
    print(f"🚀 Price Action Trap Bot | DRY_RUN={DRY_RUN}")
    print(f"   Assets: {list(ASSETS.keys())} | Risk: {RISK_PCT*100}% | SL: {SL_ATR_MULT}x ATR | TP: {TP_ATR_MULT}x ATR")
    notify(f'🚀 **Trap Bot Started**\nDRY_RUN: {DRY_RUN}\nRisk: {RISK_PCT*100}% | SL: {SL_ATR_MULT}xATR | TP: {TP_ATR_MULT}xATR')

    while True:
        try:
            now = datetime.now(timezone.utc).strftime('%H:%M:%S')

            for asset, cfg in ASSETS.items():
                if asset in active_positions:
                    continue

                # Get candles
                df = get_candles(cfg['symbol'])

                # Get current perp price for market_price (normalize 0-1 for engine)
                perp_px = get_perp_price(cfg['asset_idx'])

                # Run signal engine
                result = engine.should_trade(df, market_price=0.5)

                # Direction-aware edge fix for perps
                if result.should_trade:
                    p_fair = result.p_fair
                    perp_edge = p_fair - 0.5 if result.direction == Direction.LONG else 0.5 - p_fair
                    if perp_edge <= engine.cfg.edge_min:
                        print(f"  [{asset}] Edge filtered SHORT ({perp_edge:.4f})")
                        continue

                if result.should_trade:
                    # Get ATR for sizing
                    df_prep = engine._prepare(df)
                    atr = float(df_prep['atr14'].iloc[-1])
                    entry = perp_px

                    # SL/TP based on ATR
                    # Skip T1_FAILED_BREAKOUT — negative expectancy
                    trap_str = result.reasoning[2] if len(result.reasoning) > 2 else ''
                    if 'T1_FAILED_BREAKOUT' in trap_str:
                        print(f"  [{asset}] Skip T1_FAILED_BREAKOUT")
                        continue

                    # Per-trap TP/SL
                    if 'T2_STOP_SWEEP' in trap_str:
                        sl_mult, tp_mult = 1.4, 2.4
                    elif 'T4_OUTSIDE_DOUBLE_TRAP' in trap_str:
                        sl_mult, tp_mult = 1.5, 3.2
                    else:
                        sl_mult, tp_mult = SL_ATR_MULT, TP_ATR_MULT

                    if result.direction == Direction.LONG:
                        sl = entry - sl_mult * atr
                        tp = entry + tp_mult * atr
                    else:
                        sl = entry + sl_mult * atr
                        tp = entry - tp_mult * atr

                    size = calc_size(cfg['asset_idx'], entry, atr)
                    trap = result.reasoning[2] if len(result.reasoning) > 2 else 'unknown'

                    print(f"\n🎯 [{now}] {asset} {result.direction.value} SIGNAL")
                    print(f"   Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
                    print(f"   Size: {size} | Conf: {result.confidence:.2f} | {trap}")

                    # Set leverage per asset
                    if not DRY_RUN and cw:
                        cw.set_leverage(cfg['asset_idx'], cfg['leverage'])

                    # Place order
                    if not DRY_RUN and cw:
                        tx, status = cw.place_perp_order(
                            asset=cfg['asset_idx'],
                            is_buy=(result.direction == Direction.LONG),
                            limit_price=entry,
                            size=size,
                            tif=2
                        )
                        print(f"   TX: {tx} status={status}")
                    else:
                        print(f"   [DRY RUN] Would place {result.direction.value} {size} {asset} @ {entry:.2f}")

                    # Log to DB
                    c = sqlite3.connect(DB_PATH)
                    c.execute('''INSERT INTO trades 
                        (ts_open,asset,direction,entry_price,size,trap_type,confidence,edge,sl,tp,dry_run)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                        (datetime.now(timezone.utc).isoformat(), asset,
                         result.direction.value, entry, size,
                         trap, result.confidence, result.edge, sl, tp,
                         1 if DRY_RUN else 0))
                    c.commit()
                    tid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
                    c.close()

                    active_positions[asset] = {'size': size, 'entry': entry, 'direction': result.direction.value}

                    notify(f'🎯 **{asset} {result.direction.value}**\nEntry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}\nConf: {result.confidence:.2f} | {trap}')

                    # Start monitor
                    monitor_position(asset, tid, result.direction.value, entry, sl, tp, cfg['asset_idx'])

                else:
                    regime = result.regime.value if result.regime else 'UNK'
                    print(f"[{now}] {asset}: {regime} | No signal")

            time.sleep(60)

        except KeyboardInterrupt:
            print("\nStopping...")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(10)

if __name__ == '__main__':
    run()
