import os
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from coinbase.rest import RESTClient

app = FastAPI(
    title="Quant Bot Backend",
    version="1.0.0",
    description="Coinbase market scanner and strategy API. Trading is disabled by default.",
)

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"

PUBLIC_BASE = "https://api.exchange.coinbase.com"
DEFAULT_HEADERS = {"User-Agent": "quant-bot-backend/1.0"}


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


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "name": "Quant Bot Backend",
        "status": "online",
        "trading_enabled": TRADING_ENABLED,
        "endpoints": [
            "/health",
            "/price/BTC-USD",
            "/signal/BTC-USD",
            "/scan?products=BTC-USD,ETH-USD,SOL-USD",
            "/coinbase-auth-check",
        ],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "coinbase_credentials_present": bool(COINBASE_API_KEY and COINBASE_API_SECRET),
        "trading_enabled": TRADING_ENABLED,
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
        "note": "Signal is informational and is not an order.",
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
        client = RESTClient(api_key=COINBASE_API_KEY, api_secret=COINBASE_API_SECRET)
        client.get_accounts(limit=1)
        return {
            "connected": True,
            "credentials_present": True,
            "trading_enabled": TRADING_ENABLED,
            "note": "Credentials authenticated successfully; no balances or secrets are exposed.",
        }
    except Exception as exc:
        return {
            "connected": False,
            "credentials_present": True,
            "trading_enabled": TRADING_ENABLED,
            "error_type": type(exc).__name__,
            "note": "Authentication failed. Check the Coinbase API key name/secret format and permissions.",
        }
