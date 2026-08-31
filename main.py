import os
import uuid
import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from coinbase.rest import RESTClient
from liquidity_strategy import build_confirmed_breakout_signal
from tjr_strategy import build_tjr_core_signal

STRATEGY_NAME = "confirmed_breakout_v1+tjr_core_v1"
FEE_RATE = 0.004
SLIPPAGE_RATE = 0.001
app = FastAPI(title="Quant Bot Backend", version="3.1.0", description="Coinbase confirmed-breakout paper scanner with paper P/L tracking and guarded execution controls.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
KILL_SWITCH = os.getenv("KILL_SWITCH", "false").lower() == "true"
AUTO_TRADING = os.getenv("AUTO_TRADING", "false").lower() == "true"
TJR_ENABLED = os.getenv("TJR_ENABLED", "true").lower() == "true"
# TJR must pass paper verification and managed-live-exit verification before this is enabled.
TJR_LIVE_ENABLED = os.getenv("TJR_LIVE_ENABLED", "false").lower() == "true"
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "10"))
AUTO_ORDER_USD = min(float(os.getenv("AUTO_ORDER_USD", "5")), MAX_ORDER_USD)
SCAN_INTERVAL_SECONDS = max(int(os.getenv("SCAN_INTERVAL_SECONDS", "300")), 60)
AUTO_PRODUCTS = [p.strip().upper() for p in os.getenv("AUTO_PRODUCTS", "BTC-USD,ETH-USD,SOL-USD").split(",") if p.strip()][:20]
PUBLIC_BASE = "https://api.exchange.coinbase.com"
DEFAULT_HEADERS = {"User-Agent": "quant-bot-backend/3.1"}

bot_state: dict[str, Any] = {
    "running": False,
    "last_scan": None,
    "last_results": [],
    "dry_run_actions": [],
    "errors": [],
    "processed_signal_keys": [],
    "paper_positions": {},
    "paper_closed_trades": [],
}
state_lock = threading.Lock()

class TradeRequest(BaseModel):
    product_id: str = Field(default="BTC-USD")
    side: Literal["BUY", "SELL"]
    size_usd: float = Field(gt=0)
    require_signal: bool = True

def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    try:
        r = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=12)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Coinbase market-data request failed: {exc}") from exc

def _sma(v: list[float], n: int) -> float | None:
    return sum(v[-n:]) / n if len(v) >= n else None

def _rsi(v: list[float], n: int = 14) -> float | None:
    if len(v) <= n:
        return None
    changes = [v[i] - v[i-1] for i in range(1, len(v))][-n:]
    gains = sum(max(x, 0.0) for x in changes) / n
    losses = sum(max(-x, 0.0) for x in changes) / n
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)

def _client() -> RESTClient:
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise HTTPException(status_code=503, detail="Coinbase credentials are not configured")
    return RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)

def _ticker_price(product: str) -> float:
    return float(_get_json(f"{PUBLIC_BASE}/products/{product}/ticker")["price"])

def build_signal(product_id: str) -> dict[str, Any]:
    try:
        return build_confirmed_breakout_signal(product_id.strip().upper())
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Coinbase strategy-data request failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def build_strategy_signals(product_id: str) -> list[dict[str, Any]]:
    signals = [build_signal(product_id)]
    if TJR_ENABLED:
        try:
            signals.append(build_tjr_core_signal(product_id.strip().upper()))
        except requests.RequestException as exc:
            signals.append({
                "product": product_id.strip().upper(),
                "strategy": "tjr_core_v1",
                "signal": "NO_TRADE",
                "trade_ready": False,
                "error": f"Coinbase strategy-data request failed: {type(exc).__name__}",
            })
        except ValueError as exc:
            signals.append({
                "product": product_id.strip().upper(),
                "strategy": "tjr_core_v1",
                "signal": "NO_TRADE",
                "trade_ready": False,
                "error": str(exc),
            })
    return signals

def _execute(product: str, side: str, size_usd: float, require_signal: bool = True) -> dict[str, Any]:
    if KILL_SWITCH:
        raise HTTPException(status_code=423, detail="Trading kill switch is active")
    if size_usd > MAX_ORDER_USD:
        raise HTTPException(status_code=400, detail="Order exceeds MAX_ORDER_USD")
    sig = build_signal(product)
    required = "BULLISH" if side == "BUY" else "BEARISH"
    if require_signal and sig["signal"] != required:
        raise HTTPException(status_code=409, detail=f"Trade blocked: {side} requires {required}; current signal {sig['signal']}")
    oid, px = str(uuid.uuid4()), float(sig["price"])
    base = size_usd / px
    preview = {"client_order_id": oid, "product_id": product, "side": side, "size_usd": round(size_usd, 2), "estimated_base_size": round(base, 12), "reference_price": px, "signal": sig["signal"], "signal_key": sig.get("signal_key"), "strategy": sig.get("strategy"), "stop_loss": sig.get("stop_loss"), "exit_plan": sig.get("exit_plan")}
    if DRY_RUN or not TRADING_ENABLED:
        return {"accepted": True, "executed": False, "mode": "DRY_RUN", "preview": preview}
    client = _client()
    try:
        result = client.market_order_buy(client_order_id=oid, product_id=product, quote_size=f"{size_usd:.2f}") if side == "BUY" else client.market_order_sell(client_order_id=oid, product_id=product, base_size=f"{base:.12f}")
        return {"accepted": True, "executed": True, "mode": "LIVE", "order": result.to_dict() if hasattr(result, "to_dict") else str(result)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Coinbase order failed: {type(exc).__name__}") from exc

def _open_paper_position(sig: dict[str, Any], notional: float) -> dict[str, Any]:
    product = sig["product"]
    entry = float(sig["price"]) * (1.0 + SLIPPAGE_RATE)
    stop = float(sig["stop_loss"]["price"])
    now = datetime.now(timezone.utc)
    pos = {
        "product": product,
        "signal_key": sig["signal_key"],
        "entry_time": now.isoformat(),
        "entry_timestamp": now.timestamp(),
        "entry_price": entry,
        "notional_usd": float(notional),
        "initial_stop": stop,
        "active_stop": stop,
        "peak_price": entry,
        "trail_active": False,
        "status": "OPEN",
        "strategy": STRATEGY_NAME,
        "take_profit": (sig.get("take_profit") or {}).get("price"),
    }
    pos["strategy"] = sig.get("strategy", STRATEGY_NAME)
    bot_state["paper_positions"][product] = pos
    return pos

def _update_paper_positions() -> None:
    now = datetime.now(timezone.utc)
    closed = []
    with state_lock:
        positions = [dict(v) for v in bot_state["paper_positions"].values()]
    for pos in positions:
        product = pos["product"]
        try:
            price = _ticker_price(product)
        except Exception:
            continue
        entry = float(pos["entry_price"])
        peak = max(float(pos["peak_price"]), price)
        stop = float(pos["active_stop"])
        trail_active = bool(pos["trail_active"])
        if peak >= entry * 1.12:
            trail_active = True
        if trail_active:
            stop = max(stop, peak * 0.92)
        held_days = max(0.0, (now.timestamp() - float(pos["entry_timestamp"])) / 86400.0)
        reason = None
        exit_price = None
        take_profit = pos.get("take_profit")
        if price <= stop:
            exit_price = min(price, stop) * (1.0 - SLIPPAGE_RATE)
            reason = "stop_or_trail"
        elif take_profit is not None and price >= float(take_profit):
            exit_price = max(price, float(take_profit)) * (1.0 - SLIPPAGE_RATE)
            reason = "liquidity_target"
        elif held_days >= 45.0:
            exit_price = price * (1.0 - SLIPPAGE_RATE)
            reason = "45d_time_stop"
        with state_lock:
            if product in bot_state["paper_positions"]:
                bot_state["paper_positions"][product]["peak_price"] = peak
                bot_state["paper_positions"][product]["active_stop"] = stop
                bot_state["paper_positions"][product]["trail_active"] = trail_active
                bot_state["paper_positions"][product]["last_price"] = price
                bot_state["paper_positions"][product]["unrealized_return_pct"] = round((price / entry - 1.0) * 100.0, 4)
        if reason and exit_price is not None:
            gross = exit_price / entry - 1.0
            net = gross - 2.0 * FEE_RATE
            pnl = float(pos["notional_usd"]) * net
            trade = {**pos, "status": "CLOSED", "exit_time": now.isoformat(), "exit_price": exit_price, "exit_reason": reason, "gross_return_pct": round(gross * 100.0, 4), "net_return_pct": round(net * 100.0, 4), "pnl_usd": round(pnl, 4), "result": "WIN" if net > 0 else "LOSS"}
            closed.append((product, trade))
    if closed:
        with state_lock:
            for product, trade in closed:
                bot_state["paper_positions"].pop(product, None)
                bot_state["paper_closed_trades"] = (bot_state["paper_closed_trades"] + [trade])[-500:]

def _paper_stats() -> dict[str, Any]:
    with state_lock:
        closed = list(bot_state["paper_closed_trades"])
        open_positions = list(bot_state["paper_positions"].values())
    wins = sum(1 for x in closed if x.get("result") == "WIN")
    losses = sum(1 for x in closed if x.get("result") == "LOSS")
    total = len(closed)
    realized = sum(float(x.get("pnl_usd", 0.0)) for x in closed)
    unrealized = 0.0
    for p in open_positions:
        if "last_price" in p:
            unrealized += float(p["notional_usd"]) * (float(p["last_price"]) / float(p["entry_price"]) - 1.0)
    return {"closed_trades": total, "wins": wins, "losses": losses, "win_rate_pct": round(100.0 * wins / total, 2) if total else 0.0, "open_positions": len(open_positions), "realized_pnl_usd": round(realized, 4), "unrealized_pnl_usd_before_exit_fees": round(unrealized, 4), "total_paper_pnl_usd_approx": round(realized + unrealized, 4)}

def _algo_cycle() -> None:
    _update_paper_positions()
    results, actions = [], []
    with state_lock:
        processed = set(bot_state.get("processed_signal_keys", []))
        open_products = set(bot_state["paper_positions"].keys())
    for product in AUTO_PRODUCTS:
        try:
            product_signals = build_strategy_signals(product)
            results.extend(product_signals)
            for sig in product_signals:
                key = sig.get("signal_key")
                strategy = sig.get("strategy")
                if sig.get("signal") != "BULLISH" or not key:
                    continue
                if product in open_products:
                    actions.append({"product": product, "strategy": strategy, "action": "OPEN_POSITION_EXISTS", "executed": False, "signal_key": key})
                elif key in processed:
                    actions.append({"product": product, "strategy": strategy, "action": "DUPLICATE_SIGNAL_SKIPPED", "executed": False, "signal_key": key})
                else:
                    if strategy == "tjr_core_v1" and not TJR_LIVE_ENABLED:
                        action = {
                            "accepted": True,
                            "executed": False,
                            "mode": "PAPER_TEST",
                            "strategy": strategy,
                            "preview": {
                                "product_id": product,
                                "side": "BUY",
                                "size_usd": AUTO_ORDER_USD,
                                "reference_price": sig.get("price"),
                                "signal_key": key,
                                "stop_loss": sig.get("stop_loss"),
                                "take_profit": sig.get("take_profit"),
                            },
                        }
                    else:
                        action = _execute(product, "BUY", AUTO_ORDER_USD, True)
                    with state_lock:
                        paper = _open_paper_position(sig, AUTO_ORDER_USD)
                    action["paper_position"] = paper
                    actions.append(action)
                    processed.add(key)
                    open_products.add(product)
        except Exception as exc:
            results.append({"product": product, "error": type(exc).__name__})
    with state_lock:
        bot_state["last_scan"] = datetime.now(timezone.utc).isoformat()
        bot_state["last_results"] = results
        bot_state["processed_signal_keys"] = list(processed)[-200:]
        if actions:
            bot_state["dry_run_actions"] = (bot_state["dry_run_actions"] + actions)[-100:]

def _loop() -> None:
    with state_lock:
        bot_state["running"] = True
    while True:
        try:
            _algo_cycle()
        except Exception as exc:
            with state_lock:
                bot_state["errors"] = (bot_state["errors"] + [type(exc).__name__])[-20:]
        time.sleep(SCAN_INTERVAL_SECONDS)

@app.on_event("startup")
def start_algo() -> None:
    if AUTO_TRADING:
        threading.Thread(target=_loop, daemon=True, name="quant-confirmed-breakout-loop").start()

@app.get("/")
def root() -> dict[str, Any]:
    return {"name": "Quant Bot Backend", "version": "3.2.0", "strategy": STRATEGY_NAME, "status": "online", "auto_trading": AUTO_TRADING, "trading_enabled": TRADING_ENABLED, "dry_run": DRY_RUN, "kill_switch": KILL_SWITCH, "tjr_enabled": TJR_ENABLED, "tjr_live_enabled": TJR_LIVE_ENABLED, "products": AUTO_PRODUCTS, "scan_interval_seconds": SCAN_INTERVAL_SECONDS, "max_order_usd": MAX_ORDER_USD, "auto_order_usd": AUTO_ORDER_USD}

@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat(), "credentials_present": bool(COINBASE_API_KEY and COINBASE_API_SECRET), "auto_trading": AUTO_TRADING, "trading_enabled": TRADING_ENABLED, "dry_run": DRY_RUN, "kill_switch": KILL_SWITCH, "tjr_enabled": TJR_ENABLED, "tjr_live_enabled": TJR_LIVE_ENABLED, "strategy": STRATEGY_NAME}

@app.get("/price/{product_id}")
def price(product_id: str) -> dict[str, Any]:
    t = _get_json(f"{PUBLIC_BASE}/products/{product_id.upper()}/ticker")
    return {"product": product_id.upper(), "price": float(t["price"]), "bid": float(t["bid"]), "ask": float(t["ask"]), "volume_24h": float(t["volume"]), "time": t.get("time")}

@app.get("/signal/{product_id}")
def signal(product_id: str) -> dict[str, Any]:
    return build_signal(product_id)

@app.get("/scan")
def scan(products: str = "BTC-USD,ETH-USD,SOL-USD") -> dict[str, Any]:
    items = []
    for p in [x.strip().upper() for x in products.split(",") if x.strip()][:20]:
        try:
            items.extend(build_strategy_signals(p))
        except HTTPException as exc:
            items.append({"product": p, "error": exc.detail})
    return {"count": len(items), "strategy": STRATEGY_NAME, "results": items}

@app.post("/algo/run-once")
def algo_run_once() -> dict[str, Any]:
    _algo_cycle()
    with state_lock:
        return dict(bot_state)

@app.get("/algo/status")
def algo_status() -> dict[str, Any]:
    with state_lock:
        snapshot = {**bot_state, "configured": AUTO_TRADING, "dry_run": DRY_RUN, "trading_enabled": TRADING_ENABLED, "kill_switch": KILL_SWITCH, "products": AUTO_PRODUCTS, "interval_seconds": SCAN_INTERVAL_SECONDS, "strategy": STRATEGY_NAME}
    snapshot["paper_stats"] = _paper_stats()
    return snapshot

@app.get("/paper/stats")
def paper_stats() -> dict[str, Any]:
    _update_paper_positions()
    return _paper_stats()

@app.get("/paper/trades")
def paper_trades() -> dict[str, Any]:
    _update_paper_positions()
    with state_lock:
        return {"stats": _paper_stats(), "open_positions": list(bot_state["paper_positions"].values()), "closed_trades": list(bot_state["paper_closed_trades"])}

@app.get("/coinbase-auth-check")
def auth_check() -> dict[str, Any]:
    try:
        _client().get_accounts(limit=1)
        return {"connected": True, "trading_enabled": TRADING_ENABLED, "dry_run": DRY_RUN}
    except Exception as exc:
        return {"connected": False, "error_type": type(exc).__name__}

@app.post("/trade")
def trade(request: TradeRequest) -> dict[str, Any]:
    return _execute(request.product_id.strip().upper(), request.side.upper(), request.size_usd, request.require_signal)
