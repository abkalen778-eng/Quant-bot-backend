from __future__ import annotations
from typing import Any
from datetime import datetime, timezone
import requests

PUBLIC_BASE = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "quant-bot-confirmed-breakout/1.0"}
ALLOWED_PRODUCTS = {
    "BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD","ADA-USD","AVAX-USD","LINK-USD",
    "LTC-USD","BCH-USD","DOT-USD","UNI-USD","AAVE-USD","ATOM-USD","NEAR-USD","ICP-USD",
    "FIL-USD","ARB-USD","OP-USD","SUI-USD"
}
VOLUME_MULTIPLE = 2.0
BREAKOUT_CONFIRM_PCT = 0.01
CONSOLIDATION_MAX_PCT = 0.08
NEAR_RESISTANCE_MAX_PCT = 0.08
RISK_PER_TRADE_PCT = 0.5


def _candles(product: str, granularity: int) -> list[dict[str, float]]:
    r = requests.get(
        f"{PUBLIC_BASE}/products/{product.upper()}/candles",
        params={"granularity": granularity},
        headers=HEADERS,
        timeout=12,
    )
    r.raise_for_status()
    return [
        {"time": float(x[0]), "low": float(x[1]), "high": float(x[2]), "open": float(x[3]), "close": float(x[4]), "volume": float(x[5])}
        for x in sorted(r.json(), key=lambda x: x[0])
    ]


def _ticker(product: str) -> dict[str, Any]:
    r = requests.get(f"{PUBLIC_BASE}/products/{product.upper()}/ticker", headers=HEADERS, timeout=12)
    r.raise_for_status()
    return r.json()


def _completed_daily(daily: list[dict[str, float]]) -> list[dict[str, float]]:
    now = datetime.now(timezone.utc).timestamp()
    current_day_start = int(now // 86400) * 86400
    return [c for c in daily if c["time"] < current_day_start]


def build_confirmed_breakout_signal(product: str) -> dict[str, Any]:
    product = product.strip().upper()
    if product not in ALLOWED_PRODUCTS:
        return {
            "product": product,
            "signal": "NO_TRADE",
            "trade_ready": False,
            "reasons": ["Product is outside the approved paper-scan universe"],
            "strategy": "confirmed_breakout_v1",
        }

    daily = _completed_daily(_candles(product, 86400))
    if len(daily) < 41:
        raise ValueError(f"Not enough completed daily market data for {product}")

    c = daily[-1]
    prior40 = daily[-41:-1]
    prior10 = daily[-11:-1]
    pre5 = daily[-6:-1]

    resistance = max(x["high"] for x in prior40)
    avg10 = sum(x["volume"] for x in prior10) / 10
    volume_ratio = c["volume"] / avg10 if avg10 else 0.0
    pre_high = max(x["high"] for x in pre5)
    pre_low = min(x["low"] for x in pre5)
    consolidation_pct = (pre_high - pre_low) / max(pre_low, 1e-12)
    distance_to_resistance = (resistance - pre5[-1]["close"]) / resistance
    breakout_pct = c["close"] / resistance - 1.0

    volume_ok = volume_ratio >= VOLUME_MULTIPLE
    consolidation_ok = consolidation_pct <= CONSOLIDATION_MAX_PCT
    near_resistance_ok = distance_to_resistance <= NEAR_RESISTANCE_MAX_PCT
    breakout_ok = breakout_pct >= BREAKOUT_CONFIRM_PCT
    ready = volume_ok and consolidation_ok and near_resistance_ok and breakout_ok

    ticker = _ticker(product)
    price = float(ticker["price"])
    initial_stop = max(price * 0.92, resistance * 0.975) if ready else None
    risk_distance_pct = ((price - initial_stop) / price * 100.0) if ready and initial_stop and price > initial_stop else None
    signal_key = f"{product}:{int(c['time'])}:confirmed_breakout" if ready else None

    reasons = [
        f"completed daily breakout: {breakout_pct*100:.2f}% (need >= {BREAKOUT_CONFIRM_PCT*100:.2f}%)",
        f"volume ratio: {volume_ratio:.2f}x (need >= {VOLUME_MULTIPLE:.2f}x)",
        f"5-day consolidation: {consolidation_pct*100:.2f}% (need <= {CONSOLIDATION_MAX_PCT*100:.2f}%)",
        f"prior close distance to resistance: {distance_to_resistance*100:.2f}% (need <= {NEAR_RESISTANCE_MAX_PCT*100:.2f}%)",
    ]

    return {
        "product": product,
        "signal": "BULLISH" if ready else "NO_TRADE",
        "direction": "BULLISH" if ready else None,
        "trade_ready": ready,
        "price": price,
        "signal_key": signal_key,
        "signal_candle_time": int(c["time"]),
        "signal_close": c["close"],
        "resistance": resistance,
        "breakout_pct": round(breakout_pct * 100, 4),
        "volume_ratio": round(volume_ratio, 4),
        "consolidation_pct": round(consolidation_pct * 100, 4),
        "distance_to_resistance_pct": round(distance_to_resistance * 100, 4),
        "stop_loss": {"price": initial_stop, "planned_risk_pct": RISK_PER_TRADE_PCT} if ready else None,
        "exit_plan": {"trail_activation_gain_pct": 12.0, "trail_distance_pct": 8.0, "max_hold_days": 45} if ready else None,
        "filters": {
            "volume_2x": volume_ok,
            "breakout_confirmed_1pct": breakout_ok,
            "consolidation_8pct": consolidation_ok,
            "near_resistance_8pct": near_resistance_ok,
        },
        "reasons": reasons,
        "strategy": "confirmed_breakout_v1",
        "mode_note": "Signal engine only; execution safety is controlled by main.py environment flags.",
    }


# Backward-compatible alias for older imports.
def build_liquidity_signal(product: str) -> dict[str, Any]:
    return build_confirmed_breakout_signal(product)
