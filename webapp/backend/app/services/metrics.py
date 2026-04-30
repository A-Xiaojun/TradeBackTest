from __future__ import annotations

from math import sqrt


def calc_max_drawdown(equity_values: list[float]) -> float:
    if not equity_values:
        return 0.0
    peak = equity_values[0]
    max_dd = 0.0
    for value in equity_values:
        if value > peak:
            peak = value
        if peak <= 0:
            continue
        dd = (peak - value) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calc_returns(equity_values: list[float]) -> list[float]:
    if len(equity_values) < 2:
        return []
    returns: list[float] = []
    for i in range(1, len(equity_values)):
        prev = equity_values[i - 1]
        curr = equity_values[i]
        if prev == 0:
            continue
        returns.append((curr - prev) / prev)
    return returns


def calc_sharpe(returns: list[float], annual_factor: int = 252) -> float:
    if len(returns) < 2:
        return 0.0
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
    std = variance**0.5
    if std == 0:
        return 0.0
    return (mean_ret / std) * sqrt(annual_factor)
