#!/usr/bin/env python3
"""
Price Action Trap Bot — Deriv Synthetic Indices via MT5 (Deriv-Demo)
Mirrors forex_bot.py exactly in structure. Differences from that file:
DB_PATH, MT5_PORT default, ASSETS, MIN_LOTS (empty — not yet observed),
MAGIC, and a startup tick-warmup loop (see run()) that forex_bot.py is
still missing. type_filling (FOK) is carried over as an untested guess,
not a confirmed Deriv requirement.
"""
import os, time, sys, sqlite3, requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from signal_engine import SignalEngine, EngineConfig, Direction, TrapType
from mt5linux import MetaTrader5
import pandas as pd

# ── Config ───────────────────────────────────────────────────────────────
RISK_PCT     = float(os.getenv('RISK_PCT', '0.05'))
SL_ATR_MULT  = float(os.getenv('SL_ATR_MULT', '1.5'))
TP_ATR_MULT  = float(os.getenv('TP_ATR_MULT', '5'))
CAPITAL_BASE = float(os.getenv('CAPITAL_BASE', '50'))
DISCORD_WEBHOOK  = os.getenv('DISCORD_WEBHOOK', '')
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DB_PATH      = os.path.expanduser('~/deriv_trades.db')
MT5_HOST     = os.getenv('MT5_HOST', 'localhost')
MT5_PORT     = int(os.getenv('MT5_PORT', '18002'))
MAGIC        = 234100

ASSETS = {
    'Volatility 10 Index':  {'symbol': 'Volatility 10 Index'},
    'Volatility 25 Index':  {'symbol': 'Volatility 25 Index'},
    'Volatility 50 Index':  {'symbol': 'Volatility 50 Index'},
    'Volatility 75 Index':  {'symbol': 'Volatility 75 Index'},
    'Volatility 100 Index': {'symbol': 'Volatility 100 Index'},
    'Boom 500 Index':       {'symbol': 'Boom 500 Index'},
    'Boom 1000 Index':      {'symbol': 'Boom 1000 Index'},
    'Crash 500 Index':      {'symbol': 'Crash 500 Index'},
    'Crash 1000 Index':     {'symbol': 'Crash 1000 Index'},
}

MIN_LOTS = {}   # empty on purpose — see note above the code

# ── Init ─────────────────────────────────────────────────────────────────
engine = SignalEngine(EngineConfig(use_binary_edge=False))
mt5 = MetaTrader5(host=MT5_HOST, port=MT5_PORT)
if not mt5.initialize():
    print("mt5.initialize() failed:", mt5.last_error())
    sys.exit(1)

def notify(msg):
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK, json={'content': msg}, timeout=3)
        except Exception:
            pass
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
                timeout=3)
        except Exception:
            pass

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
        outcome TEXT, ticket INTEGER
    )''')
    c.commit(); c.close()

def warmup_symbols():
    """Select every symbol and wait for a real (non-zero) tick before the
    main loop starts. Every symbol here is new to this terminal, so all
    nine are at risk of the zero-tick window we diagnosed on the FBS
    bridge (BTCUSD's first attempt) — not optional for this bot."""
    print("Warming up symbols...")
    for asset, cfg in ASSETS.items():
        symbol = cfg['symbol']
        mt5.symbol_select(symbol, True)
        for attempt in range(10):
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and tick.bid != 0:
                print(f"  {asset}: OK ({tick.bid}/{tick.ask})")
                break
            time.sleep(1)
        else:
            print(f"  {asset}: WARNING — no live tick after 10s, may not be tradeable yet")

def get_candles(symbol, limit=200):
    """5m candles via MT5. Last row is the currently forming bar."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, limit)
    df = pd.DataFrame(rates)
    df['ts'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.rename(columns={'tick_volume': 'volume'})
    df = df[['ts', 'open', 'high', 'low', 'close', 'volume']]
    df[['open', 'high', 'low', 'close', 'volume']] = \
        df[['open', 'high', 'low', 'close', 'volume']].astype(float)
    return df.set_index('ts')

def get_account_value():
    info = mt5.account_info()
    return float(info.balance) if info else 100.0

def calc_size(symbol, atr, direction):
    """Currency-general lot sizing via order_calc_profit() — unchanged
    from forex_bot.py, works for any symbol without modification."""
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    account = get_account_value()
    risk_usd = CAPITAL_BASE * RISK_PCT
    sl_dist = SL_ATR_MULT * atr

    order_type = mt5.ORDER_TYPE_BUY if direction == 'LONG' else mt5.ORDER_TYPE_SELL
    price = tick.ask if direction == 'LONG' else tick.bid
    close_price = price - sl_dist if direction == 'LONG' else price + sl_dist

    loss_per_lot = abs(mt5.order_calc_profit(order_type, symbol, 1.0, price, close_price))
    raw_lots = risk_usd / loss_per_lot

    step = info.volume_step
    lots = round(raw_lots / step) * step
    lots = max(lots, MIN_LOTS.get(symbol, 0))
    lots = max(info.volume_min, min(info.volume_max, lots))
    return round(lots, 2)

def place_order(symbol, direction, lot, entry, sl, tp):
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if direction == 'LONG' else mt5.ORDER_TYPE_SELL,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "hl-trap-bot deriv",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    return mt5.order_send(request)

active_positions = {}
last_closed_bar_ts = {}

t4_consecutive_losses = 0
t4_cooldown_until     = None
t2_consecutive_losses = 0
t2_cooldown_until     = None

def _record_close(asset, tid, ticket, direction, entry, trap_type):
    global t4_consecutive_losses, t4_cooldown_until
    global t2_consecutive_losses, t2_cooldown_until

    deals = mt5.history_deals_get(position=ticket)
    close_deal = None
    if deals:
        for d in deals:
            if d.entry == 1:
                close_deal = d
    if close_deal is None:
        print(f"  [{asset}] Could not find closing deal for ticket {ticket} — will retry")
        return False

    exit_price = close_deal.price
    pnl = close_deal.profit
    outcome = 'WIN' if pnl > 0 else 'LOSS'

    c = sqlite3.connect(DB_PATH)
    c.execute('UPDATE trades SET ts_close=?,exit_price=?,pnl=?,outcome=? WHERE id=?',
        (datetime.now(timezone.utc).isoformat(), exit_price, pnl, outcome, tid))
    c.commit()
    total_pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts_close IS NOT NULL").fetchone()[0]
    wins = c.execute("SELECT COUNT(*) FROM trades WHERE outcome='WIN'").fetchone()[0]
    losses = c.execute("SELECT COUNT(*) FROM trades WHERE outcome='LOSS'").fetchone()[0]
    c.close()

    icon = '💰' if outcome == 'WIN' else '❌'
    msg = (f'{icon} **{asset} {direction}**\nEntry: {entry:.2f} → Exit: {exit_price:.2f}\n'
           f'P&L: **${pnl:+.2f}** | {outcome}\nSession P&L: **${total_pnl:+.2f}** | W:{wins} L:{losses}')
    print(f"  [{asset}] {outcome} @ {exit_price:.2f} P&L: ${pnl:+.2f}")
    notify(msg)

    now_dt = datetime.now(timezone.utc)
    if trap_type == TrapType.T4_OUTSIDE_DOUBLE_TRAP:
        if outcome == 'LOSS':
            t4_consecutive_losses += 1
            if t4_consecutive_losses >= 2:
                t4_cooldown_until = now_dt + timedelta(minutes=90)
                notify(f'⏸️ **T4 Cooldown activated** ({t4_consecutive_losses} losses) — 90 min pause')
        else:
            t4_consecutive_losses = 0
            t4_cooldown_until = None
    elif trap_type == TrapType.T2_STOP_SWEEP:
        if outcome == 'LOSS':
            t2_consecutive_losses += 1
            if t2_consecutive_losses >= 3:
                t2_cooldown_until = now_dt + timedelta(minutes=45)
                notify(f'⏸️ **T2 Cooldown activated** ({t2_consecutive_losses} losses) — 45 min pause')
        else:
            t2_consecutive_losses = 0
            t2_cooldown_until = None

    active_positions.pop(asset, None)
    return True

def check_closed_positions():
    for asset, pos in list(active_positions.items()):
        still_open = mt5.positions_get(ticket=pos['ticket'])
        if not still_open:
            _record_close(asset, pos['tid'], pos['ticket'], pos['direction'],
                          pos['entry'], pos['trap_type'])

def reconcile_open_trades():
    c = sqlite3.connect(DB_PATH)
    rows = c.execute(
        "SELECT id, asset, direction, entry_price, size, trap_type, ticket "
        "FROM trades WHERE ts_close IS NULL").fetchall()
    c.close()
    if not rows:
        return
    print(f"[Reconcile] Found {len(rows)} open trades from previous session")
    for tid, asset, direction, entry, size, trap_type, ticket in rows:
        try:
            still_open = mt5.positions_get(ticket=ticket) if ticket else None
            if still_open:
                active_positions[asset] = {
                    'ticket': ticket, 'tid': tid, 'direction': direction,
                    'entry': entry, 'size': size, 'trap_type': trap_type,
                }
                print(f"[Reconcile] #{tid} {asset} {direction} — still open, resuming tracking")
            else:
                _record_close(asset, tid, ticket, direction, entry, trap_type)
                print(f"[Reconcile] #{tid} {asset} — was closed while offline, recorded")
        except Exception as e:
            print(f"[Reconcile] Error #{tid}: {e}")

def run():
    global t4_consecutive_losses, t4_cooldown_until
    global t2_consecutive_losses, t2_cooldown_until
    init_db()
    warmup_symbols()
    reconcile_open_trades()
    print(f"Deriv Trap Bot | Assets: {list(ASSETS.keys())} | Risk: {RISK_PCT*100}%")
    notify(f'**Deriv Trap Bot Started**\nAssets: {list(ASSETS.keys())}\nRisk: {RISK_PCT*100}%')

    while True:
        try:
            now = datetime.now(timezone.utc).strftime('%H:%M:%S')
            check_closed_positions()

            for asset, cfg in ASSETS.items():
                if asset in active_positions:
                    continue

                symbol = cfg['symbol']
                df_raw = get_candles(symbol)

                if len(df_raw) < 2:
                    print(f"[{now}] {asset}: not enough candles")
                    continue

                df = df_raw.iloc[:-1].copy()
                closed_ts = df.index[-1]

                if last_closed_bar_ts.get(asset) == closed_ts:
                    continue

                result = engine.should_trade(df, market_price=0.5)

                if not result.should_trade:
                    regime = result.regime.value if result.regime else 'UNK'
                    print(f"[{now}] {asset}: {regime} | No signal (closed bar {closed_ts})")
                    last_closed_bar_ts[asset] = closed_ts
                    continue

                trap = result.trap
                now_dt = datetime.now(timezone.utc)

                if trap == TrapType.T1_FAILED_BREAKOUT:
                    print(f"  [{asset}] Skip T1_FAILED_BREAKOUT")
                    last_closed_bar_ts[asset] = closed_ts
                    continue

                if trap == TrapType.T4_OUTSIDE_DOUBLE_TRAP and 0.77 <= result.confidence <= 0.81:
                    print(f"  [{asset}] Skip toxic T4 confidence zone ({result.confidence:.2f})")
                    last_closed_bar_ts[asset] = closed_ts
                    continue

                if trap == TrapType.T4_OUTSIDE_DOUBLE_TRAP and t4_cooldown_until:
                    if now_dt < t4_cooldown_until:
                        print(f"  [{asset}] T4 cooldown active until {t4_cooldown_until.strftime('%H:%M:%S')} UTC")
                        last_closed_bar_ts[asset] = closed_ts
                        continue
                    t4_consecutive_losses, t4_cooldown_until = 0, None

                if trap == TrapType.T2_STOP_SWEEP and t2_cooldown_until:
                    if now_dt < t2_cooldown_until:
                        print(f"  [{asset}] T2 cooldown active until {t2_cooldown_until.strftime('%H:%M:%S')} UTC")
                        last_closed_bar_ts[asset] = closed_ts
                        continue
                    t2_consecutive_losses, t2_cooldown_until = 0, None

                df_prep = engine._prepare(df)
                atr = float(df_prep['atr14'].iloc[-1])

                if trap == TrapType.T2_STOP_SWEEP:
                    sl_mult, tp_mult = 1.4, 4.4
                elif trap == TrapType.T4_OUTSIDE_DOUBLE_TRAP:
                    sl_mult, tp_mult = 1.5, 5.2
                else:
                    sl_mult, tp_mult = SL_ATR_MULT, TP_ATR_MULT

                tick = mt5.symbol_info_tick(symbol)
                entry = tick.ask if result.direction == Direction.LONG else tick.bid

                if result.direction == Direction.LONG:
                    sl = entry - sl_mult * atr
                    tp = entry + tp_mult * atr
                else:
                    sl = entry + sl_mult * atr
                    tp = entry - tp_mult * atr

                lot = calc_size(symbol, atr, result.direction.value)

                print(f"\n[{now}] {asset} {result.direction.value} SIGNAL (closed bar {closed_ts})")
                print(f"   Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
                print(f"   Size: {lot} | Conf: {result.confidence:.2f} | {trap.value}")

                res = place_order(symbol, result.direction.value, lot, entry, sl, tp)
                if res.retcode != mt5.TRADE_RETCODE_DONE:
                    print(f"   ORDER REJECTED: retcode={res.retcode} comment={res.comment}")
                    notify(f'**{asset} order rejected**: {res.comment}')
                    continue

                print(f"   Order placed: ticket={res.order}")
                last_closed_bar_ts[asset] = closed_ts

                c = sqlite3.connect(DB_PATH)
                c.execute('''INSERT INTO trades
                    (ts_open,asset,direction,entry_price,size,trap_type,confidence,edge,sl,tp,ticket)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (datetime.now(timezone.utc).isoformat(), asset,
                     result.direction.value, entry, lot,
                     trap.value, result.confidence, result.edge, sl, tp, res.order))
                c.commit()
                tid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
                c.close()

                active_positions[asset] = {
                    'ticket': res.order, 'tid': tid, 'direction': result.direction.value,
                    'entry': entry, 'size': lot, 'trap_type': trap,
                }
                notify(f'**{asset} {result.direction.value}**\nEntry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}\n'
                       f'Conf: {result.confidence:.2f} | {trap.value}')

            time.sleep(300)

        except KeyboardInterrupt:
            print("\nStopping...")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(10)

if __name__ == '__main__':
    run()