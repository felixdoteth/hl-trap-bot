from mt5linux import MetaTrader5

mt5 = MetaTrader5(host='localhost', port=18002)

if not mt5.initialize():
    print("initialize() failed:", mt5.last_error())
else:
    print("Connected OK")
    print(mt5.version())
    print(mt5.terminal_info())
