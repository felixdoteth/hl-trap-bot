from mt5linux import MetaTrader5

mt5 = MetaTrader5(host='localhost', port=18002)
mt5.initialize()

print(mt5.account_info())

print("\n--- Synthetic index symbols ---")
symbols = mt5.symbols_get()
for s in symbols:
    name = s.name
    if any(k in name for k in ['Volatility', 'Boom', 'Crash', 'Step', 'Jump', 'Range Break']):
        print(name)
