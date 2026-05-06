#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib.pyplot as plt
import pandas as pd

from okx_paper_trader import (
    ADX_PERIOD,
    ADX_THRESHOLD,
    ATR_PERIOD,
    BB_BANDWIDTH_THRESHOLD,
    BB_DEV,
    BB_PERIOD,
    COOLDOWN_BARS,
    DEFAULT_BAR,
    DEFAULT_INST_ID,
    EMA_FAST,
    EMA_SLOW,
    INITIAL_CASH,
    LEVERAGE,
    MIN_EMA_SPREAD_PCT,
    MIN_SWING_ATR_MULT,
    ORDER_UTILIZATION,
    PIVOT_PERIOD,
    PROFIT_POOL_FLOOR,
    RISK_PERCENT,
    _adx,
    _atr,
    _floor_to_step,
    _format_log_dt,
    _indicator_warmup_bars,
    _normalize_inst_id,
    _normalize_start_time,
    _sanitize_filename,
)


DEFAULT_CONTRACT_VALUE = 0.1
DEFAULT_CONTRACT_STEP = 0.01
DEFAULT_MIN_CONTRACTS = 0.01
DEFAULT_FEE_RATE = 0.0005
DEFAULT_OUTPUT_SUBDIR = "output/replay"
REPLAY_AUTO_TOPUP_TO_INITIAL = True
REPLAY_AUTO_WITHDRAW_PROFIT = True
REPLAY_MAX_EXTERNAL_TOPUP = 2600.0


@dataclass
class Position:
    side: int = 0
    contracts: float = 0.0
    base_size: float = 0.0
    avg_price: float = 0.0
    pos_side: str = ""
    entry_dt: Optional[str] = None
    entry_fee: float = 0.0


@dataclass
class TradeRecord:
    entry_dt: str
    exit_dt: str
    side: str
    entry_price: float
    exit_price: float
    contracts: float
    base_size: float
    gross_pnl: float
    fees: float
    net_pnl: float
    reason: str


class OfflineReplayTrader:
    def __init__(
        self,
        *,
        csv_path: Path,
        inst_id: str,
        bar: str,
        start_time: Optional[pd.Timestamp],
        initial_equity: float,
        contract_value: float,
        contract_step: float,
        min_contracts: float,
        fee_rate: float,
        auto_topup_to_initial: bool,
        auto_withdraw_profit: bool,
        max_external_topup: float,
        profit_pool_floor: float,
        verbose_bars: bool,
        output_dir: Path,
    ) -> None:
        self.csv_path = csv_path
        self.inst_id = _normalize_inst_id(inst_id)
        self.bar = bar
        self.start_time = start_time
        self.initial_equity = float(initial_equity)
        self.contract_value = max(float(contract_value), 1e-9)
        self.contract_step = max(float(contract_step), 1e-9)
        self.min_contracts = max(float(min_contracts), self.contract_step)
        self.fee_rate = max(float(fee_rate), 0.0)
        self.auto_topup_to_initial = auto_topup_to_initial
        self.auto_withdraw_profit = auto_withdraw_profit
        self.max_external_topup = max_external_topup
        self.profit_pool_floor = profit_pool_floor
        self.verbose_bars = verbose_bars
        self.output_dir = output_dir

        self.source_df = self._load_csv(csv_path)
        self.market_df = self._update_indicators(self.source_df.copy())

        self.position = Position()
        self.stop_price: Optional[float] = None
        self.last_exit_dt: Optional[pd.Timestamp] = None
        self.last_processed_dt: Optional[pd.Timestamp] = None

        self.highs: List[float] = []
        self.lows: List[float] = []

        self.cash = float(initial_equity)
        self.cum_withdraw = 0.0
        self.cum_topup_external = 0.0
        self.profit_pool = 0.0
        self.funding_exhausted = False
        self.funding_exhausted_notified = False

        self.trade_records: List[TradeRecord] = []
        self.equity_points: List[Dict[str, Any]] = []
        self.latest_close = float(self.market_df.iloc[0]["close"]) if not self.market_df.empty else 0.0

    def _log(self, message: str) -> None:
        print(message, flush=True)

    def _load_csv(self, csv_path: Path) -> pd.DataFrame:
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
        df = pd.read_csv(csv_path)
        rename_map = {col: str(col).strip().lower() for col in df.columns}
        df = df.rename(columns=rename_map)
        required = {"datetime", "open", "high", "low", "close"}
        missing = sorted(required - set(df.columns))
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).reset_index(drop=True)
        if df.empty:
            raise ValueError(f"No valid candle rows in CSV: {csv_path}")
        return df

    def _update_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = out["close"].rolling(EMA_FAST, min_periods=EMA_FAST).mean()
        out["ema_slow"] = out["close"].rolling(EMA_SLOW, min_periods=EMA_SLOW).mean()
        out["atr"] = _atr(out, ATR_PERIOD)
        out["adx"] = _adx(out, ADX_PERIOD)
        mid = out["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
        std = out["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).std(ddof=0)
        out["bb_top"] = mid + BB_DEV * std
        out["bb_bot"] = mid - BB_DEV * std
        return out

    def _current_unrealized_pnl(self, close: float) -> float:
        if self.position.side == 0:
            return 0.0
        return (close - self.position.avg_price) * self.position.base_size * self.position.side

    def _current_equity(self, close: float) -> float:
        return self.cash + self._current_unrealized_pnl(close)

    def _current_available_eq(self, close: float) -> float:
        if self.position.side == 0:
            return max(self.cash, 0.0)
        margin_used = abs(self.position.base_size * close) / max(LEVERAGE, 1e-9)
        return max(self._current_equity(close) - margin_used, 0.0)

    def _calc_order_plan(self, close: float) -> Dict[str, float]:
        if close <= 0:
            return {"contracts": 0.0, "base_size": 0.0, "target_notional": 0.0, "max_notional_now": 0.0}
        target_notional = self.initial_equity * RISK_PERCENT * LEVERAGE
        max_notional_now = self._current_available_eq(close) * LEVERAGE * ORDER_UTILIZATION
        notional = min(target_notional, max_notional_now)
        if notional <= 0:
            return {"contracts": 0.0, "base_size": 0.0, "target_notional": target_notional, "max_notional_now": max_notional_now}
        contracts = notional / close / self.contract_value
        contracts = _floor_to_step(contracts, self.contract_step)
        if contracts + 1e-12 < self.min_contracts:
            contracts = 0.0
        return {
            "contracts": contracts,
            "base_size": contracts * self.contract_value,
            "target_notional": target_notional,
            "max_notional_now": max_notional_now,
        }

    def _apply_cashflow_policy(self, close: float) -> None:
        if self.position.side != 0:
            return
        current_value = self._current_equity(close)
        eps = 1e-9

        if self.auto_withdraw_profit and current_value > self.initial_equity + eps:
            amount = current_value - self.initial_equity
            self.cash -= amount
            self.cum_withdraw += amount
            self.profit_pool += amount
            self._log(
                f"{_format_log_dt(self.last_processed_dt)} cashflow: withdraw={amount:.2f}, "
                f"profit_pool={self.profit_pool:.2f}"
            )
            return

        if self.auto_topup_to_initial and current_value < self.initial_equity - eps:
            deficit = self.initial_equity - current_value
            pool_usable = max(self.profit_pool - self.profit_pool_floor, 0.0)
            from_pool = min(deficit, pool_usable)
            remain = deficit - from_pool
            external_left = max(self.max_external_topup - self.cum_topup_external, 0.0)
            external = min(remain, external_left) if remain > eps else 0.0
            topup = from_pool + external
            if topup <= eps:
                self.funding_exhausted = True
                if not self.funding_exhausted_notified:
                    self._log(
                        f"{_format_log_dt(self.last_processed_dt)} cashflow: exhausted, "
                        f"external_limit={self.max_external_topup:.2f}"
                    )
                    self.funding_exhausted_notified = True
                return
            self.cash += topup
            self.profit_pool -= from_pool
            self.cum_topup_external += external
            self._log(
                f"{_format_log_dt(self.last_processed_dt)} cashflow: topup={topup:.2f}, "
                f"from_pool={from_pool:.2f}, external={external:.2f}, "
                f"profit_pool={self.profit_pool:.2f}, external_total={self.cum_topup_external:.2f}"
            )
            if topup + eps < deficit:
                self.funding_exhausted = True
                if not self.funding_exhausted_notified:
                    self._log(
                        f"{_format_log_dt(self.last_processed_dt)} cashflow: external topup reached limit "
                        f"{self.max_external_topup:.2f}, remaining_gap={deficit - topup:.2f}"
                    )
                    self.funding_exhausted_notified = True

    def _can_continue_trading_when_funding_exhausted(self, close: float) -> bool:
        external_left = self.max_external_topup - self.cum_topup_external
        current_real_pnl = self._current_equity(close) + self.cum_withdraw - self.cum_topup_external - self.initial_equity
        return (external_left > 1e-9) or (current_real_pnl > 0.0)

    def _detect_pivot(self, df: pd.DataFrame, idx: int) -> None:
        p = PIVOT_PERIOD
        if idx < 2 * p:
            return
        window = df.iloc[idx - 2 * p : idx + 1]
        if len(window) != 2 * p + 1:
            return
        center = window.iloc[p]
        center_high = float(center["high"])
        center_low = float(center["low"])
        is_pivot_high = all(center_high > float(v) for j, v in enumerate(window["high"].values) if j != p)
        is_pivot_low = all(center_low < float(v) for j, v in enumerate(window["low"].values) if j != p)
        if is_pivot_high:
            self.highs.append(center_high)
            self.highs = self.highs[-5:]
        if is_pivot_low:
            self.lows.append(center_low)
            self.lows = self.lows[-5:]

    def _bars_since_exit(self, current_dt: pd.Timestamp) -> int:
        if self.last_exit_dt is None:
            return 10_000
        delta = current_dt - self.last_exit_dt
        bar_seconds = int((pd.Timedelta(self.bar).total_seconds()) if self.bar[-1].lower() in ("m", "h", "d") else 0)
        if bar_seconds <= 0:
            unit = self.bar[-1].lower()
            num = int(self.bar[:-1])
            if unit == "m":
                bar_seconds = num * 60
            elif unit == "h":
                bar_seconds = num * 3600
            elif unit == "d":
                bar_seconds = num * 86400
        return int(delta.total_seconds() // max(bar_seconds, 1))

    def _record_equity_point(self, dt: pd.Timestamp, close: float) -> None:
        equity = self._current_equity(close)
        real_equity = equity + self.cum_withdraw - self.cum_topup_external
        self.equity_points.append(
            {
                "datetime": dt.isoformat(),
                "close": close,
                "equity": equity,
                "real_equity": real_equity,
                "cash": self.cash,
                "unrealized_pnl": self._current_unrealized_pnl(close),
                "position_side": self.position.side,
                "contracts": self.position.contracts,
            }
        )

    def _place_entry(
        self,
        side: int,
        close: float,
        stop: float,
        dt: pd.Timestamp,
        signal_reason: str,
        atr: float,
        adx: float,
        bb_width: float,
        ema_spread: float,
    ) -> None:
        pos_side = "long" if side > 0 else "short"
        plan = self._calc_order_plan(close)
        contracts = plan["contracts"]
        if contracts <= 0:
            self._log(
                f"{_format_log_dt(dt)} skip entry: reason={signal_reason}, close={close:.2f}, "
                f"target_notional={plan['target_notional']:.2f}, max_notional={plan['max_notional_now']:.2f}"
            )
            return
        fee = close * plan["base_size"] * self.fee_rate
        self.cash -= fee
        self.position = Position(
            side=side,
            contracts=contracts,
            base_size=plan["base_size"],
            avg_price=close,
            pos_side=pos_side,
            entry_dt=dt.isoformat(),
            entry_fee=fee,
        )
        self.stop_price = stop
        self._log(
            f"{_format_log_dt(dt)} entry filled: side={pos_side}, reason={signal_reason}, close={close:.2f}, "
            f"stop={stop:.2f}, atr={atr:.4f}, adx={adx:.2f}, bb_width={bb_width:.4f}, "
            f"ema_spread={ema_spread:.4f}, contracts={contracts:.4f}, base_size={plan['base_size']:.6f}, fee={fee:.6f}"
        )

    def _close_position(self, dt: pd.Timestamp, reason: str, trigger_price: float) -> None:
        if self.position.side == 0:
            return
        gross_pnl = (trigger_price - self.position.avg_price) * self.position.base_size * self.position.side
        exit_fee = abs(trigger_price * self.position.base_size) * self.fee_rate
        net_pnl = gross_pnl - self.position.entry_fee - exit_fee
        self.cash += gross_pnl - exit_fee
        self.trade_records.append(
            TradeRecord(
                entry_dt=self.position.entry_dt or dt.isoformat(),
                exit_dt=dt.isoformat(),
                side=self.position.pos_side,
                entry_price=self.position.avg_price,
                exit_price=trigger_price,
                contracts=self.position.contracts,
                base_size=self.position.base_size,
                gross_pnl=gross_pnl,
                fees=self.position.entry_fee + exit_fee,
                net_pnl=net_pnl,
                reason=reason,
            )
        )
        self._log(
            f"{_format_log_dt(dt)} close filled: side={self.position.pos_side}, reason={reason}, "
            f"entry={self.position.avg_price:.2f}, exit={trigger_price:.2f}, contracts={self.position.contracts:.4f}, "
            f"gross_pnl={gross_pnl:.4f}, fees={self.position.entry_fee + exit_fee:.6f}, net_pnl={net_pnl:.4f}"
        )
        self.position = Position()
        self.last_exit_dt = dt
        self.stop_price = None

    def _on_bar(self, df: pd.DataFrame, idx: int) -> None:
        row = df.iloc[idx]
        dt = pd.Timestamp(row["datetime"])
        close = float(row["close"])
        self._detect_pivot(df, idx)

        atr = float(row["atr"]) if not pd.isna(row["atr"]) else float("nan")
        adx = float(row["adx"]) if not pd.isna(row["adx"]) else float("nan")
        bb_top = float(row["bb_top"]) if not pd.isna(row["bb_top"]) else float("nan")
        bb_bot = float(row["bb_bot"]) if not pd.isna(row["bb_bot"]) else float("nan")
        ema_fast = float(row["ema_fast"]) if not pd.isna(row["ema_fast"]) else float("nan")
        ema_slow = float(row["ema_slow"]) if not pd.isna(row["ema_slow"]) else float("nan")

        if any(pd.isna(v) for v in (atr, adx, bb_top, bb_bot, ema_fast, ema_slow)):
            return

        bb_width = (bb_top - bb_bot) / max(abs(close), 1e-9)
        ema_spread = abs(ema_fast - ema_slow) / max(abs(close), 1e-9)

        if self.position.side == 0 and self._bars_since_exit(dt) <= COOLDOWN_BARS:
            return
        if (adx < ADX_THRESHOLD) or (bb_width < BB_BANDWIDTH_THRESHOLD) or (ema_spread < MIN_EMA_SPREAD_PCT):
            return

        if self.position.side == 0:
            if len(self.lows) >= 2:
                swing_ok = (self.lows[-1] - self.lows[-2]) >= MIN_SWING_ATR_MULT * max(atr, 1e-9)
                if self.lows[-1] > self.lows[-2] and swing_ok:
                    self._place_entry(1, close, self.lows[-1], dt, "higher_low", atr, adx, bb_width, ema_spread)
                    return
            if len(self.highs) >= 2:
                swing_ok = (self.highs[-2] - self.highs[-1]) >= MIN_SWING_ATR_MULT * max(atr, 1e-9)
                if self.highs[-1] < self.highs[-2] and swing_ok:
                    self._place_entry(-1, close, self.highs[-1], dt, "lower_high", atr, adx, bb_width, ema_spread)
                    return
        elif self.position.side > 0:
            if self.stop_price is not None and close <= self.stop_price:
                self._close_position(dt, "long_stop", close)
            elif len(self.highs) >= 2 and self.highs[-1] < self.highs[-2]:
                self._close_position(dt, "long_momentum_exhausted", close)
        else:
            if self.stop_price is not None and close >= self.stop_price:
                self._close_position(dt, "short_stop", close)
            elif len(self.lows) >= 2 and self.lows[-1] > self.lows[-2]:
                self._close_position(dt, "short_momentum_exhausted", close)

    def _build_summary(self) -> Dict[str, Any]:
        equity_df = pd.DataFrame(self.equity_points)
        trades_df = pd.DataFrame([asdict(item) for item in self.trade_records])
        final_close = self.latest_close
        final_equity = self._current_equity(final_close)
        real_final_equity = final_equity + self.cum_withdraw - self.cum_topup_external
        equity_peak = equity_df["equity"].cummax() if not equity_df.empty else pd.Series(dtype=float)
        equity_drawdown_pct = ((equity_df["equity"] - equity_peak) / equity_peak).min() * 100.0 if not equity_df.empty else 0.0
        real_peak = equity_df["real_equity"].cummax() if not equity_df.empty else pd.Series(dtype=float)
        real_drawdown_pct = ((equity_df["real_equity"] - real_peak) / real_peak).min() * 100.0 if not equity_df.empty else 0.0
        closed_trades = len(self.trade_records)
        wins = sum(1 for trade in self.trade_records if trade.net_pnl > 0)
        gross_profit = float(trades_df.loc[trades_df["net_pnl"] > 0, "net_pnl"].sum()) if not trades_df.empty else 0.0
        gross_loss = float(trades_df.loc[trades_df["net_pnl"] < 0, "net_pnl"].sum()) if not trades_df.empty else 0.0
        profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else None
        avg_trade = float(trades_df["net_pnl"].mean()) if not trades_df.empty else 0.0
        best_trade = float(trades_df["net_pnl"].max()) if not trades_df.empty else 0.0
        worst_trade = float(trades_df["net_pnl"].min()) if not trades_df.empty else 0.0
        return {
            "csv_path": str(self.csv_path),
            "inst_id": self.inst_id,
            "bar": self.bar,
            "start_time": self.start_time.isoformat() if self.start_time is not None else None,
            "bars_total": len(self.market_df),
            "bars_replayed": len(self.equity_points),
            "initial_equity": self.initial_equity,
            "final_equity": final_equity,
            "net_pnl": final_equity - self.initial_equity,
            "return_pct": ((final_equity / self.initial_equity) - 1.0) * 100.0 if self.initial_equity > 0 else 0.0,
            "real_final_equity": real_final_equity,
            "real_net_pnl": real_final_equity - self.initial_equity,
            "real_return_pct": ((real_final_equity / self.initial_equity) - 1.0) * 100.0 if self.initial_equity > 0 else 0.0,
            "max_drawdown_pct": float(equity_drawdown_pct) if pd.notna(equity_drawdown_pct) else 0.0,
            "real_max_drawdown_pct": float(real_drawdown_pct) if pd.notna(real_drawdown_pct) else 0.0,
            "closed_trades": closed_trades,
            "win_rate_pct": (wins / closed_trades * 100.0) if closed_trades > 0 else 0.0,
            "profit_factor": profit_factor,
            "avg_trade_net_pnl": avg_trade,
            "best_trade_net_pnl": best_trade,
            "worst_trade_net_pnl": worst_trade,
            "open_position": asdict(self.position),
            "cum_withdraw": self.cum_withdraw,
            "cum_topup_external": self.cum_topup_external,
            "profit_pool": self.profit_pool,
            "funding_exhausted": self.funding_exhausted,
        }

    def _write_plot(self, equity_df: pd.DataFrame, trades_df: pd.DataFrame, plot_path: Path) -> None:
        if equity_df.empty:
            return
        chart_df = equity_df.copy()
        chart_df["datetime"] = pd.to_datetime(chart_df["datetime"])
        chart_df["peak_equity"] = chart_df["equity"].cummax()
        chart_df["equity_drawdown_pct"] = ((chart_df["equity"] - chart_df["peak_equity"]) / chart_df["peak_equity"]) * 100.0
        chart_df["peak_real_equity"] = chart_df["real_equity"].cummax()
        chart_df["real_drawdown_pct"] = (
            (chart_df["real_equity"] - chart_df["peak_real_equity"]) / chart_df["peak_real_equity"]
        ) * 100.0

        fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=False, gridspec_kw={"height_ratios": [2, 1, 1]})

        axes[0].plot(chart_df["datetime"], chart_df["equity"], label="Account Equity", color="#1f77b4", linewidth=1.2)
        axes[0].plot(chart_df["datetime"], chart_df["real_equity"], label="Real Equity", color="#ff7f0e", linewidth=1.2)
        axes[0].axhline(self.initial_equity, label="Initial Equity", color="#999999", linestyle="--", linewidth=1.0)
        axes[0].set_title("Offline Replay Summary")
        axes[0].set_ylabel("Equity")
        axes[0].legend(loc="upper left")
        axes[0].grid(alpha=0.2)

        axes[1].plot(
            chart_df["datetime"],
            chart_df["equity_drawdown_pct"],
            label="Account Drawdown %",
            color="#d62728",
            linewidth=1.2,
        )
        axes[1].plot(
            chart_df["datetime"],
            chart_df["real_drawdown_pct"],
            label="Real Drawdown %",
            color="#9467bd",
            linewidth=1.2,
        )
        axes[1].axhline(0, color="#444444", linewidth=1.0)
        axes[1].set_ylabel("Drawdown %")
        axes[1].legend(loc="lower left")
        axes[1].grid(alpha=0.2)

        if not trades_df.empty:
            trade_plot = trades_df.copy()
            trade_plot["exit_dt"] = pd.to_datetime(trade_plot["exit_dt"])
            colors = ["#2ca02c" if value > 0 else "#d62728" for value in trade_plot["net_pnl"]]
            axes[2].bar(trade_plot["exit_dt"], trade_plot["net_pnl"], color=colors, width=0.02)
        else:
            axes[2].text(0.5, 0.5, "No closed trades", ha="center", va="center", transform=axes[2].transAxes)
        axes[2].axhline(0, color="#444444", linewidth=1.0)
        axes[2].set_ylabel("Trade PnL")
        axes[2].set_xlabel("Time")
        axes[2].grid(alpha=0.2)

        fig.tight_layout()
        fig.savefig(plot_path, dpi=150, bbox_inches="tight")
        plt.close(fig)

    def _write_report(
        self,
        summary: Dict[str, Any],
        trades_df: pd.DataFrame,
        report_path: Path,
        plot_path: Path,
        summary_path: Path,
        trades_path: Path,
        equity_path: Path,
    ) -> None:
        recent_trades_lines: List[str] = []
        if trades_df.empty:
            recent_trades_lines.append("- No closed trades")
        else:
            recent = trades_df.tail(10)
            for _, row in recent.iterrows():
                recent_trades_lines.append(
                    "- "
                    f"{row['exit_dt']} | {row['side']} | entry={float(row['entry_price']):.2f} | "
                    f"exit={float(row['exit_price']):.2f} | net={float(row['net_pnl']):.4f} | reason={row['reason']}"
                )

        profit_factor = summary.get("profit_factor")
        profit_factor_text = f"{profit_factor:.3f}" if isinstance(profit_factor, (int, float)) else "N/A"

        report = "\n".join(
            [
                "# Offline Replay Report",
                "",
                "## Overview",
                f"- CSV: `{self.csv_path}`",
                f"- Instrument: `{summary['inst_id']}`",
                f"- Bar: `{summary['bar']}`",
                f"- Start time: `{summary['start_time']}`",
                f"- Bars replayed: `{summary['bars_replayed']}` / `{summary['bars_total']}`",
                "",
                "## Metrics",
                f"- Initial equity: `{summary['initial_equity']:.2f}`",
                f"- Final equity: `{summary['final_equity']:.2f}`",
                f"- Net PnL: `{summary['net_pnl']:.2f}`",
                f"- Return: `{summary['return_pct']:.2f}%`",
                f"- Real final equity: `{summary['real_final_equity']:.2f}`",
                f"- Real net PnL: `{summary['real_net_pnl']:.2f}`",
                f"- Real return: `{summary['real_return_pct']:.2f}%`",
                f"- Account max drawdown: `{summary['max_drawdown_pct']:.2f}%`",
                f"- Real max drawdown: `{summary['real_max_drawdown_pct']:.2f}%`",
                f"- Closed trades: `{summary['closed_trades']}`",
                f"- Win rate: `{summary['win_rate_pct']:.2f}%`",
                f"- Profit factor: `{profit_factor_text}`",
                f"- Avg trade net PnL: `{summary['avg_trade_net_pnl']:.4f}`",
                f"- Best trade net PnL: `{summary['best_trade_net_pnl']:.4f}`",
                f"- Worst trade net PnL: `{summary['worst_trade_net_pnl']:.4f}`",
                "",
                "## Outputs",
                f"- Summary JSON: `{summary_path}`",
                f"- Trades CSV: `{trades_path}`",
                f"- Equity CSV: `{equity_path}`",
                f"- Summary Plot: `{plot_path}`",
                "",
                "## Recent Trades",
                *recent_trades_lines,
            ]
        )
        report_path.write_text(report + "\n", encoding="utf-8")

    def _write_outputs(self, summary: Dict[str, Any]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stem = (
            f"offline_replay_{_sanitize_filename(self.inst_id)}_"
            f"{_sanitize_filename(self.bar)}_{self.csv_path.stem}"
        )
        summary_path = self.output_dir / f"{stem}_summary.json"
        trades_path = self.output_dir / f"{stem}_trades.csv"
        equity_path = self.output_dir / f"{stem}_equity.csv"
        plot_path = self.output_dir / f"{stem}_summary.png"
        report_path = self.output_dir / f"{stem}_report.md"
        trades_df = pd.DataFrame([asdict(item) for item in self.trade_records])
        equity_df = pd.DataFrame(self.equity_points)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        trades_df.to_csv(trades_path, index=False)
        equity_df.to_csv(equity_path, index=False)
        self._write_plot(equity_df, trades_df, plot_path)
        self._write_report(summary, trades_df, report_path, plot_path, summary_path, trades_path, equity_path)
        self._log(f"Summary saved: {summary_path}")
        self._log(f"Trades saved: {trades_path}")
        self._log(f"Equity saved: {equity_path}")
        self._log(f"Plot saved: {plot_path}")
        self._log(f"Report saved: {report_path}")

    def run(self) -> Dict[str, Any]:
        warmup_bars = min(_indicator_warmup_bars(), len(self.market_df))
        replay_started = False
        replay_bar_count = 0

        self._log(
            f"Offline replay start: csv={self.csv_path.name}, instId={self.inst_id}, bar={self.bar}, "
            f"start_time={_format_log_dt(self.start_time)}, warmup_bars={warmup_bars}, "
            f"contract_value={self.contract_value}, contract_step={self.contract_step}, min_contracts={self.min_contracts}"
        )

        for idx in range(len(self.market_df)):
            history_df = self.market_df.iloc[: idx + 1]
            row = history_df.iloc[-1]
            dt = pd.Timestamp(row["datetime"])
            close = float(row["close"])
            self.latest_close = close

            should_start = idx >= warmup_bars and (
                self.start_time is None or dt >= self.start_time
            )
            if not replay_started and should_start:
                replay_started = True
                self._log(
                    f"Replay started: dt={_format_log_dt(dt)}, configured_start_time={_format_log_dt(self.start_time)}"
                )

            if replay_started:
                if self.verbose_bars:
                    self._log(
                        f"Replay bar: dt={_format_log_dt(dt)}, close={close:.2f}, "
                        f"position_side={self.position.side}, stop={self.stop_price}"
                    )
                self._on_bar(history_df, idx)
                self.last_processed_dt = dt
                if self.position.side == 0:
                    self._apply_cashflow_policy(close)
                    if self.funding_exhausted and not self._can_continue_trading_when_funding_exhausted(close):
                        self._log(
                            f"{_format_log_dt(dt)} funding exhausted and continuation conditions not met; "
                            "skipping new entries."
                        )
                self._record_equity_point(dt, close)
                replay_bar_count += 1
            else:
                self._detect_pivot(history_df, idx)
                self.last_processed_dt = dt

        summary = self._build_summary()
        self._write_outputs(summary)
        self._log(
            f"Offline replay done: replay_bars={replay_bar_count}, closed_trades={summary['closed_trades']}, "
            f"final_equity={summary['final_equity']:.2f}, return_pct={summary['return_pct']:.2f}, "
            f"max_drawdown_pct={summary['max_drawdown_pct']:.2f}"
        )
        return summary


def _default_csv_path(project_root: Path) -> Optional[Path]:
    kline_dir = project_root / "output" / "kline"
    if not kline_dir.exists():
        return None
    candidates = sorted(kline_dir.glob("*.csv"), key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _load_local_config(base_dir: Path) -> Dict[str, Any]:
    local_cfg = base_dir / "config.local"
    cfg_path = local_cfg if local_cfg.exists() else base_dir / "config"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline replay trader using local CSV candles.")
    parser.add_argument("--csv", dest="csv_path", help="Path to the local CSV file.")
    parser.add_argument("--symbol", default=None, help="Instrument id, e.g. ETH-USDT-SWAP.")
    parser.add_argument("--bar", default=None, help="Bar timeframe, e.g. 15m.")
    parser.add_argument("--start-time", default=None, help="Replay start time after warmup.")
    parser.add_argument("--initial-equity", type=float, default=INITIAL_CASH, help="Initial trading equity.")
    parser.add_argument("--contract-value", type=float, default=DEFAULT_CONTRACT_VALUE, help="Base size per contract.")
    parser.add_argument("--contract-step", type=float, default=DEFAULT_CONTRACT_STEP, help="Contract step size.")
    parser.add_argument("--min-contracts", type=float, default=DEFAULT_MIN_CONTRACTS, help="Minimum order size.")
    parser.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE, help="Taker fee rate used in replay.")
    parser.add_argument(
        "--auto-topup",
        action=argparse.BooleanOptionalAction,
        default=REPLAY_AUTO_TOPUP_TO_INITIAL,
        help="Enable topup policy.",
    )
    parser.add_argument(
        "--auto-withdraw",
        action=argparse.BooleanOptionalAction,
        default=REPLAY_AUTO_WITHDRAW_PROFIT,
        help="Enable profit withdrawal.",
    )
    parser.add_argument(
        "--max-external-topup",
        type=float,
        default=REPLAY_MAX_EXTERNAL_TOPUP,
        help="External topup cap.",
    )
    parser.add_argument("--profit-pool-floor", type=float, default=PROFIT_POOL_FLOOR, help="Reserved profit pool floor.")
    parser.add_argument("--quiet-bars", action="store_true", help="Hide per-bar replay logs.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_SUBDIR, help="Directory for summary/trade outputs.")
    return parser


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    base_dir = Path(__file__).resolve().parent
    cfg = _load_local_config(base_dir)
    parser = build_arg_parser()
    args = parser.parse_args()

    csv_arg = args.csv_path
    if csv_arg:
        csv_path = Path(csv_arg).expanduser()
        if not csv_path.is_absolute():
            csv_path = (project_root / csv_path).resolve()
    else:
        default_csv = _default_csv_path(project_root)
        if default_csv is None:
            raise ValueError("No CSV provided and no default CSV found under output/kline")
        csv_path = default_csv

    inst_id = args.symbol or cfg.get("symbol") or DEFAULT_INST_ID
    bar = args.bar or str(cfg.get("bar") or DEFAULT_BAR)
    start_time = _normalize_start_time(str(args.start_time or cfg.get("start_time") or ""))
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = (project_root / output_dir).resolve()

    trader = OfflineReplayTrader(
        csv_path=csv_path,
        inst_id=inst_id,
        bar=bar,
        start_time=start_time,
        initial_equity=args.initial_equity,
        contract_value=args.contract_value,
        contract_step=args.contract_step,
        min_contracts=args.min_contracts,
        fee_rate=args.fee_rate,
        auto_topup_to_initial=args.auto_topup,
        auto_withdraw_profit=args.auto_withdraw,
        max_external_topup=args.max_external_topup,
        profit_pool_floor=args.profit_pool_floor,
        verbose_bars=not args.quiet_bars,
        output_dir=output_dir,
    )
    trader.run()


if __name__ == "__main__":
    main()
