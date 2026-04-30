from datetime import datetime, timedelta

from app.core.db import get_connection, init_db


def main() -> None:
    init_db()
    with get_connection() as db:
        row = db.execute(
            "SELECT id FROM strategies WHERE name = ?",
            ("ETH 15m EMA",),
        ).fetchone()
        if row is None:
            cursor = db.execute(
                "INSERT INTO strategies(name, exchange, symbol, timeframe, status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("ETH 15m EMA", "okx", "ETH-USDT-SWAP", "15m", "running"),
            )
            strategy_id = cursor.lastrowid
        else:
            strategy_id = row["id"]

        base = datetime.utcnow() - timedelta(days=30)
        equity = 10000.0
        for i in range(180):
            ts = (base + timedelta(hours=4 * i)).strftime("%Y-%m-%d %H:%M:%S")
            pnl = ((i % 9) - 4) * 3.2
            equity += pnl
            db.execute(
                "INSERT OR REPLACE INTO equity_snapshots(strategy_id, ts, equity, pnl, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (strategy_id, ts, round(equity, 2), round(pnl, 2), 1.0),
            )

        for i in range(40):
            ts = (base + timedelta(hours=6 * i)).strftime("%Y-%m-%d %H:%M:%S")
            side = "buy" if i % 2 == 0 else "sell"
            action = "open" if i % 2 == 0 else "close"
            pnl = 0.0 if action == "open" else ((i % 7) - 3) * 8.5
            db.execute(
                "INSERT INTO trades(strategy_id, trade_id, ts, side, action, price, qty, fee, pnl, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    strategy_id,
                    f"T{i+1:04d}",
                    ts,
                    side,
                    action,
                    2800 + i * 3.5,
                    0.02,
                    0.4,
                    round(pnl, 2),
                    "seed-data",
                ),
            )

        db.commit()
    print("Example data imported.")


if __name__ == "__main__":
    main()
