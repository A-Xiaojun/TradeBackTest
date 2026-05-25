#
import ccxt
import pandas as pd
import datetime
import time
import os
import json
from pathlib import Path

DEFAULT_EXCHANGES = ["okx", "binance"]


def _load_runtime_config():
    base_dir = Path(__file__).resolve().parent
    candidates = [base_dir / "config.local", base_dir / "config"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh), str(path)
        except Exception as exc:
            print(f"读取配置失败: {path} -> {exc}")
    return {}, None


def _resolve_proxy():
    cfg, cfg_path = _load_runtime_config()
    proxy = (
        cfg.get("proxy")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or os.getenv("ALL_PROXY")
    )
    return proxy, cfg_path

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

def _create_exchange(exchange_name: str, timeout_ms: int = 30000):
    if not hasattr(ccxt, exchange_name):
        raise ValueError(f"不支持的交易所: {exchange_name}")
    klass = getattr(ccxt, exchange_name)
    proxy, cfg_path = _resolve_proxy()
    exchange = klass({'timeout': timeout_ms, 'enableRateLimit': True})
    if proxy:
        exchange.httpsProxy = proxy
        print(f"[{exchange_name}] 使用代理: {proxy} (来源: {cfg_path or 'environment'})")
    else:
        print(f"[{exchange_name}] 未启用代理")
    return exchange


def fetch_kline_range(
    symbol,
    timeframe,
    start_iso=None,
    end_iso=None,
    limit_per_call=100,
    filename=None,
    max_candles=200000,
    exchanges=None,
    retry_per_exchange=2,
):
    """
    抓取区间K线，支持备用交易所自动切换。
    - exchanges: 例如 ["okx", "binance"]
    - retry_per_exchange: 单交易所失败重试次数
    """
    exchange_names = exchanges or DEFAULT_EXCHANGES
    last_err = None

    for ex_name in exchange_names:
        for attempt in range(1, retry_per_exchange + 1):
            try:
                exchange = _create_exchange(ex_name)
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
                    raise RuntimeError(f"{ex_name} 未返回K线数据")
                df = pd.DataFrame(all_rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df.drop_duplicates(subset=['timestamp'], inplace=True)
                df.sort_values('timestamp', inplace=True)
                df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
                df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']]
                out = filename or _auto_filename(symbol, timeframe)
                df.to_csv(out, index=False)
                print(f"使用交易所: {ex_name}")
                print(f"K线数据已保存到 {out}")
                return df
            except Exception as e:
                last_err = e
                print(f"[{ex_name}] 第{attempt}/{retry_per_exchange}次失败: {e}")
                time.sleep(min(2 * attempt, 6))
                continue

        print(f"[{ex_name}] 失败，切换到下一个备用交易所...")

    raise RuntimeError(f"所有交易所均失败，最后错误: {last_err}")


def fetch_okx_kline_range(symbol, timeframe, start_iso=None, end_iso=None, limit_per_call=100, filename=None, max_candles=200000):
    # 兼容旧调用：优先okx，失败后自动切binance
    return fetch_kline_range(
        symbol=symbol,
        timeframe=timeframe,
        start_iso=start_iso,
        end_iso=end_iso,
        limit_per_call=limit_per_call,
        filename=filename,
        max_candles=max_candles,
        exchanges=["okx", "binance"],
    )

def fetch_okx_kline(symbol, timeframe, limit, filename=None):
    exchange = _create_exchange("okx")
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
    fetch_kline_range(symbol='ETH/USDT', timeframe='15m', start_iso='2025-01-01T00:00:00Z', exchanges=["okx", "binance"])
