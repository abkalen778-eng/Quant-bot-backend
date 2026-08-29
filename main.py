import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from coinbase.rest import RESTClient

app = FastAPI(
    title="Quant Bot Backend",
    version="1.1.0",
    description="Coinbase market scanner with guarded Coinbase Advanced Trade execution support.",
)

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
KILL_SWITCH = os.getenv("KILL_SWITCH", "false").lower() == "true"
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "10"))

PUBLIC_BASE = "https://api.exchange.coinbase.com"
DEFAULT_HEADERS = {"User-Agent": "quant-bot-backend/1.1"}


class TradeRequest(BaseModel):
    product_id: str = Field(default="BTC-USD", examples=["BTC-USD"])
    side: Literal["BUY", "SELL"]
    size_usd: float = Field(gt=0)
    require_signal: bool = True


def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    try:
        response = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=12)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Coinbase market-data request failed: {exc}") from exc


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    recent = changes[-period:]
    gains = sum(max(c, 0.0) for c in recent) / period
    losses = sum(max(-c, 0.0) for c in recent) / period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _client() -> RESTClient:
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        raise HTTPException(status_code=503, detail="Coinbase credentials are not configured")
    return RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "Quant Bot Backend",
        "status": "online",
        "trading_enabled": TRADING_ENABLED,
        "dry_run": DRY_RUN,
        "kill_switch": KILL_SWITCH,
        "max_order_usd": MAX_ORDER_USD,
        "endpoints": [
            "/health",
            "/price/BTC-USD",
            "/signal/BTC-USD",
            "/scan?products=BTC-USD,ETH-USD,SOL-USD",
            "/coinbase-auth-check",
            "POST /trade",
        ],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "coinbase_credentials_present": bool(COINBASE_API_KEY and COINBASE_API_SECRET),
        "trading_enabled": TRADING_ENABLED,
        "dry_run": DRY_RUN,
        "kill_switch": KILL_SWITCH,
        "max_order_usd": MAX_ORDER_USD,
    }


@app.get("/price/{product_id}")
def price(product_id: str) -> dict[str, Any]:
    product = product_id.upper()
    ticker = _get_json(f"{PUBLIC_BASE}/products/{product}/ticker")
    return {
        "product": product,
        "price": float(ticker["price"]),
        "bid": float(ticker["bid"]),
        "ask": float(ticker["ask"]),
        "volume_24h": float(ticker["volume"]),
        "time": ticker.get("time"),
    }


def build_signal(product_id: str) -> dict[str, Any]:
    product = product_id.upper()
    candles = _get_json(
        f"{PUBLIC_BASE}/products/{product}/candles",
        params={"granularity": 3600},
    )
    if not isinstance(candles, list) or len(candles) < 55:
        raise HTTPException(status_code=502, detail=f"Not enough candle data for {product}")

    candles = sorted(candles, key=lambda row: row[0])
    closes = [float(row[4]) for row in candles]
    current = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    rsi14 = _rsi(closes, 14)

    score = 0
    reasons: list[str] = []

    if sma20 is not None and sma50 is not None:
        if sma20 > sma50:
            score += 1
            reasons.append("SMA20 is above SMA50")
        else:
            score -= 1
            reasons.append("SMA20 is below SMA50")

    if rsi14 is not None:
        if rsi14 < 35:
            score += 1
            reasons.append("RSI is near oversold")
        elif rsi14 > 70:
            score -= 1
            reasons.append("RSI is overbought")
        else:
            reasons.append("RSI is neutral")

    if sma20 is not None:
        if current > sma20:
            score += 1
            reasons.append("Price is above SMA20")
        else:
            score -= 1
            reasons.append("Price is below SMA20")

    if score >= 2:
        signal = "BULLISH"
    elif score <= -2:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    return {
        "product": product,
        "signal": signal,
        "score": score,
        "price": round(current, 8),
        "sma20": round(sma20, 8) if sma20 is not None else None,
        "sma50": round(sma50, 8) if sma50 is not None else None,
        "rsi14": round(rsi14, 2) if rsi14 is not None else None,
        "reasons": reasons,
        "timeframe": "1h",
        "trading_enabled": TRADING_ENABLED,
        "dry_run": DRY_RUN,
        "note": "Signal is informational unless an explicit POST /trade request is accepted.",
    }


@app.get("/signal/{product_id}")
def signal(product_id: str) -> dict[str, Any]:
    return build_signal(product_id)


@app.get("/scan")
def scan(products: str = "BTC-USD,ETH-USD,SOL-USD") -> dict[str, Any]:
    product_list = [p.strip().upper() for p in products.split(",") if p.strip()][:10]
    if not product_list:
        raise HTTPException(status_code=400, detail="Provide at least one product")

    results = []
    for product in product_list:
        try:
            results.append(build_signal(product))
        except HTTPException as exc:
            results.append({"product": product, "error": exc.detail})

    rank = {"BULLISH": 2, "NEUTRAL": 1, "BEARISH": 0}
    results.sort(key=lambda x: (rank.get(x.get("signal", ""), -1), x.get("score", -99)), reverse=True)
    return {"count": len(results), "results": results}


@app.get("/coinbase-auth-check")
def coinbase_auth_check() -> dict[str, Any]:
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return {"connected": False, "reason": "Coinbase environment variables are missing"}

    try:
        client = _client()
        client.get_accounts(limit=1)
        return {
            "connected": True,
            "credentials_present": True,
            "trading_enabled": TRADING_ENABLED,
            "dry_run": DRY_RUN,
            "kill_switch": KILL_SWITCH,
            "note": "Credentials authenticated successfully; no balances or secrets are exposed.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        return {
            "connected": False,
            "credentials_present": True,
            "trading_enabled": TRADING_ENABLED,
            "dry_run": DRY_RUN,
            "error_type": type(exc).__name__,
            "note": "Authentication failed. Check the Coinbase API key name/secret format and permissions.",
        }


@app.post("/trade")
def trade(request: TradeRequest) -> dict[str, Any]:
    product = request.product_id.strip().upper()
    side = request.side.upper()

    if KILL_SWITCH:
        raise HTTPException(status_code=423, detail="Trading kill switch is active")

    if request.size_usd > MAX_ORDER_USD:
        raise HTTPException(
            status_code=400,
            detail=f"Order exceeds MAX_ORDER_USD safety limit of ${MAX_ORDER_USD:.2f}",
        )

    signal_data = build_signal(product)
    required_signal = "BULLISH" if side == "BUY" else "BEARISH"
    if request.require_signal and signal_data["signal"] != required_signal:
        raise HTTPException(
            status_code=409,
            detail=f"Trade blocked: {side} requires {required_signal}, current signal is {signal_data['signal']}",
        )

    client_order_id = str(uuid.uuid4())
    current_price = float(signal_data["price"])
    estimated_base_size = request.size_usd / current_price

    preview = {
        "client_order_id": client_order_id,
        "product_id": product,
        "side": side,
        "size_usd": round(request.size_usd, 2),
        "estimated_base_size": round(estimated_base_size, 12),
        "reference_price": current_price,
        "signal": signal_data["signal"],
        "risk_limit_usd": MAX_ORDER_USD,
    }

    if DRY_RUN or not TRADING_ENABLED:
        return {
            "accepted": True,
            "executed": False,
            "mode": "DRY_RUN",
            "preview": preview,
            "note": "No Coinbase order was submitted.",
        }

    try:
        client = _client()
        if side == "BUY":
            result = client.market_order_buy(
                client_order_id=client_order_id,
                product_id=product,
                quote_size=f"{request.size_usd:.2f}",
            )
        else:
            result = client.market_order_sell(
                client_order_id=client_order_id,
                product_id=product,
                base_size=f"{estimated_base_size:.12f}",
            )

        response_data = result.to_dict() if hasattr(result, "to_dict") else str(result)
        return {
            "accepted": True,
            "executed": True,
            "mode": "LIVE",
            "order": response_data,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Coinbase Advanced Trade order submission failed: {type(exc).__name__}",
        ) from exc
