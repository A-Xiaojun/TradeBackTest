import ccxt
import pandas as pd

def fetch_okx_kline(symbol, timeframe, limit, filename):
    exchange = ccxt.okx({
        'timeout': 30000,
        'proxies': {
            'http': 'http://127.0.0.1:7897',
            'https': 'http://127.0.0.1:7897',
        }
    })
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    # 将datetime列放到第一列，并去掉原始timestamp列
    df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']]
    df.to_csv(filename, index=False)
    print(f"K线数据已保存到 {filename}")
    return df

if __name__ == "__main__":
    fetch_okx_kline(symbol='BTC/USDT', timeframe='1d', limit=300, filename='btc_kline.csv')
