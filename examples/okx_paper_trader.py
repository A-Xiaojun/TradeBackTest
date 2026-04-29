import asyncio
import json
import os
import queue
import ssl
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import timezone, timedelta

import certifi
import ccxt
import numpy as np
import pandas as pd
import websockets


# ======== OKX runtime config ========
DEFAULT_INST_ID = "ETH-USDT-SWAP"
DEFAULT_BAR = "15m"
POLL_SECONDS = 10
HISTORY_LIMIT = 200
HISTORY_PAGE_LIMIT = 100
MAX_HISTORY_BACKFILL_PAGES = 300
REST_TIMEOUT_MS = 20000
TD_MODE = "isolated"
POS_MODE = "long_short_mode"
SIMULATED_FLAG = "1"  # "1" for paper trading, "0" for live
CONFIG_BASENAME = "config"
FUNDING_ACCOUNT = "6"
TRADING_ACCOUNT = "18"
SETTLE_CCY = "USDT"

# ======== Strategy params (kept aligned with bt_run.py) ========
INITIAL_CASH = 750.0
PIVOT_PERIOD = 5
RISK_PERCENT = 1.00
LEVERAGE = 10.0
ORDER_UTILIZATION = 1.00
AUTO_TOPUP_TO_INITIAL = False
AUTO_WITHDRAW_PROFIT = False
MAX_EXTERNAL_TOPUP = 750.0
PROFIT_POOL_FLOOR = 50.0
MIN_CASHFLOW_TRANSFER = 5.0
STARTUP_RETRY_ATTEMPTS = 5
STARTUP_RETRY_BASE_DELAY = 2.0
MAX_NETWORK_BACKOFF_SECONDS = 60
POSITION_SYNC_IDLE_CYCLES = 6
POSITION_SYNC_ACTIVE_CYCLES = 3
WS_QUEUE_TIMEOUT_SECONDS = 5
WS_RECONNECT_DELAY_SECONDS = 5
WS_SUBSCRIBE_TIMEOUT_SECONDS = 8
WS_HEARTBEAT_SECONDS = 20
CLOSED_BAR_POLL_INTERVAL_SECONDS = 8
WARMUP_CACHE_TAIL_BARS = 120
WARMUP_CACHE_VALIDATION_BARS = 20
WARMUP_CACHE_MAX_GAP_BARS = 32
ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_THRESHOLD = 18.0
BB_PERIOD = 20
BB_DEV = 2.0
BB_BANDWIDTH_THRESHOLD = 0.010
MIN_SWING_ATR_MULT = 0.6
MIN_EMA_SPREAD_PCT = 0.003
COOLDOWN_BARS = 2
EMA_FAST = 10
EMA_SLOW = 20
LOG_TZ = timezone(timedelta(hours=8))
LOG_TZ_NAME = "UTC+8"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _bar_seconds(bar: str) -> int:
    unit = bar[-1].lower()
    num = int(bar[:-1])
    if unit == "m":
        return num * 60
    if unit == "h":
        return num * 3600
    if unit == "d":
        return num * 86400
    raise ValueError(f"Unsupported bar: {bar}")


def _normalize_inst_id(value: str) -> str:
    symbol = (value or "").strip().upper().replace("/", "-")
    if not symbol:
        return DEFAULT_INST_ID
    if symbol.endswith("-SWAP") or symbol.endswith("-FUTURES"):
        return symbol
    if symbol.count("-") >= 1:
        return f"{symbol}-SWAP"
    return symbol


def _sanitize_filename(value: str) -> str:
    out = []
    for ch in value:
        out.append(ch if ch.isalnum() else "_")
    return "".join(out).strip("_") or "default"


def _inst_id_to_ccxt_symbol(inst_id: str) -> str:
    parts = inst_id.split("-")
    if len(parts) >= 3 and parts[-1] == "SWAP":
        base = parts[0]
        quote = parts[1]
        return f"{base}/{quote}:{quote}"
    raise ValueError(f"Unsupported instId for ccxt swap symbol conversion: {inst_id}")


def _normalize_start_time(value: str) -> Optional[pd.Timestamp]:
    if not str(value).strip():
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        return ts.tz_convert("UTC").tz_localize(None)
    return ts


def _format_log_dt(value: Optional[pd.Timestamp]) -> str:
    if value is None:
        return "None"
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return f"{ts.tz_convert(LOG_TZ).strftime('%Y-%m-%d %H:%M:%S')} {LOG_TZ_NAME}"


def _daily_pnl_key(value: pd.Timestamp) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.tz_convert(LOG_TZ).strftime("%Y-%m-%d")


def _is_retryable_network_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("timed out", "timeout", "handshake", "read operation", "_ssl"))


def _indicator_warmup_bars() -> int:
    # 预留足够的历史K线，让滚动指标和 pivot 识别先稳定下来，
    # 再开始进入真实的策略判断。
    return max(EMA_SLOW, BB_PERIOD, ATR_PERIOD + ADX_PERIOD, PIVOT_PERIOD * 2 + 1, COOLDOWN_BARS + 1) + 5


def _bar_to_ws_channel(bar: str) -> str:
    unit = bar[-1].lower()
    num = bar[:-1]
    if unit == "m":
        return f"candle{num}m"
    if unit == "h":
        return f"candle{num}H"
    if unit == "d":
        return f"candle{num}D"
    raise ValueError(f"Unsupported websocket bar: {bar}")


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    quant = Decimal(str(step))
    floored = (Decimal(str(value)) / quant).to_integral_value(rounding=ROUND_DOWN) * quant
    return float(floored)


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
    side: int = 0
    contracts: float = 0.0
    base_size: float = 0.0
    avg_price: float = 0.0
    pos_side: str = ""


class OkxCandleStream:
    def __init__(self, inst_id: str, bar: str, flag: str, proxy: Optional[str], logger) -> None:
        self.inst_id = inst_id
        self.bar = bar
        self.flag = str(flag)
        self.proxy = proxy
        self.logger = logger
        self.channel = _bar_to_ws_channel(bar)
        # OKX v5 candlestick subscriptions are served from the business websocket
        # endpoint; using the public endpoint returns 60018 for candle channels.
        self.url = "wss://ws.okx.com:8443/ws/v5/business"
        self.queue: "queue.Queue[Dict[str, float]]" = queue.Queue(maxsize=1024)
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.pending_candle: Optional[Dict[str, float]] = None
        self.last_live_log_dt: Optional[pd.Timestamp] = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_thread, name="okx-candle-stream", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def get_next_candle(self, timeout: float) -> Optional[Dict[str, float]]:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run_thread(self) -> None:
        asyncio.run(self._run_forever())

    async def _run_forever(self) -> None:
        ssl_context = ssl.create_default_context()
        ssl_context.load_verify_locations(certifi.where())
        subscribe_payload = json.dumps(
            {"op": "subscribe", "args": [{"channel": self.channel, "instId": self.inst_id}]}
        )
        proxy_candidates = [self.proxy]
        if self.proxy:
            # 如果本地代理不稳定，则自动回退到直连再试一次。
            proxy_candidates.append(None)
        attempt_idx = 0
        while not self.stop_event.is_set():
            proxy_value = proxy_candidates[attempt_idx % len(proxy_candidates)]
            attempt_idx += 1
            try:
                async with websockets.connect(
                    self.url,
                    proxy=proxy_value,
                    ssl=ssl_context,
                    open_timeout=20,
                    ping_interval=None,
                    ping_timeout=None,
                    max_queue=64,
                ) as ws:
                    proxy_label = proxy_value or "direct"
                    self.logger(
                        f"WebSocket connected: channel={self.channel}, instId={self.inst_id}, route={proxy_label}"
                    )
                    await ws.send(subscribe_payload)
                    subscribed = await self._wait_for_subscription(ws)
                    if not subscribed:
                        raise RuntimeError("WebSocket subscribe ack timeout")
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for message in ws:
                            if self.stop_event.is_set():
                                break
                            self._handle_message(message)
                    finally:
                        heartbeat_task.cancel()
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                proxy_label = proxy_value or "direct"
                self.logger(
                    f"WebSocket error: route={proxy_label}, err={exc}; reconnecting in {WS_RECONNECT_DELAY_SECONDS}s"
                )
                await asyncio.sleep(WS_RECONNECT_DELAY_SECONDS)

    async def _wait_for_subscription(self, ws) -> bool:
        deadline = time.time() + WS_SUBSCRIBE_TIMEOUT_SECONDS
        while not self.stop_event.is_set() and time.time() < deadline:
            timeout = max(deadline - time.time(), 0.1)
            message = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if self._handle_message(message):
                return True
        return False

    async def _heartbeat(self, ws) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(WS_HEARTBEAT_SECONDS)
            pong_waiter = await ws.ping()
            await asyncio.wait_for(pong_waiter, timeout=WS_HEARTBEAT_SECONDS)

    def _handle_message(self, message: str) -> bool:
        payload = json.loads(message)
        if payload.get("event") == "subscribe":
            self.logger(f"WebSocket subscribed: channel={self.channel}, instId={self.inst_id}")
            return True
        if payload.get("event") == "error":
            self.logger(f"WebSocket subscription error: {payload}")
            return False
        if payload.get("event") == "notice":
            self.logger(f"WebSocket notice: {payload}")
            return False
        rows = payload.get("data") or []
        for row in rows:
            candle = OkxPaperTrader.parse_ws_candle_row(row)
            if candle is None:
                continue
            confirmed = str(row[8]) == "1"
            self._maybe_log_live_candle(candle, confirmed)
            self._handle_ws_candle(candle, confirmed)
        return False

    def _handle_ws_candle(self, candle: Dict[str, float], confirmed: bool) -> None:
        # Some OKX sessions only push the rolling bar with confirm=0. To keep
        # New bar processing stable, we treat a timestamp rollover as the signal
        # that the previous cached bar has finished.
        if self.pending_candle is None:
            if confirmed:
                self._enqueue_candle(candle)
            else:
                self.pending_candle = candle
            return

        pending_dt = pd.Timestamp(self.pending_candle["datetime"])
        current_dt = pd.Timestamp(candle["datetime"])

        if current_dt > pending_dt:
            self._enqueue_candle(self.pending_candle)
            self.pending_candle = None if confirmed else candle
            if confirmed:
                self._enqueue_candle(candle)
            return

        if current_dt == pending_dt:
            self.pending_candle = candle
            if confirmed:
                self._enqueue_candle(candle)
                self.pending_candle = None

    def _enqueue_candle(self, candle: Dict[str, float]) -> None:
        try:
            self.queue.put_nowait(candle)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(candle)

    def _maybe_log_live_candle(self, candle: Dict[str, float], confirmed: bool) -> None:
        dt = pd.Timestamp(candle["datetime"])
        if self.last_live_log_dt is not None and dt <= self.last_live_log_dt:
            return
        self.last_live_log_dt = dt
        self.logger(
            f"Live bar: dt={_format_log_dt(dt)}, close={float(candle['close']):.2f}, "
            f"high={float(candle['high']):.2f}, low={float(candle['low']):.2f}, confirmed={int(confirmed)}"
        )


class OkxPaperTrader:
    def __init__(self) -> None:
        self.config = self._load_config()
        self.proxy = self.config.get("proxy") or os.getenv("OKX_PROXY")
        self.inst_id = _normalize_inst_id(
            str(
                self.config.get("instId")
                or self.config.get("symbol")
                or self.config.get("trading_symbol")
                or DEFAULT_INST_ID
            )
        )
        self.bar = str(self.config.get("bar") or DEFAULT_BAR)
        self.ccxt_symbol = _inst_id_to_ccxt_symbol(self.inst_id)
        raw_start_time = (
            self.config.get("start_time")
            or self.config.get("startTime")
            or os.getenv("OKX_START_TIME")
            or ""
        )
        self.start_time = _normalize_start_time(str(raw_start_time))
        self.state_path = self._build_state_path()
        self.exchange = ccxt.okx(
            {
                "apiKey": self.config["apiKey"],
                "secret": self.config["secret"],
                "password": self.config["password"],
                "enableRateLimit": True,
                "timeout": REST_TIMEOUT_MS,
                "options": {"defaultType": "swap"},
            }
        )
        if self.proxy:
            self.exchange.httpProxy = self.proxy
        if str(self.config.get("flag", SIMULATED_FLAG)) == "1":
            self.exchange.set_sandbox_mode(True)
        self.account = self.exchange
        self.trade = self.exchange
        self.funding = self.exchange
        self.market = self.exchange
        self.public = self.exchange
        self.contract_value = 0.0
        self.contract_step = 1.0
        self.min_contracts = 1.0
        self.highs: List[float] = []
        self.lows: List[float] = []
        self.position = Position()
        self.market_df = pd.DataFrame()
        self.stop_price: Optional[float] = None
        self.last_position_sync_signature: Optional[tuple] = None
        self.last_exit_dt: Optional[pd.Timestamp] = None
        self.last_processed_dt: Optional[pd.Timestamp] = None
        self.last_closed_bar_poll_ts = 0.0
        self.cached_warmup_payload: Dict[str, Any] = {}
        self.initial_equity: Optional[float] = None
        self.initial_funding_balance: Optional[float] = None
        self.cum_withdraw = 0.0
        self.cum_topup_external = 0.0
        self.profit_pool = 0.0
        self.funding_exhausted = False
        self.funding_exhausted_notified = False
        self.last_known_trading_balance = {"total_eq": INITIAL_CASH, "available_eq": INITIAL_CASH}
        self.last_known_funding_balance = {"balance": 0.0, "available": 0.0}
        self.daily_pnl_stats: Dict[str, Dict[str, float]] = {}
        self.candle_stream = OkxCandleStream(
            self.inst_id,
            self.bar,
            self.config.get("flag", SIMULATED_FLAG),
            self.proxy,
            self._log,
        )
        self._load_state()
        self._retry_startup_step("load_instrument_meta", self._load_instrument_meta)
        self._retry_startup_step("configure_account", self._configure_account)
        self._retry_startup_step("sync_position", self._sync_position)

    def _log(self, message: str) -> None:
        print(message, flush=True)

    def _retry_startup_step(self, label: str, func, attempts: int = STARTUP_RETRY_ATTEMPTS) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                func()
                return
            except Exception as exc:
                last_error = exc
                self._log(f"Startup step failed: {label} ({attempt}/{attempts}) err={exc}")
                if attempt < attempts:
                    time.sleep(STARTUP_RETRY_BASE_DELAY * attempt)
        raise RuntimeError(f"Startup step failed after retries: {label}, err={last_error}")

    def _build_state_path(self) -> Path:
        start_label = self.start_time.strftime("%Y%m%dT%H%M%S") if self.start_time is not None else "latest"
        name = f"okx_paper_state_{_sanitize_filename(self.inst_id)}_{_sanitize_filename(self.bar)}_{start_label}.json"
        return Path(__file__).with_name(name)

    def _log_account_snapshot(self, prefix: str, refresh: bool = True) -> None:
        if refresh:
            trading = self._fetch_balance_snapshot()
            funding = self._fetch_funding_balance()
        else:
            trading = self.last_known_trading_balance
            funding = self.last_known_funding_balance
        self._log(
            f"{prefix} | trading_eq={trading['total_eq']:.2f}, trading_avail={trading['available_eq']:.2f}, "
            f"funding_bal={funding['balance']:.2f}, funding_avail={funding['available']:.2f}, "
            f"profit_pool={self.profit_pool:.2f}, cum_withdraw={self.cum_withdraw:.2f}, "
            f"cum_external_topup={self.cum_topup_external:.2f}"
        )

    def _normalize_daily_pnl_stats(self, raw: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        stats: Dict[str, Dict[str, float]] = {}
        for day, values in (raw or {}).items():
            if not isinstance(values, dict):
                continue
            stats[str(day)] = {
                "realized_pnl": _safe_float(values.get("realized_pnl"), 0.0),
                "fees": _safe_float(values.get("fees"), 0.0),
                "opens": _safe_float(values.get("opens"), 0.0),
                "closes": _safe_float(values.get("closes"), 0.0),
            }
        return stats

    def _record_daily_pnl(
        self,
        dt: pd.Timestamp,
        *,
        realized_pnl_delta: float = 0.0,
        fee_delta: float = 0.0,
        opens_delta: float = 0.0,
        closes_delta: float = 0.0,
    ) -> None:
        day = _daily_pnl_key(dt)
        bucket = self.daily_pnl_stats.setdefault(
            day,
            {"realized_pnl": 0.0, "fees": 0.0, "opens": 0.0, "closes": 0.0},
        )
        bucket["realized_pnl"] += float(realized_pnl_delta)
        bucket["fees"] += abs(float(fee_delta))
        bucket["opens"] += float(opens_delta)
        bucket["closes"] += float(closes_delta)

    def _log_daily_pnl_summary(self, current_dt: Optional[pd.Timestamp] = None, recent_days: int = 7) -> None:
        now = current_dt if current_dt is not None else pd.Timestamp.utcnow()
        today_key = _daily_pnl_key(pd.Timestamp(now))
        today = self.daily_pnl_stats.get(
            today_key,
            {"realized_pnl": 0.0, "fees": 0.0, "opens": 0.0, "closes": 0.0},
        )
        today_net = today["realized_pnl"] - today["fees"]
        self._log(
            f"Daily PnL today: day={today_key}, net={today_net:.4f}, realized={today['realized_pnl']:.4f}, "
            f"fees={today['fees']:.4f}, opens={int(today['opens'])}, closes={int(today['closes'])}"
        )
        recent_keys = sorted(self.daily_pnl_stats.keys(), reverse=True)[:recent_days]
        if not recent_keys:
            return
        parts = []
        for day in recent_keys:
            stats = self.daily_pnl_stats[day]
            net = stats["realized_pnl"] - stats["fees"]
            parts.append(
                f"{day}: net={net:.4f}, realized={stats['realized_pnl']:.4f}, "
                f"fees={stats['fees']:.4f}, opens={int(stats['opens'])}, closes={int(stats['closes'])}"
            )
        self._log("Recent daily PnL: " + " | ".join(parts))

    def _should_sync_position(self, loop_count: int, has_new_rows: bool) -> bool:
        if has_new_rows:
            return True
        if self.position.side != 0:
            return loop_count % POSITION_SYNC_ACTIVE_CYCLES == 0
        return loop_count % POSITION_SYNC_IDLE_CYCLES == 0

    @staticmethod
    def parse_candle_row(row: List[str]) -> Optional[Dict[str, float]]:
        if len(row) < 9:
            return None
        if str(row[8]) != "1":
            return None
        return OkxPaperTrader.parse_ws_candle_row(row)

    @staticmethod
    def parse_ws_candle_row(row: List[str]) -> Optional[Dict[str, float]]:
        if len(row) < 6:
            return None
        return {
            "datetime": pd.to_datetime(int(row[0]), unit="ms"),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }

    def _fetch_order_summary(self, order_id: str) -> Dict[str, float]:
        if not order_id:
            return {}
        try:
            row = self.trade.fetch_order(order_id, self.ccxt_symbol, {"type": "swap"})
        except Exception as exc:
            self._log(f"Order detail fetch failed: ordId={order_id}, err={exc}")
            return {}
        return {
            "avg_px": _safe_float(row.get("average"), 0.0),
            "acc_fill_sz": _safe_float(row.get("filled"), 0.0),
            "fee": _safe_float((row.get("fee") or {}).get("cost"), 0.0),
            "pnl": _safe_float((row.get("info") or {}).get("pnl"), 0.0),
            "state": row.get("state", ""),
        }

    def _calc_order_plan(self, close: float, side: str) -> Dict[str, float]:
        if close <= 0:
            return {"contracts": 0.0, "base_size": 0.0, "target_notional": 0.0, "max_notional_now": 0.0}
        if self.initial_equity is None:
            self.initial_equity = INITIAL_CASH
        snapshot = self._fetch_balance_snapshot()
        # 对应 bt_run.py 里的 _calc_order_size()：
        # 目标仓位先按“期初资金 * 风险比例 * 杠杆”计算，再受当前可用保证金约束。
        target_notional = self.initial_equity * RISK_PERCENT * LEVERAGE
        max_notional_now = snapshot["available_eq"] * LEVERAGE * ORDER_UTILIZATION
        notional = min(target_notional, max_notional_now)
        if notional <= 0:
            return {
                "contracts": 0.0,
                "base_size": 0.0,
                "target_notional": target_notional,
                "max_notional_now": max_notional_now,
            }
        base_size = notional / close
        contracts = base_size / self.contract_value
        contracts = min(contracts, self._fetch_max_contracts(side))
        contracts = _floor_to_step(contracts, self.contract_step)
        if contracts + 1e-12 < self.min_contracts:
            contracts = 0.0
        return {
            "contracts": contracts,
            "base_size": contracts * self.contract_value,
            "target_notional": target_notional,
            "max_notional_now": max_notional_now,
        }

    def _load_config(self) -> Dict[str, Any]:
        base_dir = Path(__file__).resolve().parent
        local_cfg = base_dir / f"{CONFIG_BASENAME}.local"
        cfg_path = local_cfg if local_cfg.exists() else base_dir / CONFIG_BASENAME
        with cfg_path.open("r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["apiKey"] = os.getenv("OKX_API_KEY", cfg.get("apiKey", ""))
        cfg["secret"] = os.getenv("OKX_API_SECRET", cfg.get("secret", ""))
        cfg["password"] = os.getenv("OKX_API_PASSPHRASE", cfg.get("password", ""))
        cfg["flag"] = os.getenv("OKX_FLAG", cfg.get("flag", SIMULATED_FLAG))
        if not cfg["apiKey"] or cfg["apiKey"] == "your api key":
            raise ValueError("Missing OKX API credentials. Update examples/config.local or environment variables.")
        return cfg

    def _ok(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        if response.get("code") != "0":
            raise RuntimeError(f"OKX API error: code={response.get('code')} msg={response.get('msg')}")
        return response.get("data", [])

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        stop_price = data.get("stop_price")
        self.stop_price = float(stop_price) if stop_price is not None else None
        last_exit = data.get("last_exit_dt")
        self.last_exit_dt = pd.Timestamp(last_exit) if last_exit else None
        # Trading capital anchor is fixed at INITIAL_CASH; ignore historical larger values.
        self.initial_equity = INITIAL_CASH
        self.initial_funding_balance = _safe_float(data.get("initial_funding_balance"), 0.0) or None
        self.cum_withdraw = _safe_float(data.get("cum_withdraw"), 0.0)
        self.cum_topup_external = _safe_float(data.get("cum_topup_external"), 0.0)
        self.profit_pool = _safe_float(data.get("profit_pool"), 0.0)
        # Cash transfer policies are disabled in demo mode; avoid stale funding flags blocking trading.
        self.funding_exhausted = False
        self.funding_exhausted_notified = False
        self.last_known_trading_balance = {
            "total_eq": _safe_float(data.get("last_known_trading_balance", {}).get("total_eq"), INITIAL_CASH),
            "available_eq": _safe_float(data.get("last_known_trading_balance", {}).get("available_eq"), INITIAL_CASH),
        }
        self.last_known_funding_balance = {
            "balance": _safe_float(data.get("last_known_funding_balance", {}).get("balance"), 0.0),
            "available": _safe_float(data.get("last_known_funding_balance", {}).get("available"), 0.0),
        }
        self.daily_pnl_stats = self._normalize_daily_pnl_stats(data.get("daily_pnl_stats", {}))
        self.cached_warmup_payload = data.get("warmup_cache", {}) or {}

    def _save_state(self) -> None:
        warmup_cache = self._build_warmup_cache_payload()
        payload = {
            "stop_price": self.stop_price,
            "last_exit_dt": self.last_exit_dt.isoformat() if self.last_exit_dt is not None else None,
            "initial_equity": self.initial_equity,
            "initial_funding_balance": self.initial_funding_balance,
            "cum_withdraw": self.cum_withdraw,
            "cum_topup_external": self.cum_topup_external,
            "profit_pool": self.profit_pool,
            "funding_exhausted": self.funding_exhausted,
            "funding_exhausted_notified": self.funding_exhausted_notified,
            "last_known_trading_balance": self.last_known_trading_balance,
            "last_known_funding_balance": self.last_known_funding_balance,
            "daily_pnl_stats": self.daily_pnl_stats,
            "warmup_cache": warmup_cache,
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _build_strategy_signature(self) -> Dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "bar": self.bar,
            "start_time": self.start_time.isoformat() if self.start_time is not None else None,
            "pivot_period": PIVOT_PERIOD,
            "atr_period": ATR_PERIOD,
            "adx_period": ADX_PERIOD,
            "adx_threshold": ADX_THRESHOLD,
            "bb_period": BB_PERIOD,
            "bb_dev": BB_DEV,
            "bb_bandwidth_threshold": BB_BANDWIDTH_THRESHOLD,
            "min_swing_atr_mult": MIN_SWING_ATR_MULT,
            "min_ema_spread_pct": MIN_EMA_SPREAD_PCT,
            "cooldown_bars": COOLDOWN_BARS,
            "ema_fast": EMA_FAST,
            "ema_slow": EMA_SLOW,
            "warmup_bars": _indicator_warmup_bars(),
        }

    def _build_warmup_cache_payload(self) -> Dict[str, Any]:
        if self.market_df.empty or self.last_processed_dt is None:
            return {}
        raw_cols = ["datetime", "open", "high", "low", "close", "volume"]
        available_cols = [col for col in raw_cols if col in self.market_df.columns]
        if len(available_cols) != len(raw_cols):
            return {}
        recent_df = self.market_df[raw_cols].tail(WARMUP_CACHE_TAIL_BARS).copy()
        candles = []
        for row in recent_df.to_dict(orient="records"):
            candles.append(
                {
                    "datetime": pd.Timestamp(row["datetime"]).isoformat(),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )
        return {
            "signature": self._build_strategy_signature(),
            "saved_at": pd.Timestamp.utcnow().isoformat(),
            "last_processed_dt": self.last_processed_dt.isoformat(),
            "candles": candles,
        }

    def _load_instrument_meta(self) -> None:
        response = self.exchange.public_get_public_instruments({"instType": "SWAP", "instId": self.inst_id})
        rows = self._ok(response)
        if not rows:
            raise RuntimeError(f"No instrument meta found for {self.inst_id}")
        parsed_market = self.exchange.parse_market(rows[0])
        self.exchange.set_markets([parsed_market])
        market = self.exchange.market(self.ccxt_symbol)
        self.contract_value = _safe_float(market.get("contractSize"), 0.0)
        if self.contract_value <= 0:
            raise RuntimeError(f"Invalid contract value for {self.inst_id}: {market}")
        amount_precision = _safe_float((market.get("precision") or {}).get("amount"), 0.0)
        self.contract_step = max(amount_precision, 1e-9)
        self.min_contracts = max(_safe_float((market.get("limits") or {}).get("amount", {}).get("min"), self.contract_step), self.contract_step)
        self._log(
            f"Loaded instrument meta: instId={self.inst_id}, ctVal={self.contract_value}, "
            f"lotSz={self.contract_step}, minSz={self.min_contracts}"
        )

    def _configure_account(self) -> None:
        try:
            self.account.set_position_mode(POS_MODE == "long_short_mode")
            self._log(f"Position mode set to {POS_MODE}")
        except Exception as exc:
            self._log(f"Position mode not changed: {exc}")

        for pos_side in ("long", "short"):
            try:
                self.account.set_leverage(int(LEVERAGE), self.ccxt_symbol, {"marginMode": TD_MODE, "posSide": pos_side})
                self._log(f"Leverage set: side={pos_side}, leverage={LEVERAGE}x")
            except Exception as exc:
                self._log(f"Set leverage failed for {pos_side}: {exc}")

    def _fetch_balance_snapshot(self) -> Dict[str, float]:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                balance = self.account.fetch_balance({"type": "swap"})
                rows = (balance.get("info") or {}).get("data") or []
                if not rows:
                    self.last_known_trading_balance = {"total_eq": INITIAL_CASH, "available_eq": INITIAL_CASH}
                    return self.last_known_trading_balance
                account_row = rows[0]
                total_eq = _safe_float(account_row.get("totalEq"), INITIAL_CASH)
                available_eq = _safe_float(account_row.get("availEq"), 0.0)
                details = account_row.get("details", [])
                usdt_detail = next((item for item in details if item.get("ccy") == SETTLE_CCY), {})
                if available_eq <= 0:
                    available_eq = max(
                        _safe_float(usdt_detail.get("availEq"), 0.0),
                        _safe_float(usdt_detail.get("availBal"), 0.0),
                        _safe_float(usdt_detail.get("cashBal"), 0.0),
                        _safe_float(account_row.get("adjEq"), 0.0),
                    )
                if total_eq <= 0:
                    total_eq = max(
                        _safe_float(usdt_detail.get("eq"), 0.0),
                        _safe_float(usdt_detail.get("cashBal"), 0.0),
                        INITIAL_CASH,
                    )
                self.last_known_trading_balance = {"total_eq": total_eq, "available_eq": available_eq}
                return self.last_known_trading_balance
            except Exception as exc:
                last_error = exc
                self._log(f"Trading balance fetch failed ({attempt}/3): {exc}")
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        self._log(
            "Trading balance fetch fallback: using last known trading snapshot "
            f"{self.last_known_trading_balance} due to error={last_error}"
        )
        return self.last_known_trading_balance

    def _fetch_funding_balance(self) -> Dict[str, float]:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                rows = self._ok(self.funding.private_get_asset_balances({"ccy": SETTLE_CCY}))
                if not rows:
                    self.last_known_funding_balance = {"balance": 0.0, "available": 0.0}
                    return self.last_known_funding_balance
                row = rows[0]
                balance = max(
                    _safe_float(row.get("bal"), 0.0),
                    _safe_float(row.get("cashBal"), 0.0),
                    _safe_float(row.get("availBal"), 0.0),
                )
                available = max(
                    _safe_float(row.get("availBal"), 0.0),
                    _safe_float(row.get("bal"), 0.0),
                    _safe_float(row.get("cashBal"), 0.0),
                )
                self.last_known_funding_balance = {"balance": balance, "available": available}
                return self.last_known_funding_balance
            except Exception as exc:
                last_error = exc
                self._log(f"Funding balance fetch failed ({attempt}/3): {exc}")
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        self._log(
            "Funding balance fetch fallback: using last known funding snapshot "
            f"{self.last_known_funding_balance} due to error={last_error}"
        )
        return self.last_known_funding_balance

    def _transfer_between_accounts(self, amount: float, from_account: str, to_account: str, reason: str) -> float:
        amount = round(max(amount, 0.0), 8)
        if amount <= 0:
            return 0.0
        response = self.funding.private_post_asset_transfer(
            {"ccy": SETTLE_CCY, "amt": str(amount), "from": from_account, "to": to_account, "type": "0"}
        )
        rows = self._ok(response)
        trans_id = rows[0].get("transId", "")
        self._log(
            f"Funds transfer: reason={reason}, amount={amount:.4f} {SETTLE_CCY}, "
            f"from={from_account}, to={to_account}, transId={trans_id}"
        )
        return amount

    def _apply_cashflow_policy(self) -> None:
        # Demo environment uses fixed isolated sizing without internal cash transfers.
        self.funding_exhausted = False
        self.funding_exhausted_notified = False

    def _rebalance_trading_excess_to_target(self) -> None:
        return

    def _can_continue_trading_when_funding_exhausted(self) -> bool:
        if self.initial_equity is None:
            return True
        trading = self._fetch_balance_snapshot()
        current_real_pnl = trading["total_eq"] + self.cum_withdraw - self.cum_topup_external - self.initial_equity
        external_left = MAX_EXTERNAL_TOPUP - self.cum_topup_external
        return (external_left > 1e-9) or (current_real_pnl > 0.0)

    def _sync_position(self) -> None:
        rows = self.account.fetch_positions([self.ccxt_symbol], {"type": "swap"})
        active = []
        for row in rows:
            info = row.get("info") or {}
            contracts = abs(_safe_float(row.get("contracts"), _safe_float(info.get("pos"), 0.0)))
            if contracts <= 0:
                continue
            pos_side = (row.get("side") or info.get("posSide") or "").lower()
            if pos_side == "long":
                side = 1
            elif pos_side == "short":
                side = -1
            else:
                continue
            active.append(
                Position(
                    side=side,
                    contracts=contracts,
                    base_size=contracts * self.contract_value,
                    avg_price=_safe_float(row.get("entryPrice"), _safe_float(info.get("avgPx"), 0.0)),
                    pos_side=pos_side,
                )
            )
        if len(active) > 1:
            raise RuntimeError(f"Expected at most one directional position, got {len(active)}")
        self.position = active[0] if active else Position()
        if self.position.side == 0:
            self.stop_price = None
        signature = (
            self.position.side,
            round(self.position.contracts, 12),
            round(self.position.avg_price, 8),
            self.position.pos_side,
            None if self.stop_price is None else round(self.stop_price, 8),
        )
        if signature != self.last_position_sync_signature:
            self.last_position_sync_signature = signature
            self._log(
                f"Position sync: side={self.position.side}, contracts={self.position.contracts}, "
                f"avg_price={self.position.avg_price}, stop={self.stop_price}"
            )

    def _parse_candle_rows(self, rows: List[List[str]]) -> pd.DataFrame:
        parsed = []
        for row in rows:
            candle = self.parse_candle_row(row)
            if candle is not None:
                parsed.append(candle)
        return pd.DataFrame(parsed)

    def _append_market_candle(self, candle: Dict[str, float]) -> Optional[int]:
        next_df = pd.concat([self.market_df, pd.DataFrame([candle])], ignore_index=True)
        next_df = next_df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
        if len(next_df) > HISTORY_LIMIT:
            next_df = next_df.iloc[-HISTORY_LIMIT:].reset_index(drop=True)
        next_df = self._update_indicators(next_df)
        self.market_df = next_df
        matches = next_df.index[next_df["datetime"] == candle["datetime"]]
        if len(matches) == 0:
            return None
        return int(matches[-1])

    def _fetch_candle_page(self, *, limit: int, after: str = "", before: str = "") -> List[List[str]]:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                params = {"instId": self.inst_id, "bar": self.bar, "limit": str(limit)}
                if after:
                    params["after"] = after
                if before:
                    params["before"] = before
                return self._ok(
                    self.market.public_get_market_history_candles(params)
                )
            except Exception as exc:
                last_error = exc
                self._log(f"Candle fetch failed ({attempt}/3): {type(exc).__name__}: {exc}")
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        raise RuntimeError(f"Failed to fetch candle page after retries: {last_error}")

    def _fetch_candles(self, bootstrap: bool = False) -> pd.DataFrame:
        history_start = None
        if bootstrap and self.start_time is not None:
            history_start = self.start_time - pd.Timedelta(seconds=_bar_seconds(self.bar) * _indicator_warmup_bars())

        if history_start is None:
            rows = self._fetch_candle_page(limit=HISTORY_LIMIT)
            if not rows:
                raise RuntimeError("No candles returned from OKX")
            df = self._parse_candle_rows(rows)
            if df.empty:
                raise RuntimeError("No confirmed candles available")
            return df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

        pages: List[pd.DataFrame] = []
        after = ""
        reached_history_start = False
        for page_idx in range(1, MAX_HISTORY_BACKFILL_PAGES + 1):
            rows = self._fetch_candle_page(limit=HISTORY_PAGE_LIMIT, after=after)
            if not rows:
                break
            page_df = self._parse_candle_rows(rows)
            if page_df.empty:
                break
            pages.append(page_df)
            oldest_dt = pd.Timestamp(page_df["datetime"].min())
            oldest_ms = str(int(page_df["datetime"].min().timestamp() * 1000))
            newest_dt = pd.Timestamp(page_df["datetime"].max())
            self._log(
                f"History backfill page {page_idx}: newest={newest_dt}, oldest={oldest_dt}, rows={len(page_df)}"
            )
            if oldest_dt <= history_start:
                reached_history_start = True
                break
            # The API page may contain an in-progress candle that we filter out, so pagination
            # must use raw row count instead of confirmed-row count.
            if len(rows) < HISTORY_PAGE_LIMIT:
                break
            after = oldest_ms

        if not pages:
            raise RuntimeError("No candles returned from OKX for bootstrap")

        df = pd.concat(pages, ignore_index=True)
        df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        df = df[df["datetime"] >= history_start].reset_index(drop=True)
        if df.empty:
            raise RuntimeError(f"No candles available after requested history_start={history_start.isoformat()}")
        if not reached_history_start:
            self._log(
                f"History backfill warning: reached only {df['datetime'].min()} before stop, "
                f"requested history_start={history_start}"
            )
        return df

    def _fetch_latest_closed_candle(self) -> Optional[Dict[str, float]]:
        candles = self._fetch_latest_closed_candles(limit=3)
        if not candles:
            return None
        return candles[-1]

    def _fetch_latest_closed_candles(self, limit: int) -> List[Dict[str, float]]:
        rows = self._fetch_candle_page(limit=limit)
        candles = []
        for row in rows:
            candle = self.parse_candle_row(row)
            if candle is not None:
                candles.append(candle)
        candles.sort(key=lambda item: pd.Timestamp(item["datetime"]))
        return candles

    def _maybe_poll_latest_closed_candle(self) -> Optional[Dict[str, float]]:
        now = time.time()
        if now - self.last_closed_bar_poll_ts < CLOSED_BAR_POLL_INTERVAL_SECONDS:
            return None
        self.last_closed_bar_poll_ts = now
        candle = self._fetch_latest_closed_candle()
        if candle is None:
            return None
        candle_dt = pd.Timestamp(candle["datetime"])
        if self.last_processed_dt is not None and candle_dt <= self.last_processed_dt:
            return None
        self._log(
            f"REST closed bar fallback: dt={_format_log_dt(candle_dt)}, close={float(candle['close']):.2f}"
        )
        return candle

    def _update_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        # 对应 bt_run.py 里 __init__ 的指标定义：
        # 这里沿用同样的 SMA(10/20)、ATR、ADX、布林带过滤逻辑。
        out["ema_fast"] = out["close"].rolling(EMA_FAST, min_periods=EMA_FAST).mean()
        out["ema_slow"] = out["close"].rolling(EMA_SLOW, min_periods=EMA_SLOW).mean()
        out["atr"] = _atr(out, ATR_PERIOD)
        out["adx"] = _adx(out, ADX_PERIOD)
        mid = out["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
        std = out["close"].rolling(BB_PERIOD, min_periods=BB_PERIOD).std(ddof=0)
        out["bb_top"] = mid + BB_DEV * std
        out["bb_bot"] = mid - BB_DEV * std
        return out

    def _bar_gap_count(self, earlier: pd.Timestamp, later: pd.Timestamp) -> int:
        delta_seconds = max((later - earlier).total_seconds(), 0.0)
        return int(round(delta_seconds / _bar_seconds(self.bar)))

    def _rebuild_pivots_from_market_df(self) -> None:
        self.highs = []
        self.lows = []
        if self.market_df.empty:
            return
        for idx in range(len(self.market_df)):
            self._detect_pivot(self.market_df, idx)

    def _candles_from_cache_payload(self, payload: Dict[str, Any]) -> Optional[pd.DataFrame]:
        rows = payload.get("candles")
        if not isinstance(rows, list) or not rows:
            return None
        parsed = []
        for row in rows:
            try:
                parsed.append(
                    {
                        "datetime": pd.Timestamp(row["datetime"]),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0.0)),
                    }
                )
            except Exception:
                return None
        df = pd.DataFrame(parsed)
        if df.empty:
            return None
        df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        if len(df) < _indicator_warmup_bars():
            return None
        return df

    def _validate_warmup_cache(self) -> Optional[Dict[str, Any]]:
        payload = self.cached_warmup_payload
        if not payload:
            self._log("Warmup cache miss: no cached payload.")
            return None
        if payload.get("signature") != self._build_strategy_signature():
            self._log("Warmup cache invalid: strategy signature changed.")
            return None
        df = self._candles_from_cache_payload(payload)
        if df is None:
            self._log("Warmup cache invalid: cached candles incomplete.")
            return None
        cached_last_dt = pd.Timestamp(payload.get("last_processed_dt")) if payload.get("last_processed_dt") else None
        if cached_last_dt is None or pd.Timestamp(df.iloc[-1]["datetime"]) != cached_last_dt:
            self._log("Warmup cache invalid: last_processed_dt mismatch.")
            return None
        latest_exchange = self._fetch_latest_closed_candles(limit=WARMUP_CACHE_VALIDATION_BARS)
        if not latest_exchange:
            self._log("Warmup cache validation skipped: no latest closed candles fetched.")
            return None
        latest_exchange_dt = pd.Timestamp(latest_exchange[-1]["datetime"])
        if latest_exchange_dt < cached_last_dt:
            self._log("Warmup cache invalid: cached timestamp is ahead of exchange data.")
            return None
        gap_bars = self._bar_gap_count(cached_last_dt, latest_exchange_dt)
        if gap_bars > WARMUP_CACHE_MAX_GAP_BARS:
            self._log(
                f"Warmup cache invalid: gap too large ({gap_bars} bars > {WARMUP_CACHE_MAX_GAP_BARS})."
            )
            return None
        exchange_by_dt = {pd.Timestamp(item["datetime"]): item for item in latest_exchange}
        overlap_rows = []
        for _, row in df.tail(WARMUP_CACHE_VALIDATION_BARS).iterrows():
            dt = pd.Timestamp(row["datetime"])
            remote = exchange_by_dt.get(dt)
            if remote is None:
                continue
            overlap_rows.append((row, remote))
        if not overlap_rows:
            self._log("Warmup cache invalid: no overlap with latest exchange candles.")
            return None
        for local_row, remote_row in overlap_rows:
            for key in ("open", "high", "low", "close"):
                if abs(float(local_row[key]) - float(remote_row[key])) > 1e-8:
                    self._log(
                        f"Warmup cache invalid: candle mismatch at {_format_log_dt(pd.Timestamp(local_row['datetime']))}."
                    )
                    return None
        return {"df": df, "cached_last_dt": cached_last_dt, "latest_exchange_dt": latest_exchange_dt}

    def _catch_up_cached_market_state(self) -> int:
        latest_rows = self._fetch_latest_closed_candles(limit=HISTORY_LIMIT)
        if not latest_rows or self.last_processed_dt is None:
            return 0
        new_rows = [row for row in latest_rows if pd.Timestamp(row["datetime"]) > self.last_processed_dt]
        applied = 0
        for candle in new_rows:
            idx = self._append_market_candle(candle)
            if idx is None:
                continue
            self._detect_pivot(self.market_df, idx)
            self.last_processed_dt = pd.Timestamp(candle["datetime"])
            applied += 1
        return applied

    def _restore_warmup_cache(self) -> bool:
        try:
            validated = self._validate_warmup_cache()
        except Exception as exc:
            self._log(f"Warmup cache validation failed: {exc}")
            return False
        if not validated:
            return False
        self.market_df = self._update_indicators(validated["df"])
        self.last_processed_dt = validated["cached_last_dt"]
        self._rebuild_pivots_from_market_df()
        applied = self._catch_up_cached_market_state()
        self._save_state()
        self._log(
            f"Warmup cache restored: last_processed={_format_log_dt(self.last_processed_dt)}, "
            f"cached_bars={len(self.market_df)}, catchup_bars={applied}"
        )
        return True

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
        # 对应 bt_run.py 里 next() 的 Fractal Pivot 判定：
        # 只有左右各有 p 根K线都确认后，这个 pivot 才成立。
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
        return int(delta.total_seconds() // _bar_seconds(self.bar))

    def _fetch_max_contracts(self, side: str) -> float:
        try:
            rows = self._ok(self.account.private_get_account_max_size({"instId": self.inst_id, "tdMode": TD_MODE}))
        except Exception:
            return float("inf")
        if not rows:
            return float("inf")
        row = rows[0]
        keys = ["maxBuy"] if side == "buy" else ["maxSell"]
        for key in keys:
            value = _safe_float(row.get(key), 0.0)
            if value > 0:
                return value
        return float("inf")

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
        order_side = "buy" if side > 0 else "sell"
        pos_side = "long" if side > 0 else "short"
        plan = self._calc_order_plan(close, order_side)
        contracts = plan["contracts"]
        if contracts <= 0:
            self._log(
                f"{dt} skip entry: reason={signal_reason}, close={close:.2f}, "
                f"target_notional={plan['target_notional']:.2f}, max_notional={plan['max_notional_now']:.2f}"
            )
            return
        self._log(
            f"{_format_log_dt(dt)} signal: action=open_{pos_side}, reason={signal_reason}, close={close:.2f}, stop={stop:.2f}, "
            f"atr={atr:.4f}, adx={adx:.2f}, bb_width={bb_width:.4f}, ema_spread={ema_spread:.4f}, "
            f"contracts={contracts}, base_size={plan['base_size']:.6f}, target_notional={plan['target_notional']:.2f}, "
            f"max_notional={plan['max_notional_now']:.2f}"
        )
        self._log_account_snapshot("Before entry")
        response = self.trade.create_order(
            self.ccxt_symbol,
            "market",
            order_side,
            contracts,
            None,
            {"tdMode": TD_MODE, "posSide": pos_side},
        )
        order_id = response.get("id", "")
        self.stop_price = stop
        self._log(
            f"{_format_log_dt(dt)} entry sent: side={pos_side}, contracts={contracts}, "
            f"instId={self.inst_id}, bar={self.bar}, close={close:.2f}, stop={stop:.2f}, ordId={order_id}"
        )
        time.sleep(1.0)
        order_summary = self._fetch_order_summary(order_id)
        if order_summary:
            self._record_daily_pnl(
                dt,
                fee_delta=order_summary.get("fee", 0.0),
                opens_delta=1.0,
            )
            self._log(
                f"{_format_log_dt(dt)} entry result: ordId={order_id}, state={order_summary.get('state')}, "
                f"avg_px={order_summary.get('avg_px', 0.0):.4f}, fill_sz={order_summary.get('acc_fill_sz', 0.0):.4f}, "
                f"fee={order_summary.get('fee', 0.0):.6f}"
            )
            self._log_daily_pnl_summary(dt)
        self._sync_position()
        self._log_account_snapshot("After entry")
        self._save_state()

    def _close_position(self, dt: pd.Timestamp, reason: str, trigger_price: float) -> None:
        if self.position.side == 0:
            return
        approx_pnl = (trigger_price - self.position.avg_price) * self.position.base_size * self.position.side
        self._log(
            f"{_format_log_dt(dt)} signal: action=close_{self.position.pos_side}, reason={reason}, trigger_price={trigger_price:.2f}, "
            f"entry_price={self.position.avg_price:.2f}, contracts={self.position.contracts}, "
            f"base_size={self.position.base_size:.6f}, approx_pnl={approx_pnl:.4f}"
        )
        self._log_account_snapshot("Before close")
        order_side = "sell" if self.position.side > 0 else "buy"
        response = self.trade.create_order(
            self.ccxt_symbol,
            "market",
            order_side,
            self.position.contracts,
            None,
            {"reduceOnly": True, "tdMode": TD_MODE, "posSide": self.position.pos_side},
        )
        order_id = response.get("id", "")
        self._log(f"{_format_log_dt(dt)} close sent: reason={reason}, posSide={self.position.pos_side}, ordId={order_id}")
        time.sleep(1.0)
        order_summary = self._fetch_order_summary(order_id)
        if order_summary:
            self._record_daily_pnl(
                dt,
                realized_pnl_delta=order_summary.get("pnl", 0.0),
                fee_delta=order_summary.get("fee", 0.0),
                closes_delta=1.0,
            )
            self._log(
                f"{_format_log_dt(dt)} close result: ordId={order_id}, state={order_summary.get('state')}, "
                f"avg_px={order_summary.get('avg_px', 0.0):.4f}, fill_sz={order_summary.get('acc_fill_sz', 0.0):.4f}, "
                f"fee={order_summary.get('fee', 0.0):.6f}, pnl={order_summary.get('pnl', 0.0):.4f}"
            )
            self._log_daily_pnl_summary(dt)
        self._sync_position()
        self._log_account_snapshot("After close")
        self.last_exit_dt = dt
        self.stop_price = None
        self._save_state()

    def _bootstrap_history(self, df: pd.DataFrame) -> None:
        if df.empty:
            raise RuntimeError("No candles available for bootstrap")
        self.highs = []
        self.lows = []
        warmup_bars = min(_indicator_warmup_bars(), len(df))
        self._log(
            f"Warmup started: bars={warmup_bars}, total_history={len(df)}, "
            f"mode={'replay_from_start_time' if self.start_time is not None else 'warmup_only'}"
        )
        replay_started = False
        replay_bar_count = 0
        for idx in range(len(df)):
            dt = pd.Timestamp(df.iloc[idx]["datetime"])
            if (
                not replay_started
                and self.start_time is not None
                and idx >= warmup_bars
                and dt >= self.start_time
            ):
                replay_started = True
                self._log(
                    f"Bootstrap replay started: dt={_format_log_dt(dt)}, "
                    f"configured_start_time={_format_log_dt(self.start_time)}"
                )
            if replay_started:
                # 这里是新脚本相对 bt_run.py 的补充：
                # bt_run.py 天然是整段历史回测；实盘脚本需要先回放历史K线，把状态补齐后再接实时流。
                self._on_bar(df, idx)
                replay_bar_count += 1
            else:
                # 对应 bt_run.py 里前期历史样本的自然暖机过程；
                # 在实盘脚本里这里显式只做指标和 pivot 暖机，不触发交易。
                self._detect_pivot(df, idx)
            self.last_processed_dt = dt
        self.last_processed_dt = pd.Timestamp(df.iloc[-1]["datetime"])
        self._save_state()
        self._log(
            f"Warmup done: last_processed={_format_log_dt(self.last_processed_dt)}, "
            f"instId={self.inst_id}, bar={self.bar}, warmup_bars={warmup_bars}, "
            f"start_time={_format_log_dt(self.start_time)}, "
            f"replay_bars={replay_bar_count}, highs={self.highs[-2:]}, lows={self.lows[-2:]}, stop={self.stop_price}"
        )

    def _on_bar(self, df: pd.DataFrame, idx: int) -> None:
        row = df.iloc[idx]
        dt = pd.Timestamp(row["datetime"])
        close = float(row["close"])
        self._detect_pivot(df, idx)

        atr = float(row["atr"]) if not pd.isna(row["atr"]) else np.nan
        adx = float(row["adx"]) if not pd.isna(row["adx"]) else np.nan
        bb_top = float(row["bb_top"]) if not pd.isna(row["bb_top"]) else np.nan
        bb_bot = float(row["bb_bot"]) if not pd.isna(row["bb_bot"]) else np.nan
        ema_fast = float(row["ema_fast"]) if not pd.isna(row["ema_fast"]) else np.nan
        ema_slow = float(row["ema_slow"]) if not pd.isna(row["ema_slow"]) else np.nan

        if any(np.isnan(v) for v in (atr, adx, bb_top, bb_bot, ema_fast, ema_slow)):
            return

        bb_width = (bb_top - bb_bot) / max(abs(close), 1e-9)
        ema_spread = abs(ema_fast - ema_slow) / max(abs(close), 1e-9)

        # 对应 bt_run.py next() 里的 cooldown + ADX/布林带/均线发散过滤。
        if self.position.side == 0 and self._bars_since_exit(dt) <= COOLDOWN_BARS:
            return
        if (adx < ADX_THRESHOLD) or (bb_width < BB_BANDWIDTH_THRESHOLD) or (ema_spread < MIN_EMA_SPREAD_PCT):
            return

        if self.position.side == 0:
            if len(self.lows) >= 2:
                # 对应 bt_run.py 的做多分支：
                # 最近两个 pivot low 抬高（Higher Low），且抬高幅度至少达到 ATR 阈值。
                swing_ok = (self.lows[-1] - self.lows[-2]) >= MIN_SWING_ATR_MULT * max(atr, 1e-9)
                if self.lows[-1] > self.lows[-2] and swing_ok:
                    self._place_entry(
                        1,
                        close,
                        self.lows[-1],
                        dt,
                        "higher_low",
                        atr,
                        adx,
                        bb_width,
                        ema_spread,
                    )
                    return
            if len(self.highs) >= 2:
                # 对应 bt_run.py 的做空分支：
                # 最近两个 pivot high 降低（Lower High），逻辑与做多对称。
                swing_ok = (self.highs[-2] - self.highs[-1]) >= MIN_SWING_ATR_MULT * max(atr, 1e-9)
                if self.highs[-1] < self.highs[-2] and swing_ok:
                    self._place_entry(
                        -1,
                        close,
                        self.highs[-1],
                        dt,
                        "lower_high",
                        atr,
                        adx,
                        bb_width,
                        ema_spread,
                    )
                    return
        elif self.position.side > 0:
            # 对应 bt_run.py 的多单离场：
            # 先看 stop_price 止损，否则当最新高点转弱时按动能衰竭平仓。
            if self.stop_price is not None and close <= self.stop_price:
                self._close_position(dt, "long_stop", close)
            elif len(self.highs) >= 2 and self.highs[-1] < self.highs[-2]:
                self._close_position(dt, "long_momentum_exhausted", close)
        else:
            # 对应 bt_run.py 的空单离场，和多单完全对称。
            if self.stop_price is not None and close >= self.stop_price:
                self._close_position(dt, "short_stop", close)
            elif len(self.lows) >= 2 and self.lows[-1] > self.lows[-2]:
                self._close_position(dt, "short_momentum_exhausted", close)

    def run(self) -> None:
        consecutive_network_errors = 0
        loop_count = 0
        self._fetch_balance_snapshot()
        if self.initial_equity is None:
            self.initial_equity = INITIAL_CASH
        funding_snapshot = self._fetch_funding_balance()
        if self.initial_funding_balance is None:
            self.initial_funding_balance = funding_snapshot["balance"]
        self._log(
            f"Starting OKX paper trader: instId={self.inst_id}, bar={self.bar}, "
            f"start_time={_format_log_dt(self.start_time)}, "
            f"flag={self.config.get('flag', SIMULATED_FLAG)}, trading_eq={self.initial_equity:.2f}, "
            f"funding_bal={funding_snapshot['balance']:.2f}, state_file={self.state_path.name}"
        )
        self._log_account_snapshot("Startup snapshot", refresh=False)
        self._log_daily_pnl_summary()
        restored_from_cache = False
        try:
            restored_from_cache = self._restore_warmup_cache()
        except Exception as exc:
            self._log(f"Warmup cache restore failed: {exc}")
            restored_from_cache = False
        if not restored_from_cache:
            while True:
                try:
                    # 这里是新脚本和 bt_run.py 最大的运行差异：
                    # bt_run.py 直接喂整段历史做回测；这里先用 REST 历史K线重建状态，再切到实时WebSocket。
                    self.market_df = self._update_indicators(self._fetch_candles(bootstrap=True))
                    self._bootstrap_history(self.market_df)
                    self._save_state()
                    break
                except Exception as exc:
                    self._log(f"Startup market bootstrap failed: {exc}")
                    time.sleep(POLL_SECONDS)

        self.candle_stream.start()

        while True:
            try:
                loop_count += 1
                # 对应 bt_run.py 的 next() 每根K线调用一次；
                # 新脚本里只有收到一根新的已确认K线时，才触发一次同等策略判断。
                candle = self.candle_stream.get_next_candle(timeout=WS_QUEUE_TIMEOUT_SECONDS)
                if candle is None:
                    candle = self._maybe_poll_latest_closed_candle()
                has_new_bar = candle is not None and (
                    self.last_processed_dt is None or pd.Timestamp(candle["datetime"]) > self.last_processed_dt
                )
                if self._should_sync_position(loop_count, has_new_bar):
                    self._sync_position()
                if not has_new_bar:
                    continue
                idx = self._append_market_candle(candle)
                if idx is None:
                    continue
                row = self.market_df.iloc[idx]
                self._log(
                    f"New bar: dt={_format_log_dt(pd.Timestamp(row['datetime']))}, close={float(row['close']):.2f}, "
                    f"position_side={self.position.side}, stop={self.stop_price}"
                )
                self._on_bar(self.market_df, idx)
                self.last_processed_dt = pd.Timestamp(row["datetime"])
                if self.position.side == 0:
                    self._apply_cashflow_policy()
                    if self.funding_exhausted and not self._can_continue_trading_when_funding_exhausted():
                        self._log("Funding exhausted and continuation conditions not met; waiting for next cycle.")
                self._save_state()
                if consecutive_network_errors > 0:
                    self._log("Network recovered; restoring normal poll interval.")
                consecutive_network_errors = 0
            except KeyboardInterrupt:
                self._log("Stopped by user.")
                self.candle_stream.stop()
                break
            except Exception as exc:
                self._log(f"Loop error: {exc}")
                if _is_retryable_network_error(exc):
                    consecutive_network_errors += 1
                    backoff = min(POLL_SECONDS * (2 ** min(consecutive_network_errors - 1, 3)), MAX_NETWORK_BACKOFF_SECONDS)
                    self._log(
                        f"Network backoff: consecutive_errors={consecutive_network_errors}, "
                        f"sleep={backoff}s"
                    )
                    time.sleep(backoff)
                else:
                    consecutive_network_errors = 0
                    time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    OkxPaperTrader().run()
