PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS strategies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  equity REAL NOT NULL,
  pnl REAL NOT NULL DEFAULT 0,
  position REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
  UNIQUE(strategy_id, ts)
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL,
  trade_id TEXT,
  ts TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
  action TEXT NOT NULL CHECK(action IN ('open', 'close', 'reduce', 'reverse')),
  price REAL NOT NULL,
  qty REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0,
  pnl REAL NOT NULL DEFAULT 0,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS backtests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  params_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS backtest_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backtest_id INTEGER NOT NULL,
  total_return REAL NOT NULL,
  annual_return REAL,
  max_drawdown REAL NOT NULL,
  sharpe REAL,
  sortino REAL,
  calmar REAL,
  win_rate REAL,
  trade_count INTEGER,
  profit_factor REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (backtest_id) REFERENCES backtests(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS job_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name TEXT NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('info', 'warning', 'error')),
  message TEXT NOT NULL,
  payload_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_equity_strategy_ts ON equity_snapshots(strategy_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_ts ON trades(strategy_id, ts);
CREATE INDEX IF NOT EXISTS idx_backtests_strategy ON backtests(strategy_id);
CREATE INDEX IF NOT EXISTS idx_job_logs_job_time ON job_logs(job_name, created_at);
