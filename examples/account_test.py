import json
import os
import time
from pathlib import Path
from typing import Callable, TypeVar

import ccxt

INST_ID = "ETH-USDT-SWAP"
SYMBOL = "ETH/USDT:USDT"
SIMULATED_FLAG = "1"
REQUEST_TIMEOUT_MS = 30000
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2.0
T = TypeVar("T")


def load_config() -> dict:
    base_dir = Path(__file__).resolve().parent
    local_cfg = base_dir / "config.local"
    cfg_path = local_cfg if local_cfg.exists() else base_dir / "config"
    with cfg_path.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["apiKey"] = os.getenv("OKX_API_KEY", cfg.get("apiKey", ""))
    cfg["secret"] = os.getenv("OKX_API_SECRET", cfg.get("secret", ""))
    cfg["password"] = os.getenv("OKX_API_PASSWORD", cfg.get("password", ""))
    return cfg


def build_okx_client(cfg: dict) -> ccxt.okx:
    okx = ccxt.okx(
        {
            "apiKey": cfg["apiKey"],
            "secret": cfg["secret"],
            "password": cfg["password"],
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
            "timeout": REQUEST_TIMEOUT_MS,
        }
    )
    proxy = cfg.get("proxy") or os.getenv("OKX_PROXY")
    if proxy:
        okx.httpProxy = proxy
    if str(cfg.get("flag", SIMULATED_FLAG)) == SIMULATED_FLAG:
        okx.set_sandbox_mode(True)
    return okx


def with_retry(label: str, func: Callable[[], T]) -> T:
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            print(f"{label} failed ({attempt}/{RETRY_ATTEMPTS}): {exc}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"{label} failed after retries: {last_error}")


def print_account_config(okx: ccxt.okx) -> None:
    row = with_retry("account_config", lambda: okx.private_get_account_config())["data"][0]
    print("账户配置:")
    print(
        {
            "uid": row.get("uid"),
            "acctLv": row.get("acctLv"),
            "posMode": row.get("posMode"),
            "greeksType": row.get("greeksType"),
            "level": row.get("level"),
            "autoLoan": row.get("autoLoan"),
            "perm": row.get("perm"),
        }
    )


def print_swap_balance(okx: ccxt.okx) -> None:
    balance = with_retry("swap_balance", lambda: okx.fetch_balance({"type": "swap"}))
    rows = (balance.get("info") or {}).get("data") or []
    row = rows[0] if rows else {}
    print("合约账户余额:")
    print(
        {
            "totalEq": row.get("totalEq"),
            "availEq": row.get("availEq"),
            "adjEq": row.get("adjEq"),
            "isoEq": row.get("isoEq"),
        }
    )


def print_positions(okx: ccxt.okx) -> None:
    positions = with_retry("swap_positions", lambda: okx.fetch_positions([SYMBOL], {"type": "swap"}))
    print(f"{INST_ID} 持仓:")
    if not positions:
        print("[]")
        return
    for position in positions:
        info = position.get("info") or {}
        print(
            {
                "symbol": position.get("symbol"),
                "side": position.get("side"),
                "contracts": position.get("contracts"),
                "entryPrice": position.get("entryPrice"),
                "marginMode": info.get("mgnMode"),
                "posSide": info.get("posSide"),
                "pos": info.get("pos"),
                "avgPx": info.get("avgPx"),
                "lever": info.get("lever"),
            }
        )


def main() -> None:
    cfg = load_config()
    okx = build_okx_client(cfg)
    print(
        {
            "proxy": cfg.get("proxy") or os.getenv("OKX_PROXY"),
            "timeout_ms": REQUEST_TIMEOUT_MS,
            "retry_attempts": RETRY_ATTEMPTS,
        }
    )
    print_account_config(okx)
    print_swap_balance(okx)
    print_positions(okx)


if __name__ == "__main__":
    main()
