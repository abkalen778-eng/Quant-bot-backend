from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from main import app, _sma, _rsi

PUBLIC_BASE = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "quant-bot-comparison/2.0"}
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD"]
FEE_BPS = 40.0
TEST_DAYS = 7


def _fetch_range(product: str, granularity: int, start: datetime, end: datetime) -> list[dict[str, float]]:
    step = timedelta(seconds=granularity * 280)
    cursor = start
    rows: dict[int, dict[str, float]] = {}
    while cursor < end:
        chunk_end = min(cursor + step, end)
        params = {"granularity": granularity, "start": cursor.isoformat().replace("+00:00", "Z"), "end": chunk_end.isoformat().replace("+00:00", "Z")}
        r = requests.get(f"{PUBLIC_BASE}/products/{product}/candles", params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        for x in r.json():
            ts = int(x[0])
            rows[ts] = {"time": float(ts), "low": float(x[1]), "high": float(x[2]), "open": float(x[3]), "close": float(x[4]), "volume": float(x[5])}
        cursor = chunk_end
        time.sleep(0.08)
    return [rows[k] for k in sorted(rows)]


def _old_signal(closes: list[float]) -> str:
    s20, s50, r14 = _sma(closes, 20), _sma(closes, 50), _rsi(closes, 14)
    score = 0
    if s20 is not None and s50 is not None: score += 1 if s20 > s50 else -1
    if r14 is not None:
        if r14 < 35: score += 1
        elif r14 > 70: score -= 1
    if s20 is not None: score += 1 if closes[-1] > s20 else -1
    return "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    equity = 1.0; peak = 1.0; max_dd = 0.0
    for t in trades:
        equity *= 1.0 + t["net_return"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    wins = sum(t["net_return"] > 0 for t in trades)
    return {"trades":len(trades),"wins":wins,"losses":len(trades)-wins,"win_rate_pct":round((wins/len(trades)*100) if trades else 0.0,2),"return_pct":round((equity-1.0)*100,2),"max_drawdown_pct":round(max_dd*100,2)}


def _backtest_old(hourly: list[dict[str, float]], test_start: float) -> dict[str, Any]:
    fee = FEE_BPS / 10000.0; trades=[]; in_pos=False; entry=0.0; entry_time=0; closes=[]
    for c in hourly:
        closes.append(c["close"])
        if len(closes)<50 or c["time"]<test_start: continue
        sig=_old_signal(closes)
        if not in_pos and sig=="BULLISH": in_pos=True; entry=c["close"]; entry_time=int(c["time"])
        elif in_pos and sig=="BEARISH":
            gross=c["close"]/entry-1.0; trades.append({"entry_time":entry_time,"exit_time":int(c["time"]),"net_return":gross-2*fee}); in_pos=False
    if in_pos:
        c=hourly[-1]; gross=c["close"]/entry-1.0; trades.append({"entry_time":entry_time,"exit_time":int(c["time"]),"net_return":gross-2*fee})
    return _metrics(trades)


def _previous_day_levels(hourly: list[dict[str,float]], t: float) -> tuple[float,float] | None:
    day=int(t//86400); prev=[c for c in hourly if int(c["time"]//86400)==day-1]
    if not prev: return None
    return max(c["high"] for c in prev), min(c["low"] for c in prev)


def _hourly_levels(hourly:list[dict[str,float]], t:float, lookback:int=12) -> tuple[float,float] | None:
    prev=[c for c in hourly if c["time"]<t][-lookback:]
    if len(prev)<lookback: return None
    return max(c["high"] for c in prev), min(c["low"] for c in prev)


def _five_bos(five:list[dict[str,float]], sweep_i:int, direction:str, max_bars:int=8) -> int | None:
    pre=five[max(0,sweep_i-4):sweep_i]
    if len(pre)<3: return None
    if direction=="BULLISH":
        level=max(x["high"] for x in pre)
        for j in range(sweep_i+1,min(len(five),sweep_i+1+max_bars)):
            if five[j]["close"]>level: return j
    else:
        level=min(x["low"] for x in pre)
        for j in range(sweep_i+1,min(len(five),sweep_i+1+max_bars)):
            if five[j]["close"]<level: return j
    return None


def _inverse_fvg_confirm(five:list[dict[str,float]], sweep_i:int, direction:str, max_bars:int=8) -> int | None:
    end=min(len(five),sweep_i+1+max_bars)
    for j in range(max(2,sweep_i),end):
        a,c=five[j-2],five[j]
        if direction=="BULLISH" and a["low"]>c["high"]:
            gap=a["low"]
            for k in range(j+1,end):
                if five[k]["close"]>gap: return k
        if direction=="BEARISH" and a["high"]<c["low"]:
            gap=a["high"]
            for k in range(j+1,end):
                if five[k]["close"]<gap: return k
    return None


def _bos1(one:list[dict[str,float]], idx:int, direction:str, lookback:int=5) -> bool:
    if idx<lookback: return False
    prev=one[idx-lookback:idx]
    if direction=="BULLISH": return one[idx]["close"]>max(x["high"] for x in prev)
    return one[idx]["close"]<min(x["low"] for x in prev)


def _continuation_entry(one:list[dict[str,float]], start_t:float, direction:str, window_minutes:int=45) -> dict[str,float] | None:
    candidates=[(i,c) for i,c in enumerate(one) if start_t<=c["time"]<=start_t+window_minutes*60]
    opposite="BEARISH" if direction=="BULLISH" else "BULLISH"
    opposite_seen=False
    for idx,c in candidates:
        if not opposite_seen and _bos1(one,idx,opposite):
            opposite_seen=True
            continue
        if opposite_seen and _bos1(one,idx,direction): return c
    return None


def _next_liquidity_target(direction:str, entry:float, session_hi:float, session_lo:float, h1_hi:float, h1_lo:float) -> float | None:
    levels=[session_hi,session_lo,h1_hi,h1_lo]
    candidates=[x for x in levels if (x>entry if direction=="BULLISH" else x<entry)]
    if not candidates: return None
    return min(candidates,key=lambda x:abs(x-entry))


def _backtest_liquidity(one:list[dict[str,float]], five:list[dict[str,float]], hourly:list[dict[str,float]], test_start:float) -> dict[str,Any]:
    fee=FEE_BPS/10000.0; trades=[]; busy_until=0.0
    # Long-only because current Coinbase deployment is spot, not shorting.
    for i in range(10,len(five)-10):
        c=five[i]
        if c["time"]<test_start or c["time"]<=busy_until: continue
        session=_previous_day_levels(hourly,c["time"]); h1=_hourly_levels(hourly,c["time"])
        if not session or not h1: continue
        session_hi,session_lo=session; h1_hi,h1_lo=h1
        swept_level=None
        for level in (session_lo,h1_lo):
            if c["low"]<level and c["close"]>level:
                swept_level=level; break
        if swept_level is None: continue
        bos_i=_five_bos(five,i,"BULLISH")
        ifvg_i=_inverse_fvg_confirm(five,i,"BULLISH")
        confirm_i=min([x for x in (bos_i,ifvg_i) if x is not None], default=None)
        if confirm_i is None: continue
        entry_c=_continuation_entry(one,five[confirm_i]["time"],"BULLISH")
        if entry_c is None: continue
        entry=entry_c["close"]
        target=_next_liquidity_target("BULLISH",entry,session_hi,session_lo,h1_hi,h1_lo)
        if target is None or target<=entry: continue
        stop=c["low"]
        if stop>=entry: continue
        entry_t=int(entry_c["time"])
        future=[x for x in one if x["time"]>entry_t]
        exit_px=None; exit_t=None
        for x in future:
            if x["low"]<=stop:
                exit_px=stop; exit_t=int(x["time"]); break
            if x["high"]>=target:
                exit_px=target; exit_t=int(x["time"]); break
        if exit_px is None:
            x=one[-1]; exit_px=x["close"]; exit_t=int(x["time"])
        gross=exit_px/entry-1.0
        trades.append({"entry_time":entry_t,"exit_time":exit_t,"net_return":gross-2*fee})
        busy_until=float(exit_t)
    return _metrics(trades)


def run_comparison() -> dict[str, Any]:
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    test_start_dt=now-timedelta(days=TEST_DAYS)
    history_start=test_start_dt-timedelta(days=2)
    results=[]
    for product in PRODUCTS:
        one=_fetch_range(product,60,test_start_dt-timedelta(hours=2),now)
        five=_fetch_range(product,300,test_start_dt-timedelta(hours=4),now)
        hourly=_fetch_range(product,3600,history_start,now)
        old=_backtest_old(hourly,test_start_dt.timestamp())
        liquidity=_backtest_liquidity(one,five,hourly,test_start_dt.timestamp())
        first=next((x for x in hourly if x["time"]>=test_start_dt.timestamp()),hourly[0])
        buy_hold=(hourly[-1]["close"]/first["close"]-1.0)*100
        results.append({"product":product,"old_strategy":old,"tjr_core_liquidity":liquidity,"buy_hold_return_pct":round(buy_hold,2)})
    return {"period_days":TEST_DAYS,"fee_bps_per_side":FEE_BPS,"results":results,"note":"Historical simulation. TJR core test uses only session/hourly liquidity sweep -> 5m BOS or inverse FVG -> opposite 1m BOS retrace -> 1m BOS back bullish -> exit at next session/hourly liquidity draw. Long-only to match Coinbase spot."}


comparison_state: dict[str,Any]={"status":"not_started","result":None,"error":None}


def _worker() -> None:
    comparison_state["status"]="running"
    try:
        result=run_comparison(); comparison_state["result"]=result; comparison_state["status"]="complete"
        print("STRATEGY_COMPARISON="+json.dumps(result),flush=True)
    except Exception as exc:
        comparison_state["status"]="failed"; comparison_state["error"]=f"{type(exc).__name__}: {exc}"; print("STRATEGY_COMPARISON_ERROR="+comparison_state["error"],flush=True)


@app.on_event("startup")
def start_comparison() -> None:
    threading.Thread(target=_worker,daemon=True,name="strategy-comparison").start()


@app.get("/compare-strategies")
def compare_status() -> dict[str,Any]: return comparison_state
