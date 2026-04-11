#
import ccxt
import pandas as pd
import datetime
import time
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
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'output', 'kline')
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{base}_{tf}_{now.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}.csv")

def fetch_okx_kline_range(symbol, timeframe, start_iso=None, end_iso=None, limit_per_call=100, filename=None, max_candles=200000):
    exchange = ccxt.okx({'timeout': 30000})
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    end_ms = exchange.parse8601(end_iso) if end_iso else exchange.milliseconds()
    since_ms = exchange.parse8601(start_iso) if start_iso else end_ms - tf_ms * limit_per_call
    all_rows = []
    while True:
        if since_ms >= end_ms or len(all_rows) >= max_candles:
            break
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit_per_call)
        if not ohlcv:
            break
        all_rows.extend(ohlcv)
        last_ts = ohlcv[-1][0]
        since_ms = last_ts + tf_ms
        time.sleep(exchange.rateLimit / 1000.0)
    if not all_rows:
        return pd.DataFrame(columns=['datetime','open','high','low','close','volume'])
    df = pd.DataFrame(all_rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df.drop_duplicates(subset=['timestamp'], inplace=True)
    df.sort_values('timestamp', inplace=True)
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']]
    out = filename or _auto_filename(symbol, timeframe)
    df.to_csv(out, index=False)
    print(f"K线数据已保存到 {out}")
    return df

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
    # fetch_okx_kline_range(symbol='BTC/USDT', timeframe='5m', start_iso='2025-01-01T00:00:00Z')
    # fetch_okx_kline_range(symbol='BTC/USDT', timeframe='15m', start_iso='2025-01-01T00:00:00Z')
    fetch_okx_kline_range(symbol='BTC/USDT', timeframe='1h', start_iso='2025-01-01T00:00:00Z')
