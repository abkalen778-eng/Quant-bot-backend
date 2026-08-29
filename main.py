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
from liquidity_strategy import build_liquidity_signal

app = FastAPI(title="Quant Bot Backend", version="2.0.0", description="Coinbase multi-timeframe liquidity strategy and guarded algorithmic engine.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
KILL_SWITCH = os.getenv("KILL_SWITCH", "false").lower() == "true"
AUTO_TRADING = os.getenv("AUTO_TRADING", "false").lower() == "true"
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "10"))
AUTO_ORDER_USD = min(float(os.getenv("AUTO_ORDER_USD", "5")), MAX_ORDER_USD)
SCAN_INTERVAL_SECONDS = max(int(os.getenv("SCAN_INTERVAL_SECONDS", "300")), 60)
AUTO_PRODUCTS = [p.strip().upper() for p in os.getenv("AUTO_PRODUCTS", "BTC-USD,ETH-USD,SOL-USD").split(",") if p.strip()][:10]
PUBLIC_BASE = "https://api.exchange.coinbase.com"
DEFAULT_HEADERS = {"User-Agent": "quant-bot-backend/2.0"}

bot_state: dict[str, Any] = {"running": False, "last_scan": None, "last_results": [], "dry_run_actions": [], "errors": []}
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
    if len(v) <= n: return None
    changes = [v[i] - v[i-1] for i in range(1, len(v))][-n:]
    gains = sum(max(x, 0.0) for x in changes) / n
    losses = sum(max(-x, 0.0) for x in changes) / n
    if losses == 0: return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)

def _client() -> RESTClient:
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise HTTPException(status_code=503, detail="Coinbase credentials are not configured")
    return RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)

def build_signal(product_id: str) -> dict[str, Any]:
    try:
        return build_liquidity_signal(product_id.strip().upper())
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Coinbase strategy-data request failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

def _execute(product: str, side: str, size_usd: float, require_signal: bool = True) -> dict[str, Any]:
    if KILL_SWITCH: raise HTTPException(status_code=423, detail="Trading kill switch is active")
    if size_usd > MAX_ORDER_USD: raise HTTPException(status_code=400, detail="Order exceeds MAX_ORDER_USD")
    sig = build_signal(product)
    required = "BULLISH" if side == "BUY" else "BEARISH"
    if require_signal and sig["signal"] != required:
        raise HTTPException(status_code=409, detail=f"Trade blocked: {side} requires {required}; current signal {sig['signal']}")
    oid, px = str(uuid.uuid4()), float(sig["price"])
    base = size_usd / px
    preview = {"client_order_id":oid,"product_id":product,"side":side,"size_usd":round(size_usd,2),"estimated_base_size":round(base,12),"reference_price":px,"signal":sig["signal"],"strategy":sig.get("strategy")}
    if DRY_RUN or not TRADING_ENABLED:
        return {"accepted":True,"executed":False,"mode":"DRY_RUN","preview":preview}
    client = _client()
    try:
        result = client.market_order_buy(client_order_id=oid, product_id=product, quote_size=f"{size_usd:.2f}") if side == "BUY" else client.market_order_sell(client_order_id=oid, product_id=product, base_size=f"{base:.12f}")
        return {"accepted":True,"executed":True,"mode":"LIVE","order":result.to_dict() if hasattr(result,"to_dict") else str(result)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Coinbase order failed: {type(exc).__name__}") from exc

def _algo_cycle() -> None:
    results, actions = [], []
    for product in AUTO_PRODUCTS:
        try:
            sig = build_signal(product)
            results.append(sig)
            if sig["signal"] == "BULLISH":
                actions.append(_execute(product,"BUY",AUTO_ORDER_USD,True))
            elif sig["signal"] == "BEARISH":
                actions.append({"product":product,"action":"BEARISH_SETUP","executed":False,"note":"Shorting/automatic sell execution remains disabled until position tracking and exit management are added."})
        except Exception as exc:
            results.append({"product":product,"error":type(exc).__name__})
    with state_lock:
        bot_state["last_scan"] = datetime.now(timezone.utc).isoformat()
        bot_state["last_results"] = results
        if actions: bot_state["dry_run_actions"] = (bot_state["dry_run_actions"] + actions)[-50:]

def _loop() -> None:
    with state_lock: bot_state["running"] = True
    while True:
        try: _algo_cycle()
        except Exception as exc:
            with state_lock: bot_state["errors"] = (bot_state["errors"] + [type(exc).__name__])[-20:]
        time.sleep(SCAN_INTERVAL_SECONDS)

@app.on_event("startup")
def start_algo() -> None:
    if AUTO_TRADING:
        threading.Thread(target=_loop, daemon=True, name="quant-algo-loop").start()

@app.get("/")
def root() -> dict[str, Any]:
    return {"name":"Quant Bot Backend","version":"2.0.0","strategy":"liquidity_sweep_mtf_v1","status":"online","auto_trading":AUTO_TRADING,"trading_enabled":TRADING_ENABLED,"dry_run":DRY_RUN,"kill_switch":KILL_SWITCH,"products":AUTO_PRODUCTS,"scan_interval_seconds":SCAN_INTERVAL_SECONDS,"max_order_usd":MAX_ORDER_USD,"auto_order_usd":AUTO_ORDER_USD}

@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok":True,"time":datetime.now(timezone.utc).isoformat(),"credentials_present":bool(COINBASE_API_KEY and COINBASE_API_SECRET),"auto_trading":AUTO_TRADING,"trading_enabled":TRADING_ENABLED,"dry_run":DRY_RUN,"kill_switch":KILL_SWITCH,"strategy":"liquidity_sweep_mtf_v1"}

@app.get("/price/{product_id}")
def price(product_id: str) -> dict[str, Any]:
    t = _get_json(f"{PUBLIC_BASE}/products/{product_id.upper()}/ticker")
    return {"product":product_id.upper(),"price":float(t["price"]),"bid":float(t["bid"]),"ask":float(t["ask"]),"volume_24h":float(t["volume"]),"time":t.get("time")}

@app.get("/signal/{product_id}")
def signal(product_id: str) -> dict[str, Any]: return build_signal(product_id)

@app.get("/scan")
def scan(products: str = "BTC-USD,ETH-USD,SOL-USD") -> dict[str, Any]:
    items=[]
    for p in [x.strip().upper() for x in products.split(",") if x.strip()][:10]:
        try: items.append(build_signal(p))
        except HTTPException as exc: items.append({"product":p,"error":exc.detail})
    return {"count":len(items),"strategy":"liquidity_sweep_mtf_v1","results":items}

@app.post("/algo/run-once")
def algo_run_once() -> dict[str, Any]:
    _algo_cycle()
    with state_lock: return dict(bot_state)

@app.get("/algo/status")
def algo_status() -> dict[str, Any]:
    with state_lock: return {**bot_state,"configured":AUTO_TRADING,"dry_run":DRY_RUN,"trading_enabled":TRADING_ENABLED,"kill_switch":KILL_SWITCH,"products":AUTO_PRODUCTS,"interval_seconds":SCAN_INTERVAL_SECONDS,"strategy":"liquidity_sweep_mtf_v1"}

@app.get("/coinbase-auth-check")
def auth_check() -> dict[str, Any]:
    try:
        _client().get_accounts(limit=1)
        return {"connected":True,"trading_enabled":TRADING_ENABLED,"dry_run":DRY_RUN}
    except Exception as exc:
        return {"connected":False,"error_type":type(exc).__name__}

@app.post("/trade")
def trade(request: TradeRequest) -> dict[str, Any]:
    return _execute(request.product_id.strip().upper(), request.side.upper(), request.size_usd, request.require_signal)
