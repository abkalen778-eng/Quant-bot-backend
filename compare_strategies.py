from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from main import app, _sma, _rsi

PUBLIC_BASE = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "quant-bot-comparison/1.0"}
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD"]
FEE_BPS = 40.0
RR_TARGET = 1.33
TEST_DAYS = 7


def _fetch_range(product: str, granularity: int, start: datetime, end: datetime) -> list[dict[str, float]]:
    step = timedelta(seconds=granularity * 280)
    cursor = start
    rows: dict[int, dict[str, float]] = {}
    while cursor < end:
        chunk_end = min(cursor + step, end)
        params = {
            "granularity": granularity,
            "start": cursor.isoformat().replace("+00:00", "Z"),
            "end": chunk_end.isoformat().replace("+00:00", "Z"),
        }
        r = requests.get(f"{PUBLIC_BASE}/products/{product}/candles", params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        for x in r.json():
            ts = int(x[0])
            rows[ts] = {"time": float(ts), "low": float(x[1]), "high": float(x[2]), "open": float(x[3]), "close": float(x[4]), "volume": float(x[5])}
        cursor = chunk_end
        time.sleep(0.08)
    return [rows[k] for k in sorted(rows)]


def _aggregate_4h(hourly: list[dict[str, float]]) -> list[dict[str, float]]:
    buckets: dict[int, list[dict[str, float]]] = {}
    for c in hourly:
        buckets.setdefault(int(c["time"] // 14400), []).append(c)
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        out.append({"time": b[0]["time"], "open": b[0]["open"], "high": max(x["high"] for x in b), "low": min(x["low"] for x in b), "close": b[-1]["close"]})
    return out


def _old_signal(closes: list[float]) -> str:
    s20, s50, r14 = _sma(closes, 20), _sma(closes, 50), _rsi(closes, 14)
    score = 0
    if s20 is not None and s50 is not None:
        score += 1 if s20 > s50 else -1
    if r14 is not None:
        if r14 < 35: score += 1
        elif r14 > 70: score -= 1
    if s20 is not None:
        score += 1 if closes[-1] > s20 else -1
    return "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for t in trades:
        equity *= 1.0 + t["net_return"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    wins = sum(t["net_return"] > 0 for t in trades)
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round((wins / len(trades) * 100) if trades else 0.0, 2),
        "return_pct": round((equity - 1.0) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }


def _backtest_old(hourly: list[dict[str, float]], test_start: float) -> dict[str, Any]:
    fee = FEE_BPS / 10000.0
    trades = []
    in_pos = False
    entry = 0.0
    entry_time = 0
    closes: list[float] = []
    for c in hourly:
        closes.append(c["close"])
        if len(closes) < 50 or c["time"] < test_start:
            continue
        sig = _old_signal(closes)
        if not in_pos and sig == "BULLISH":
            in_pos, entry, entry_time = True, c["close"], int(c["time"])
        elif in_pos and sig == "BEARISH":
            gross = c["close"] / entry - 1.0
            trades.append({"entry_time": entry_time, "exit_time": int(c["time"]), "net_return": gross - 2 * fee})
            in_pos = False
    if in_pos:
        c = hourly[-1]
        gross = c["close"] / entry - 1.0
        trades.append({"entry_time": entry_time, "exit_time": int(c["time"]), "net_return": gross - 2 * fee})
    return _metrics(trades)


def _prev(items: list[dict[str, float]], t: float, n: int) -> list[dict[str, float]]:
    prior = [x for x in items if x["time"] < t]
    return prior[-n:]


def _find_fvg(five: list[dict[str, float]], idx: int) -> dict[str, float] | None:
    start = max(2, idx - 8)
    for j in range(idx, start - 1, -1):
        if five[j - 2]["high"] < five[j]["low"]:
            return {"low": five[j - 2]["high"], "high": five[j]["low"]}
    return None


def _backtest_liquidity(one: list[dict[str, float]], five: list[dict[str, float]], hourly: list[dict[str, float]], test_start: float) -> dict[str, Any]:
    fee = FEE_BPS / 10000.0
    four = _aggregate_4h(hourly)
    one_by_time = {int(c["time"]): i for i, c in enumerate(one)}
    trades: list[dict[str, Any]] = []
    busy_until = 0.0

    for i in range(20, len(five) - 8):
        c = five[i]
        if c["time"] < test_start or c["time"] <= busy_until:
            continue
        prev20 = five[i - 20:i]
        lo = min(x["low"] for x in prev20)
        if not (c["low"] < lo and c["close"] > lo):
            continue

        h1 = _prev(hourly, c["time"], 20)
        h4 = _prev(four, c["time"], 10)
        if len(h1) < 20 or len(h4) < 4:
            continue
        h1_lo = min(x["low"] for x in h1)
        h4_lo = min(x["low"] for x in h4)
        sweep_px = c["low"]
        proximity = min(abs(sweep_px - h1_lo) / h1_lo, abs(sweep_px - h4_lo) / h4_lo)
        if proximity > 0.0075:
            continue

        pre = five[max(0, i - 4):i]
        structural_high = max(x["high"] for x in pre)
        mss_idx = None
        for j in range(i + 1, min(i + 7, len(five))):
            if five[j]["close"] > structural_high:
                mss_idx = j
                break
        if mss_idx is None:
            continue

        fvg = _find_fvg(five, mss_idx)
        pull_idx = None
        for j in range(mss_idx + 1, min(mss_idx + 7, len(five))):
            recent = five[max(i, j - 11):j + 1]
            eq = (max(x["high"] for x in recent) + min(x["low"] for x in recent)) / 2.0
            fvg_touch = bool(fvg and five[j]["low"] <= fvg["high"] and five[j]["high"] >= fvg["low"])
            eq_touch = five[j]["low"] <= eq <= five[j]["high"]
            if fvg_touch or eq_touch:
                pull_idx = j
                break
        if pull_idx is None:
            continue

        start_minute = int(five[pull_idx]["time"])
        minute_candidates = [x for x in one if start_minute <= x["time"] <= start_minute + 900]
        if len(minute_candidates) < 7:
            continue
        entry_c = None
        for k in range(5, len(minute_candidates)):
            prev5 = minute_candidates[k - 5:k]
            bos = max(x["high"] for x in prev5)
            if minute_candidates[k]["close"] > bos:
                entry_c = minute_candidates[k]
                break
        if entry_c is None:
            continue

        entry = entry_c["close"]
        stop = sweep_px
        risk = entry - stop
        if risk <= 0 or risk / entry > 0.05:
            continue
        target = entry + RR_TARGET * risk
        entry_t = int(entry_c["time"])
        future = [x for x in one if x["time"] > entry_t and x["time"] <= entry_t + 12 * 3600]
        exit_px = future[-1]["close"] if future else entry
        exit_t = int(future[-1]["time"]) if future else entry_t
        for x in future:
            if x["low"] <= stop:
                exit_px, exit_t = stop, int(x["time"])
                break
            if x["high"] >= target:
                exit_px, exit_t = target, int(x["time"])
                break
        gross = exit_px / entry - 1.0
        trades.append({"entry_time": entry_t, "exit_time": exit_t, "net_return": gross - 2 * fee})
        busy_until = exit_t

    return _metrics(trades)


def run_comparison() -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    test_start_dt = now - timedelta(days=TEST_DAYS)
    history_start = test_start_dt - timedelta(hours=90)
    results = []
    for product in PRODUCTS:
        one = _fetch_range(product, 60, test_start_dt - timedelta(hours=2), now)
        five = _fetch_range(product, 300, test_start_dt - timedelta(hours=4), now)
        hourly = _fetch_range(product, 3600, history_start, now)
        old = _backtest_old(hourly, test_start_dt.timestamp())
        liquidity = _backtest_liquidity(one, five, hourly, test_start_dt.timestamp())
        first = next((x for x in hourly if x["time"] >= test_start_dt.timestamp()), hourly[0])
        buy_hold = (hourly[-1]["close"] / first["close"] - 1.0) * 100
        results.append({"product": product, "old_strategy": old, "liquidity_strategy": liquidity, "buy_hold_return_pct": round(buy_hold, 2)})
    return {"period_days": TEST_DAYS, "fee_bps_per_side": FEE_BPS, "rr_target": RR_TARGET, "results": results, "note": "Historical simulation; liquidity strategy is long-only to match Coinbase spot deployment. Stop is the swept low; target is 1.33R."}


comparison_state: dict[str, Any] = {"status": "not_started", "result": None, "error": None}


def _worker() -> None:
    comparison_state["status"] = "running"
    try:
        result = run_comparison()
        comparison_state["result"] = result
        comparison_state["status"] = "complete"
        print("STRATEGY_COMPARISON=" + json.dumps(result), flush=True)
    except Exception as exc:
        comparison_state["status"] = "failed"
        comparison_state["error"] = f"{type(exc).__name__}: {exc}"
        print("STRATEGY_COMPARISON_ERROR=" + comparison_state["error"], flush=True)


@app.on_event("startup")
def start_comparison() -> None:
    threading.Thread(target=_worker, daemon=True, name="strategy-comparison").start()


@app.get("/compare-strategies")
def compare_status() -> dict[str, Any]:
    return comparison_state
