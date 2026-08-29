from __future__ import annotations

from typing import Any
import requests

PUBLIC_BASE = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "quant-bot-liquidity-strategy/2.0"}


def _candles(product: str, granularity: int) -> list[dict[str, float]]:
    r = requests.get(
        f"{PUBLIC_BASE}/products/{product.upper()}/candles",
        params={"granularity": granularity},
        headers=HEADERS,
        timeout=12,
    )
    r.raise_for_status()
    raw = r.json()
    rows = sorted(raw, key=lambda x: x[0])
    return [
        {"time": float(x[0]), "low": float(x[1]), "high": float(x[2]), "open": float(x[3]), "close": float(x[4]), "volume": float(x[5])}
        for x in rows
    ]


def _aggregate_4h(hourly: list[dict[str, float]]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    bucket: list[dict[str, float]] = []
    bucket_id = None
    for c in hourly:
        current = int(c["time"] // 14400)
        if bucket_id is None:
            bucket_id = current
        if current != bucket_id and bucket:
            out.append({
                "time": bucket[0]["time"],
                "open": bucket[0]["open"],
                "high": max(x["high"] for x in bucket),
                "low": min(x["low"] for x in bucket),
                "close": bucket[-1]["close"],
                "volume": sum(x["volume"] for x in bucket),
            })
            bucket = []
            bucket_id = current
        bucket.append(c)
    if bucket:
        out.append({
            "time": bucket[0]["time"],
            "open": bucket[0]["open"],
            "high": max(x["high"] for x in bucket),
            "low": min(x["low"] for x in bucket),
            "close": bucket[-1]["close"],
            "volume": sum(x["volume"] for x in bucket),
        })
    return out


def _range(candles: list[dict[str, float]], lookback: int) -> tuple[float, float]:
    sample = candles[-lookback - 1:-1]
    return max(x["high"] for x in sample), min(x["low"] for x in sample)


def _latest_sweep(five: list[dict[str, float]], lookback: int = 20, search: int = 8) -> dict[str, Any] | None:
    start = max(lookback, len(five) - search)
    for i in range(len(five) - 1, start - 1, -1):
        prev = five[i - lookback:i]
        hi = max(x["high"] for x in prev)
        lo = min(x["low"] for x in prev)
        c = five[i]
        # Sweep above liquidity and close back below = bearish manipulation.
        if c["high"] > hi and c["close"] < hi:
            return {"direction": "BEARISH", "index": i, "level": hi, "sweep_price": c["high"], "time": int(c["time"])}
        # Sweep below liquidity and close back above = bullish manipulation.
        if c["low"] < lo and c["close"] > lo:
            return {"direction": "BULLISH", "index": i, "level": lo, "sweep_price": c["low"], "time": int(c["time"])}
    return None


def _mss_after_sweep(five: list[dict[str, float]], sweep: dict[str, Any]) -> bool:
    i = int(sweep["index"])
    if i < 4:
        return False
    pre = five[max(0, i - 4):i]
    post = five[i:]
    if sweep["direction"] == "BULLISH":
        structural_high = max(x["high"] for x in pre)
        return any(x["close"] > structural_high for x in post)
    structural_low = min(x["low"] for x in pre)
    return any(x["close"] < structural_low for x in post)


def _recent_fvg(five: list[dict[str, float]], direction: str, lookback: int = 12) -> dict[str, float] | None:
    start = max(2, len(five) - lookback)
    for i in range(len(five) - 1, start - 1, -1):
        a, c = five[i - 2], five[i]
        if direction == "BULLISH" and a["high"] < c["low"]:
            return {"low": a["high"], "high": c["low"], "mid": (a["high"] + c["low"]) / 2.0}
        if direction == "BEARISH" and a["low"] > c["high"]:
            return {"low": c["high"], "high": a["low"], "mid": (c["high"] + a["low"]) / 2.0}
    return None


def _pullback_confluence(five: list[dict[str, float]], direction: str, fvg: dict[str, float] | None) -> tuple[bool, str]:
    current = five[-1]
    recent = five[-12:]
    dealing_high = max(x["high"] for x in recent)
    dealing_low = min(x["low"] for x in recent)
    eq = (dealing_high + dealing_low) / 2.0
    fvg_touch = bool(fvg and current["low"] <= fvg["high"] and current["high"] >= fvg["low"])
    eq_touch = current["low"] <= eq <= current["high"]
    if direction == "BULLISH":
        directional_eq = current["low"] <= eq
    else:
        directional_eq = current["high"] >= eq
    if fvg_touch:
        return True, "5m fair-value-gap pullback"
    if eq_touch or directional_eq:
        return True, "5m equilibrium pullback"
    return False, "no 5m pullback confluence"


def _one_minute_bos(one: list[dict[str, float]], direction: str, lookback: int = 5) -> tuple[bool, float]:
    if len(one) < lookback + 2:
        return False, 0.0
    previous = one[-lookback - 1:-1]
    current = one[-1]
    if direction == "BULLISH":
        level = max(x["high"] for x in previous)
        return current["close"] > level, level
    level = min(x["low"] for x in previous)
    return current["close"] < level, level


def build_liquidity_signal(product: str) -> dict[str, Any]:
    product = product.upper()
    one = _candles(product, 60)
    five = _candles(product, 300)
    hourly = _candles(product, 3600)
    four = _aggregate_4h(hourly)
    if len(one) < 30 or len(five) < 40 or len(hourly) < 30 or len(four) < 6:
        raise ValueError(f"Not enough market data for {product}")

    h1_hi, h1_lo = _range(hourly, 20)
    h4_hi, h4_lo = _range(four, min(10, len(four) - 1))
    current = one[-1]["close"]

    sweep = _latest_sweep(five)
    reasons: list[str] = []
    if not sweep:
        return {
            "product": product,
            "signal": "NO_TRADE",
            "direction": None,
            "price": current,
            "setup_score": 0,
            "reasons": ["No recent 5m liquidity sweep"],
            "levels": {"1h_high": h1_hi, "1h_low": h1_lo, "4h_high": h4_hi, "4h_low": h4_lo},
            "strategy": "liquidity_sweep_mtf_v1",
        }

    direction = sweep["direction"]
    reasons.append(f"5m {direction.lower()} liquidity sweep confirmed")

    # Require the sweep to occur near a meaningful 1h or 4h extreme (within 0.75%).
    target_levels = [h1_lo, h4_lo] if direction == "BULLISH" else [h1_hi, h4_hi]
    sweep_px = float(sweep["sweep_price"])
    proximity = min(abs(sweep_px - x) / x for x in target_levels)
    htf_ok = proximity <= 0.0075
    if htf_ok:
        reasons.append("Sweep occurred near 1h/4h liquidity")
    else:
        reasons.append("Sweep was not close enough to 1h/4h liquidity")

    mss = _mss_after_sweep(five, sweep)
    reasons.append("5m market-structure shift confirmed" if mss else "No 5m market-structure shift yet")

    fvg = _recent_fvg(five, direction)
    pullback, pullback_reason = _pullback_confluence(five, direction, fvg)
    reasons.append(pullback_reason)

    bos1, bos_level = _one_minute_bos(one, direction)
    reasons.append("1m break of structure confirmed" if bos1 else "No 1m break of structure yet")

    checks = [htf_ok, mss, pullback, bos1]
    score = sum(1 for x in checks if x)
    ready = all(checks)

    return {
        "product": product,
        "signal": direction if ready else "NO_TRADE",
        "direction": direction,
        "price": current,
        "setup_score": score,
        "max_score": 4,
        "trade_ready": ready,
        "reasons": reasons,
        "liquidity_sweep": sweep,
        "fvg": fvg,
        "one_minute_bos_level": bos_level,
        "levels": {"1h_high": h1_hi, "1h_low": h1_lo, "4h_high": h4_hi, "4h_low": h4_lo},
        "strategy": "liquidity_sweep_mtf_v1",
        "note": "Mechanical interpretation of liquidity sweep -> 5m reversal -> pullback confluence -> 1m confirmation. No setup means no trade.",
    }
