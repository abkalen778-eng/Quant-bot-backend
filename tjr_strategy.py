from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

PUBLIC_BASE = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "quant-bot-tjr-core/1.0"}
ALLOWED_PRODUCTS = {
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD",
    "AVAX-USD", "LINK-USD", "LTC-USD", "BCH-USD", "DOT-USD", "UNI-USD",
    "AAVE-USD", "ATOM-USD", "NEAR-USD", "ICP-USD", "FIL-USD", "ARB-USD",
    "OP-USD", "SUI-USD",
}


def _candles(product: str, granularity: int, limit: int = 300) -> list[dict[str, float]]:
    response = requests.get(
        f"{PUBLIC_BASE}/products/{product}/candles",
        params={"granularity": granularity, "limit": limit},
        headers=HEADERS,
        timeout=12,
    )
    response.raise_for_status()
    return [
        {
            "time": float(row[0]),
            "low": float(row[1]),
            "high": float(row[2]),
            "open": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in sorted(response.json(), key=lambda item: item[0])
    ]


def _completed(rows: list[dict[str, float]], granularity: int, now: float) -> list[dict[str, float]]:
    current_start = int(now // granularity) * granularity
    return [row for row in rows if row["time"] < current_start]


def _pivot_highs(rows: list[dict[str, float]], width: int = 2) -> list[tuple[int, float]]:
    pivots: list[tuple[int, float]] = []
    for index in range(width, len(rows) - width):
        value = rows[index]["high"]
        if all(value > rows[j]["high"] for j in range(index - width, index)) and all(
            value >= rows[j]["high"] for j in range(index + 1, index + width + 1)
        ):
            pivots.append((index, value))
    return pivots


def _pivot_lows(rows: list[dict[str, float]], width: int = 2) -> list[tuple[int, float]]:
    pivots: list[tuple[int, float]] = []
    for index in range(width, len(rows) - width):
        value = rows[index]["low"]
        if all(value < rows[j]["low"] for j in range(index - width, index)) and all(
            value <= rows[j]["low"] for j in range(index + 1, index + width + 1)
        ):
            pivots.append((index, value))
    return pivots


def _bullish_ifvg_level(rows: list[dict[str, float]], sweep_index: int) -> float | None:
    """Return the upper edge of the nearest bearish FVG preceding the sweep."""
    for index in range(sweep_index, max(1, sweep_index - 8), -1):
        if index < 2:
            break
        if rows[index]["high"] < rows[index - 2]["low"]:
            return rows[index - 2]["low"]
    return None


def evaluate_tjr_core(
    product: str,
    daily: list[dict[str, float]],
    five_minute: list[dict[str, float]],
    one_minute: list[dict[str, float]],
    ticker_price: float,
    now: float,
) -> dict[str, Any]:
    """Evaluate the user's conservative, spot-long TJR Core sequence.

    Significant liquidity is the prior completed UTC session high/low. A long entry
    requires a low sweep, a 5-minute BOS or bullish IFVG inversion, then a 1-minute
    countertrend BOS followed by a fresh bullish BOS. Shorts are intentionally absent.
    """
    product = product.strip().upper()
    base = {
        "product": product,
        "signal": "NO_TRADE",
        "trade_ready": False,
        "strategy": "tjr_core_v1",
        "direction": None,
    }
    if product not in ALLOWED_PRODUCTS:
        return {**base, "reasons": ["Product is outside the approved spot universe"]}

    daily = _completed(daily, 86400, now)
    five = _completed(five_minute, 300, now)
    one = _completed(one_minute, 60, now)
    if len(daily) < 2 or len(five) < 30 or len(one) < 30:
        return {**base, "reasons": ["Insufficient completed candle history"]}

    prior_session = daily[-1]
    session_low = prior_session["low"]
    session_high = prior_session["high"]

    sweep_index: int | None = None
    for index in range(max(2, len(five) - 36), len(five)):
        candle = five[index]
        if candle["low"] < session_low and candle["close"] > session_low:
            sweep_index = index
    if sweep_index is None:
        return {
            **base,
            "liquidity": {"prior_session_low": session_low, "prior_session_high": session_high},
            "reasons": ["No completed 5-minute sweep and reclaim of the prior session low"],
        }

    prior_pivots = [(i, price) for i, price in _pivot_highs(five[: sweep_index + 1]) if i < sweep_index]
    bos_level = prior_pivots[-1][1] if prior_pivots else None
    ifvg_level = _bullish_ifvg_level(five, sweep_index)
    confirm_index: int | None = None
    confirmation = None
    for index in range(sweep_index + 1, len(five)):
        close = five[index]["close"]
        if bos_level is not None and close > bos_level:
            confirm_index, confirmation = index, "5m_bos"
            break
        if ifvg_level is not None and close > ifvg_level:
            confirm_index, confirmation = index, "5m_inverse_fvg"
            break
    if confirm_index is None:
        return {
            **base,
            "liquidity": {"prior_session_low": session_low, "prior_session_high": session_high},
            "sweep_time": int(five[sweep_index]["time"]),
            "reasons": ["Liquidity sweep found, but no completed 5-minute BOS or inverse-FVG confirmation"],
        }

    confirm_time = five[confirm_index]["time"] + 300
    one_after = [row for row in one if row["time"] >= confirm_time]
    if len(one_after) < 7:
        return {
            **base,
            "sweep_time": int(five[sweep_index]["time"]),
            "confirmation": confirmation,
            "confirmation_time": int(five[confirm_index]["time"]),
            "reasons": ["Waiting for completed 1-minute retracement and continuation structure"],
        }

    pullback_index: int | None = None
    continuation_index: int | None = None
    for index in range(5, len(one_after)):
        history = one_after[:index]
        lows = _pivot_lows(history)
        if pullback_index is None and lows and one_after[index]["close"] < lows[-1][1]:
            pullback_index = index
            continue
        if pullback_index is not None and index > pullback_index:
            highs = _pivot_highs(one_after[:index])
            eligible = [item for item in highs if item[0] >= pullback_index]
            if eligible and one_after[index]["close"] > eligible[-1][1]:
                continuation_index = index
                break

    if pullback_index is None or continuation_index is None:
        return {
            **base,
            "sweep_time": int(five[sweep_index]["time"]),
            "confirmation": confirmation,
            "confirmation_time": int(five[confirm_index]["time"]),
            "reasons": ["Waiting for 1-minute countertrend BOS followed by bullish BOS"],
        }

    continuation = one_after[continuation_index]
    signal_age = now - (continuation["time"] + 60)
    if signal_age < 0 or signal_age > 600:
        return {
            **base,
            "sweep_time": int(five[sweep_index]["time"]),
            "confirmation": confirmation,
            "confirmation_time": int(five[confirm_index]["time"]),
            "continuation_time": int(continuation["time"]),
            "reasons": ["Completed TJR sequence is stale; entry window is limited to 10 minutes"],
        }

    sweep_low = five[sweep_index]["low"]
    stop = sweep_low * 0.999
    target = session_high
    if not (stop < ticker_price < target):
        return {
            **base,
            "sweep_time": int(five[sweep_index]["time"]),
            "confirmation": confirmation,
            "continuation_time": int(continuation["time"]),
            "reasons": ["No valid protected long entry between the sweep stop and next session liquidity target"],
        }

    signal_key = f"{product}:{int(five[sweep_index]['time'])}:{int(continuation['time'])}:tjr_core"
    return {
        **base,
        "signal": "BULLISH",
        "trade_ready": True,
        "direction": "BULLISH",
        "price": ticker_price,
        "signal_key": signal_key,
        "sweep_time": int(five[sweep_index]["time"]),
        "sweep_level": session_low,
        "sweep_low": sweep_low,
        "confirmation": confirmation,
        "confirmation_time": int(five[confirm_index]["time"]),
        "continuation_time": int(continuation["time"]),
        "stop_loss": {"price": stop, "reason": "below_swept_liquidity"},
        "take_profit": {"price": target, "reason": "prior_session_high_liquidity"},
        "exit_plan": {"stop_price": stop, "target_price": target},
        "liquidity": {"prior_session_low": session_low, "prior_session_high": session_high},
        "reasons": [
            "Prior session low swept and reclaimed",
            f"Reversal confirmed by {confirmation}",
            "1-minute countertrend BOS and bullish continuation BOS completed",
        ],
    }


def build_tjr_core_signal(product: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).timestamp()
    ticker = requests.get(
        f"{PUBLIC_BASE}/products/{product.strip().upper()}/ticker",
        headers=HEADERS,
        timeout=12,
    )
    ticker.raise_for_status()
    return evaluate_tjr_core(
        product,
        _candles(product.strip().upper(), 86400, 10),
        _candles(product.strip().upper(), 300, 300),
        _candles(product.strip().upper(), 60, 300),
        float(ticker.json()["price"]),
        now,
    )
