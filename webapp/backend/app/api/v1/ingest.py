from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.db import get_db

router = APIRouter(prefix="/ingest", tags=["ingest"])


class EquityPoint(BaseModel):
    ts: str
    equity: float
    pnl: float = 0
    position: float = 0


class EquityIngestRequest(BaseModel):
    strategy_id: int
    points: list[EquityPoint]


class TradePoint(BaseModel):
    trade_id: Optional[str] = None
    ts: str
    side: str
    action: str
    price: float
    qty: float
    fee: float = 0
    pnl: float = 0
    note: Optional[str] = None


class TradeIngestRequest(BaseModel):
    strategy_id: int
    trades: list[TradePoint]


def _assert_strategy_exists(db: sqlite3.Connection, strategy_id: int) -> None:
    row = db.execute("SELECT id FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")


@router.post("/equity")
def ingest_equity(payload: EquityIngestRequest, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _assert_strategy_exists(db, payload.strategy_id)
    for point in payload.points:
        db.execute(
            "INSERT OR REPLACE INTO equity_snapshots(strategy_id, ts, equity, pnl, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (payload.strategy_id, point.ts, point.equity, point.pnl, point.position),
        )
    db.commit()
    return {"ok": True, "count": len(payload.points)}


@router.post("/trades")
def ingest_trades(payload: TradeIngestRequest, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _assert_strategy_exists(db, payload.strategy_id)
    for trade in payload.trades:
        db.execute(
            "INSERT INTO trades(strategy_id, trade_id, ts, side, action, price, qty, fee, pnl, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.strategy_id,
                trade.trade_id,
                trade.ts,
                trade.side,
                trade.action,
                trade.price,
                trade.qty,
                trade.fee,
                trade.pnl,
                trade.note,
            ),
        )
    db.commit()
    return {"ok": True, "count": len(payload.trades)}
