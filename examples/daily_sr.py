import pandas as pd


def load_daily_ohlc(csv_path: str) -> pd.DataFrame:
    """读取日线OHLCV并规范索引为日期。"""
    df = pd.read_csv(csv_path, parse_dates=["datetime"])
    df = df.sort_values("datetime").set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]]


def build_daily_sr_table(df_daily: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """
    基于历史窗口构建日线支撑/压力：
    - pressure(压力位): 过去lookback天最高点（不含当天，避免未来函数）
    - support(支撑位): 过去lookback天最低点（不含当天）
    """
    out = pd.DataFrame(index=df_daily.index.copy())
    out["resistance"] = df_daily["high"].rolling(lookback, min_periods=3).max().shift(1)
    out["support"] = df_daily["low"].rolling(lookback, min_periods=3).min().shift(1)
    out = out.dropna(how="all")
    return out

