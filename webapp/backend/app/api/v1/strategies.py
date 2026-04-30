from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.db import get_db
from app.services.metrics import calc_max_drawdown, calc_returns, calc_sharpe

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyCreate(BaseModel):
    name: str
    exchange: str
    symbol: str
    timeframe: str
    status: str = "running"


@router.get("")
def list_strategies(db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        "SELECT id, name, exchange, symbol, timeframe, status, created_at, updated_at "
        "FROM strategies ORDER BY id DESC"
    ).fetchall()
    return [dict(row) for row in rows]


@router.post("")
def create_strategy(payload: StrategyCreate, db: sqlite3.Connection = Depends(get_db)) -> dict:
    cursor = db.execute(
        "INSERT INTO strategies(name, exchange, symbol, timeframe, status) VALUES (?, ?, ?, ?, ?)",
        (payload.name, payload.exchange, payload.symbol, payload.timeframe, payload.status),
    )
    db.commit()
    strategy_id = cursor.lastrowid
    row = db.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return dict(row)


@router.get("/{strategy_id}")
def get_strategy(strategy_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    row = db.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return dict(row)


@router.get("/{strategy_id}/metrics")
def get_strategy_metrics(strategy_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    strategy = db.execute("SELECT id FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    rows = db.execute(
        "SELECT ts, equity, pnl FROM equity_snapshots WHERE strategy_id = ? ORDER BY ts ASC",
        (strategy_id,),
    ).fetchall()
    if not rows:
        return {
            "strategy_id": strategy_id,
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "win_rate": 0.0,
            "profit_loss_ratio": 0.0,
            "trade_count": 0,
        }

    equity_values = [float(r["equity"]) for r in rows]
    first_equity = equity_values[0]
    last_equity = equity_values[-1]
    total_return = 0.0 if first_equity == 0 else (last_equity - first_equity) / first_equity

    returns = calc_returns(equity_values)
    max_drawdown = calc_max_drawdown(equity_values)
    sharpe = calc_sharpe(returns)
    annual_return = 0.0
    if returns:
        avg_daily = sum(returns) / len(returns)
        annual_return = avg_daily * 252

    trade_stats = db.execute(
        "SELECT COUNT(1) AS c, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS win "
        "FROM trades WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()
    pnl_stats = db.execute(
        "SELECT AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win, "
        "AVG(CASE WHEN pnl < 0 THEN ABS(pnl) END) AS avg_loss "
        "FROM trades WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()
    trade_count = int(trade_stats["c"] or 0)
    win_count = int(trade_stats["win"] or 0)
    win_rate = 0.0 if trade_count == 0 else win_count / trade_count
    avg_win = float(pnl_stats["avg_win"] or 0.0)
    avg_loss = float(pnl_stats["avg_loss"] or 0.0)
    profit_loss_ratio = 0.0 if avg_loss == 0 else avg_win / avg_loss

    return {
        "strategy_id": strategy_id,
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 6),
        "win_rate": round(win_rate, 6),
        "profit_loss_ratio": round(profit_loss_ratio, 6),
        "trade_count": trade_count,
    }
