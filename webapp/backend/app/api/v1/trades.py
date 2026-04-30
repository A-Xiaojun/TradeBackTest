from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.db import get_db

router = APIRouter(prefix="/strategies/{strategy_id}/trades", tags=["trades"])


@router.get("")
def list_trades(
    strategy_id: int,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    strategy = db.execute("SELECT id FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    offset = (page - 1) * size
    rows = db.execute(
        "SELECT id, trade_id, ts, side, action, price, qty, fee, pnl, note "
        "FROM trades WHERE strategy_id = ? ORDER BY ts DESC LIMIT ? OFFSET ?",
        (strategy_id, size, offset),
    ).fetchall()
    total = db.execute("SELECT COUNT(1) AS c FROM trades WHERE strategy_id = ?", (strategy_id,)).fetchone()
    return {"items": [dict(row) for row in rows], "total": int(total["c"])}
