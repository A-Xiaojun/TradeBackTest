import argparse
import sys
from pathlib import Path

from app.core.db import get_connection, init_db


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_fetcher_class():
    repo_root = _repo_root()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from examples.get_okx_kline import OKXDataFetcher  # pylint: disable=import-error

    return OKXDataFetcher


def _upsert_strategy(db, name: str, exchange: str, symbol: str, timeframe: str) -> int:
    row = db.execute("SELECT id FROM strategies WHERE name = ?", (name,)).fetchone()
    if row is not None:
        db.execute(
            "UPDATE strategies SET exchange = ?, symbol = ?, timeframe = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (exchange, symbol, timeframe, row["id"]),
        )
        return int(row["id"])

    cursor = db.execute(
        "INSERT INTO strategies(name, exchange, symbol, timeframe, status) VALUES (?, ?, ?, ?, ?)",
        (name, exchange, symbol, timeframe, "running"),
    )
    return int(cursor.lastrowid)


def _import_equity_points(db, strategy_id: int, df, initial_equity: float) -> int:
    closes = df["close"].tolist()
    times = list(df.index)
    if len(closes) < 2:
        return 0

    equity = float(initial_equity)
    inserted = 0
    for i in range(len(closes)):
        ts = times[i].strftime("%Y-%m-%d %H:%M:%S")
        pnl = 0.0
        if i > 0:
            prev_close = float(closes[i - 1])
            curr_close = float(closes[i])
            ret = 0.0 if prev_close == 0 else (curr_close - prev_close) / prev_close
            prev_equity = equity
            equity = equity * (1 + ret)
            pnl = equity - prev_equity

        db.execute(
            "INSERT OR REPLACE INTO equity_snapshots(strategy_id, ts, equity, pnl, position) VALUES (?, ?, ?, ?, ?)",
            (strategy_id, ts, round(equity, 4), round(pnl, 4), 1.0),
        )
        inserted += 1

    return inserted


def _import_trade_points(db, strategy_id: int, df, step: int, qty: float, fee_rate: float) -> int:
    closes = df["close"].tolist()
    times = list(df.index)
    if len(closes) <= step:
        return 0

    inserted = 0
    trade_no = 1
    for close_idx in range(step, len(closes), step):
        open_idx = close_idx - step
        open_price = float(closes[open_idx])
        close_price = float(closes[close_idx])
        open_ts = times[open_idx].strftime("%Y-%m-%d %H:%M:%S")
        close_ts = times[close_idx].strftime("%Y-%m-%d %H:%M:%S")

        is_long = close_price >= open_price
        open_side = "buy" if is_long else "sell"
        close_side = "sell" if is_long else "buy"
        gross = (close_price - open_price) * qty if is_long else (open_price - close_price) * qty
        fee = (open_price + close_price) * qty * fee_rate
        pnl = gross - fee

        open_trade_id = "OKX-%05d-O" % trade_no
        close_trade_id = "OKX-%05d-C" % trade_no
        trade_no += 1

        db.execute(
            "INSERT INTO trades(strategy_id, trade_id, ts, side, action, price, qty, fee, pnl, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (strategy_id, open_trade_id, open_ts, open_side, "open", open_price, qty, round(fee / 2, 6), 0.0, "okx-kline"),
        )
        db.execute(
            "INSERT INTO trades(strategy_id, trade_id, ts, side, action, price, qty, fee, pnl, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                strategy_id,
                close_trade_id,
                close_ts,
                close_side,
                "close",
                close_price,
                qty,
                round(fee / 2, 6),
                round(pnl, 6),
                "okx-kline",
            ),
        )
        inserted += 2

    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Import OKX kline data to webapp sqlite.")
    parser.add_argument("--strategy-name", default="OKX Kline Strategy")
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--bar", default="1H")
    parser.add_argument("--days", type=int, default=15)
    parser.add_argument("--initial-equity", type=float, default=10000.0)
    parser.add_argument("--trade-step", type=int, default=12, help="Every N bars create one open-close trade pair.")
    parser.add_argument("--qty", type=float, default=0.01)
    parser.add_argument("--fee-rate", type=float, default=0.0008)
    parser.add_argument("--proxy", default="", help="e.g. http://127.0.0.1:7897")
    parser.add_argument("--reset", action="store_true", help="Delete old equity/trades for this strategy before import.")
    args = parser.parse_args()

    init_db()
    OKXDataFetcher = _load_fetcher_class()
    proxies = None
    if args.proxy:
        proxies = {"http": args.proxy, "https": args.proxy}
    fetcher = OKXDataFetcher(proxies=proxies)

    df = fetcher.get_historical_klines(instId=args.symbol, bar=args.bar, days=args.days)
    if df is None or len(df) == 0:
        raise RuntimeError("No kline data fetched from OKX.")

    with get_connection() as db:
        strategy_id = _upsert_strategy(
            db=db,
            name=args.strategy_name,
            exchange="okx",
            symbol=args.symbol,
            timeframe=args.bar,
        )

        if args.reset:
            db.execute("DELETE FROM equity_snapshots WHERE strategy_id = ?", (strategy_id,))
            db.execute("DELETE FROM trades WHERE strategy_id = ?", (strategy_id,))

        equity_count = _import_equity_points(
            db=db,
            strategy_id=strategy_id,
            df=df,
            initial_equity=args.initial_equity,
        )
        trade_count = _import_trade_points(
            db=db,
            strategy_id=strategy_id,
            df=df,
            step=max(args.trade_step, 1),
            qty=args.qty,
            fee_rate=max(args.fee_rate, 0.0),
        )
        db.commit()

    print("Import finished.")
    print("strategy_id=%s, symbol=%s, bar=%s, points=%s, trades=%s" % (strategy_id, args.symbol, args.bar, equity_count, trade_count))


if __name__ == "__main__":
    main()
