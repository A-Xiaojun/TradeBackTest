#
import ccxt
import pandas as pd
import datetime
import os

def _format_symbol_for_filename(symbol: str) -> str:
    name = symbol.replace('/', '-').upper()
    if name.endswith('-USDT'):
        name = name[:-5] + 'USD'
    return name

def _auto_filename(symbol: str, timeframe: str) -> str:
    base = _format_symbol_for_filename(symbol)
    tf = timeframe.upper()
    now = datetime.datetime.now()
    # 保存到 output/kline/ 目录，避免提交到仓库
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'output', 'kline')
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{base}_{tf}_{now.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}.csv")

def fetch_okx_kline(symbol, timeframe, limit, filename=None):
    exchange = ccxt.okx({'timeout': 30000})
    # 得到最新数据
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    # 将datetime列放到第一列，并去掉原始timestamp列
    df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']]
    out = filename or _auto_filename(symbol, timeframe)
    df.to_csv(out, index=False)
    print(f"K线数据已保存到 {out}")
    return df

if __name__ == "__main__":
    fetch_okx_kline(symbol='BTC/USDT', timeframe='1d', limit=400)
