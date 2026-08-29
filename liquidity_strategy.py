from __future__ import annotations

from typing import Any
import requests

PUBLIC_BASE = "https://api.exchange.coinbase.com"
HEADERS = {"User-Agent": "quant-bot-liquidity-core/3.0"}


def _candles(product: str, granularity: int) -> list[dict[str, float]]:
    r = requests.get(f"{PUBLIC_BASE}/products/{product.upper()}/candles", params={"granularity": granularity}, headers=HEADERS, timeout=12)
    r.raise_for_status()
    return [{"time":float(x[0]),"low":float(x[1]),"high":float(x[2]),"open":float(x[3]),"close":float(x[4]),"volume":float(x[5])} for x in sorted(r.json(), key=lambda x:x[0])]


def _session_levels(hourly: list[dict[str,float]]) -> tuple[float,float]:
    # Mechanical crypto session proxy: previous completed UTC day high/low.
    latest_day = int(hourly[-1]["time"] // 86400)
    prev = [c for c in hourly if int(c["time"] // 86400) == latest_day - 1]
    sample = prev if prev else hourly[-25:-1]
    return max(c["high"] for c in sample), min(c["low"] for c in sample)


def _hourly_levels(hourly: list[dict[str,float]], lookback: int = 12) -> tuple[float,float]:
    sample = hourly[-lookback-1:-1]
    return max(c["high"] for c in sample), min(c["low"] for c in sample)


def _sweep(five: list[dict[str,float]], levels: list[tuple[str,float]], search: int = 12) -> dict[str,Any] | None:
    for i in range(len(five)-1, max(0,len(five)-search)-1, -1):
        c=five[i]
        for name,level in levels:
            if c["low"] < level and c["close"] > level:
                return {"direction":"BULLISH","index":i,"level":level,"level_name":name,"sweep_price":c["low"],"time":int(c["time"])}
            if c["high"] > level and c["close"] < level:
                return {"direction":"BEARISH","index":i,"level":level,"level_name":name,"sweep_price":c["high"],"time":int(c["time"])}
    return None


def _five_bos(five:list[dict[str,float]], sweep:dict[str,Any]) -> bool:
    i=int(sweep["index"])
    if i < 4: return False
    pre=five[max(0,i-4):i]
    post=five[i+1:]
    if sweep["direction"]=="BULLISH":
        level=max(x["high"] for x in pre)
        return any(x["close"] > level for x in post)
    level=min(x["low"] for x in pre)
    return any(x["close"] < level for x in post)


def _inverse_fvg(five:list[dict[str,float]], direction:str, lookback:int=12) -> bool:
    start=max(2,len(five)-lookback)
    for i in range(start,len(five)):
        a,c=five[i-2],five[i]
        if direction=="BULLISH" and a["low"] > c["high"] and five[-1]["close"] > a["low"]: return True
        if direction=="BEARISH" and a["high"] < c["low"] and five[-1]["close"] < a["high"]: return True
    return False


def _one_bos(one:list[dict[str,float]], direction:str, end:int, lookback:int=5) -> bool:
    if end < lookback: return False
    prev=one[end-lookback:end]
    c=one[end]
    if direction=="BULLISH": return c["close"] > max(x["high"] for x in prev)
    return c["close"] < min(x["low"] for x in prev)


def _continuation_sequence(one:list[dict[str,float]], intended:str, window:int=30) -> tuple[bool,str]:
    # Required continuation: 1m BOS opposite intended direction, then BOS back intended direction.
    if len(one) < 12: return False,"not enough 1m data"
    opposite="BEARISH" if intended=="BULLISH" else "BULLISH"
    start=max(5,len(one)-window)
    opposite_at=None
    for i in range(start,len(one)-1):
        if _one_bos(one,opposite,i): opposite_at=i
    if opposite_at is None: return False,"waiting for opposite 1m BOS retrace"
    for i in range(opposite_at+1,len(one)):
        if _one_bos(one,intended,i): return True,"opposite 1m BOS retrace then intended-direction 1m BOS confirmed"
    return False,"retrace confirmed; waiting for 1m BOS back in intended direction"


def _exit_draw(direction:str, price:float, levels:list[tuple[str,float]]) -> dict[str,Any] | None:
    candidates=[(n,l) for n,l in levels if (l>price if direction=="BULLISH" else l<price)]
    if not candidates: return None
    name,level=min(candidates,key=lambda x:abs(x[1]-price))
    return {"level_name":name,"price":level}


def build_liquidity_signal(product:str) -> dict[str,Any]:
    product=product.upper()
    one=_candles(product,60); five=_candles(product,300); hourly=_candles(product,3600)
    if len(one)<40 or len(five)<40 or len(hourly)<30: raise ValueError(f"Not enough market data for {product}")
    session_hi,session_lo=_session_levels(hourly); h1_hi,h1_lo=_hourly_levels(hourly)
    levels=[("session_high",session_hi),("session_low",session_lo),("hourly_high",h1_hi),("hourly_low",h1_lo)]
    price=one[-1]["close"]
    sweep=_sweep(five,levels)
    if not sweep:
        return {"product":product,"signal":"NO_TRADE","trade_ready":False,"price":price,"reasons":["No session/hourly liquidity sweep"],"levels":dict(levels),"strategy":"tjr_core_liquidity_v1"}
    direction=sweep["direction"]
    reversal_bos=_five_bos(five,sweep)
    reversal_ifvg=_inverse_fvg(five,direction)
    reversal=reversal_bos or reversal_ifvg
    continuation,continuation_reason=_continuation_sequence(one,direction)
    target=_exit_draw(direction,price,levels)
    ready=reversal and continuation and target is not None
    reasons=[f"{sweep['level_name']} swept", "5m reversal confirmed by BOS/iFVG" if reversal else "waiting for 5m BOS or inverse FVG", continuation_reason, "liquidity-draw exit available" if target else "no valid session/hourly liquidity exit draw"]
    return {"product":product,"signal":direction if ready else "NO_TRADE","direction":direction,"trade_ready":ready,"price":price,"reasons":reasons,"liquidity_sweep":sweep,"five_minute_bos":reversal_bos,"inverse_fvg":reversal_ifvg,"continuation_confirmed":continuation,"take_profit":target,"levels":dict(levels),"strategy":"tjr_core_liquidity_v1","note":"Uses only: session/hourly liquidity sweep -> 5m BOS or inverse FVG reversal -> opposite 1m BOS retrace -> 1m BOS back with intended direction -> exit at another session/hourly liquidity draw."}
