import time
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd


# ======== 运行配置 ========
CSV_PATH = "/Users/shirj/word/TradeBackTest/output/kline/ETHUSD_15M_20260413_115652.csv"
POLL_SECONDS = 2
MODE = "signal"  # signal / paper

# 策略参数（与 bt_run.py 核心逻辑保持一致）
PIVOT_PERIOD = 5
ATR_PERIOD = 14
ADX_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2.0
ADX_THRESHOLD = 18.0
BB_BANDWIDTH_THRESHOLD = 0.010
MIN_SWING_ATR_MULT = 0.6
MIN_EMA_SPREAD_PCT = 0.003
COOLDOWN_BARS = 2
EMA_FAST = 10
EMA_SLOW = 20
LEVERAGE = 10.0
RISK_PERCENT = 1.0
INITIAL_CASH = 750.0


def _load_csv(path: str) -> pd.DataFrame:
    """读取 CSV，兼容有/无表头两种格式。"""
    try:
        df = pd.read_csv(path, parse_dates=["datetime"])
    except Exception:
        df = pd.read_csv(
            path,
            header=None,
            names=["datetime", "open", "high", "low", "close", "volume"],
            parse_dates=["datetime"],
        )
    df = df[["datetime", "open", "high", "low", "close", "volume"]].copy()
    df = df.dropna(subset=["datetime"]).drop_duplicates(subset=["datetime"]).sort_values("datetime")
    return df.reset_index(drop=True)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    h, l = df["high"], df["low"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat(
        [
            (h - l).abs(),
            (h - df["close"].shift(1)).abs(),
            (l - df["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period, min_periods=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period, min_periods=period).mean() / atr
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).replace([np.inf, -np.inf], np.nan)
    return dx.rolling(period, min_periods=period).mean()


@dataclass
class Position:
    side: int = 0  # 1 long, -1 short, 0 flat
    size: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0


class CsvLiveTrader:
    def __init__(self) -> None:
        self.df = _load_csv(CSV_PATH)
        self.last_processed_idx = -1
        self.highs: List[float] = []
        self.lows: List[float] = []
        self.position = Position()
        self.cash = float(INITIAL_CASH)
        self.last_exit_idx = -10_000

    def _log(self, msg: str) -> None:
        print(msg, flush=True)

    def _calc_size(self, close: float) -> float:
        target_notional = self.cash * RISK_PERCENT * LEVERAGE
        return max(target_notional / max(close, 1e-9), 0.0)

    def _refresh_df(self) -> int:
        new_df = _load_csv(CSV_PATH)
        if len(new_df) > len(self.df):
            add = len(new_df) - len(self.df)
            self.df = new_df
            return add
        return 0

    def _update_indicators(self) -> None:
        self.df["ema_fast"] = self.df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        self.df["ema_slow"] = self.df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
        self.df["atr"] = _atr(self.df, ATR_PERIOD)
        self.df["adx"] = _adx(self.df, ADX_PERIOD)
        mid = self.df["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
        std = self.df["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).std(ddof=0)
        self.df["bb_top"] = mid + BB_DEV * std
        self.df["bb_bot"] = mid - BB_DEV * std

    def _detect_pivot(self, idx: int) -> None:
        p = PIVOT_PERIOD
        if idx < 2 * p:
            return
        window = self.df.iloc[idx - 2 * p : idx + 1]
        if len(window) != 2 * p + 1:
            return
        center = window.iloc[p]
        ch = float(center["high"])
        cl = float(center["low"])
        is_ph = all(ch > float(v) for j, v in enumerate(window["high"].values) if j != p)
        is_pl = all(cl < float(v) for j, v in enumerate(window["low"].values) if j != p)
        if is_ph:
            self.highs.append(ch)
            self.highs = self.highs[-5:]
        if is_pl:
            self.lows.append(cl)
            self.lows = self.lows[-5:]

    def _enter(self, side: int, price: float, stop: float, dt: pd.Timestamp) -> None:
        size = self._calc_size(price)
        if size <= 0:
            return
        self.position = Position(side=side, size=size, entry_price=price, stop_price=stop)
        name = "做多" if side > 0 else "做空"
        self._log(f"{dt} {name} 入场: price={price:.2f}, size={size:.4f}, stop={stop:.2f}")

    def _close(self, price: float, dt: pd.Timestamp, reason: str) -> None:
        if self.position.side == 0:
            return
        pnl = (price - self.position.entry_price) * self.position.size * self.position.side
        fee = (abs(self.position.entry_price * self.position.size) + abs(price * self.position.size)) * 0.0008
        pnl_net = pnl - fee
        if MODE == "paper":
            self.cash += pnl_net
        self._log(f"{dt} 平仓({reason}): price={price:.2f}, pnl_net={pnl_net:.2f}, cash={self.cash:.2f}")
        self.position = Position()

    def _on_bar(self, idx: int) -> None:
        row = self.df.iloc[idx]
        dt = pd.Timestamp(row["datetime"])
        close = float(row["close"])
        atr = float(row["atr"]) if not pd.isna(row["atr"]) else np.nan
        adx = float(row["adx"]) if not pd.isna(row["adx"]) else np.nan
        bb_top = float(row["bb_top"]) if not pd.isna(row["bb_top"]) else np.nan
        bb_bot = float(row["bb_bot"]) if not pd.isna(row["bb_bot"]) else np.nan
        ema_fast = float(row["ema_fast"])
        ema_slow = float(row["ema_slow"])

        if np.isnan(atr) or np.isnan(adx) or np.isnan(bb_top) or np.isnan(bb_bot):
            return

        bb_width = (bb_top - bb_bot) / max(abs(close), 1e-9)
        ema_spread = abs(ema_fast - ema_slow) / max(abs(close), 1e-9)
        if (adx < ADX_THRESHOLD) or (bb_width < BB_BANDWIDTH_THRESHOLD) or (ema_spread < MIN_EMA_SPREAD_PCT):
            return

        self._detect_pivot(idx)

        if self.position.side == 0 and (idx - self.last_exit_idx) <= COOLDOWN_BARS:
            return

        if self.position.side == 0:
            if len(self.lows) >= 2:
                swing_ok = (self.lows[-1] - self.lows[-2]) >= MIN_SWING_ATR_MULT * max(atr, 1e-9)
                if self.lows[-1] > self.lows[-2] and swing_ok:
                    if MODE == "signal":
                        self._log(f"{dt} 做多信号: close={close:.2f}, stop={self.lows[-1]:.2f}")
                    else:
                        self._enter(1, close, self.lows[-1], dt)
                    return
            if len(self.highs) >= 2:
                swing_ok = (self.highs[-2] - self.highs[-1]) >= MIN_SWING_ATR_MULT * max(atr, 1e-9)
                if self.highs[-1] < self.highs[-2] and swing_ok:
                    if MODE == "signal":
                        self._log(f"{dt} 做空信号: close={close:.2f}, stop={self.highs[-1]:.2f}")
                    else:
                        self._enter(-1, close, self.highs[-1], dt)
        else:
            if self.position.side > 0:
                if close <= self.position.stop_price:
                    self._close(close, dt, "多头止损")
                    self.last_exit_idx = idx
                elif len(self.highs) >= 2 and self.highs[-1] < self.highs[-2]:
                    self._close(close, dt, "多头动能衰竭")
                    self.last_exit_idx = idx
            else:
                if close >= self.position.stop_price:
                    self._close(close, dt, "空头止损")
                    self.last_exit_idx = idx
                elif len(self.lows) >= 2 and self.lows[-1] > self.lows[-2]:
                    self._close(close, dt, "空头动能衰竭")
                    self.last_exit_idx = idx

    def run(self) -> None:
        self._log(f"启动 CSV 流式交易: {CSV_PATH}")
        self._log(f"运行模式: {MODE}")
        while True:
            added = self._refresh_df()
            if added > 0:
                self._update_indicators()
                start = max(self.last_processed_idx + 1, 0)
                end = len(self.df) - 1
                for i in range(start, end + 1):
                    self._on_bar(i)
                    self.last_processed_idx = i
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    CsvLiveTrader().run()
