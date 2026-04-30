from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.db import get_db

router = APIRouter(prefix="/strategies/{strategy_id}/equity", tags=["equity"])


@router.get("")
def list_equity(
    strategy_id: int,
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10000),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    strategy = db.execute("SELECT id FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    sql = "SELECT ts, equity, pnl, position FROM equity_snapshots WHERE strategy_id = ?"
    params: list[object] = [strategy_id]
    if start:
        sql += " AND ts >= ?"
        params.append(start)
    if end:
        sql += " AND ts <= ?"
        params.append(end)
    sql += " ORDER BY ts ASC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


@router.get("/drawdown")
def equity_drawdown(strategy_id: int, db: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        "SELECT ts, equity FROM equity_snapshots WHERE strategy_id = ? ORDER BY ts ASC",
        (strategy_id,),
    ).fetchall()
    if not rows:
        return []

    peak = float(rows[0]["equity"])
    output: list[dict] = []
    for row in rows:
        equity = float(row["equity"])
        if equity > peak:
            peak = equity
        drawdown = 0.0 if peak == 0 else (peak - equity) / peak
        output.append({"ts": row["ts"], "drawdown": round(drawdown, 6)})
    return output
