from __future__ import annotations
from typing import Any
import requests

PUBLIC_BASE="https://api.exchange.coinbase.com"
HEADERS={"User-Agent":"quant-bot-tjr-filtered/4.0"}

def _candles(product:str,granularity:int)->list[dict[str,float]]:
    r=requests.get(f"{PUBLIC_BASE}/products/{product.upper()}/candles",params={"granularity":granularity},headers=HEADERS,timeout=12);r.raise_for_status()
    return [{"time":float(x[0]),"low":float(x[1]),"high":float(x[2]),"open":float(x[3]),"close":float(x[4]),"volume":float(x[5])} for x in sorted(r.json(),key=lambda x:x[0])]

def _session_levels(h):
    d=int(h[-1]["time"]//86400);p=[c for c in h if int(c["time"]//86400)==d-1] or h[-25:-1]
    return max(c["high"] for c in p),min(c["low"] for c in p)

def _hourly_levels(h,n=12):
    s=h[-n-1:-1];return max(c["high"] for c in s),min(c["low"] for c in s)

def _four_hour_levels(h):
    buckets={}
    for c in h[:-1]: buckets.setdefault(int(c["time"]//14400),[]).append(c)
    vals=[]
    for b in sorted(buckets)[-10:]:
        a=buckets[b];vals.append((max(x["high"] for x in a),min(x["low"] for x in a)))
    return max(x[0] for x in vals),min(x[1] for x in vals)

def _sweep(five,levels,search=12):
    for i in range(len(five)-1,max(0,len(five)-search)-1,-1):
        c=five[i]
        for name,level in levels:
            if c["low"]<level and c["close"]>level:return {"direction":"BULLISH","index":i,"level":level,"level_name":name,"sweep_price":c["low"],"time":int(c["time"])}
            if c["high"]>level and c["close"]<level:return {"direction":"BEARISH","index":i,"level":level,"level_name":name,"sweep_price":c["high"],"time":int(c["time"])}
    return None

def _five_bos(five,sweep):
    i=sweep["index"];pre=five[max(0,i-4):i];post=five[i+1:]
    if len(pre)<3:return False
    if sweep["direction"]=="BULLISH":return any(x["close"]>max(y["high"] for y in pre) for x in post)
    return any(x["close"]<min(y["low"] for y in pre) for x in post)

def _inverse_fvg(five,direction,lookback=12):
    for i in range(max(2,len(five)-lookback),len(five)):
        a,c=five[i-2],five[i]
        if direction=="BULLISH" and a["low"]>c["high"] and five[-1]["close"]>a["low"]:return True
        if direction=="BEARISH" and a["high"]<c["low"] and five[-1]["close"]<a["high"]:return True
    return False

def _one_bos(one,direction,end,lookback=5):
    if end<lookback:return False
    p=one[end-lookback:end];c=one[end]
    return c["close"]>max(x["high"] for x in p) if direction=="BULLISH" else c["close"]<min(x["low"] for x in p)

def _continuation(one,intended,window=30):
    opp="BEARISH" if intended=="BULLISH" else "BULLISH";start=max(5,len(one)-window);seen=None
    for i in range(start,len(one)-1):
        if _one_bos(one,opp,i):seen=i
    if seen is None:return False,"waiting for opposite 1m BOS retrace"
    for i in range(seen+1,len(one)):
        if _one_bos(one,intended,i):return True,"1m continuation confirmed"
    return False,"waiting for 1m BOS back in intended direction"

def _fvg_pullback(five,direction,lookback=12):
    cur=five[-1]
    for i in range(max(2,len(five)-lookback),len(five)):
        a,c=five[i-2],five[i]
        if direction=="BULLISH" and a["high"]<c["low"] and cur["low"]<=c["low"]:return True
        if direction=="BEARISH" and a["low"]>c["high"] and cur["high"]>=c["high"]:return True
    return False

def _equilibrium_ok(direction,price,hi,lo):
    mid=(hi+lo)/2
    return price<=mid if direction=="BULLISH" else price>=mid

def _trend_ok(direction,h):
    closes=[x["close"] for x in h]
    if len(closes)<50:return False
    s20=sum(closes[-20:])/20;s50=sum(closes[-50:])/50
    return s20>s50 if direction=="BULLISH" else s20<s50

def _proximity_ok(sweep_price,levels,pct=.0075):
    return min(abs(sweep_price-l)/l for _,l in levels)<=pct

def _exit_draw(direction,price,levels):
    c=[(n,l) for n,l in levels if (l>price if direction=="BULLISH" else l<price)]
    if not c:return None
    n,l=min(c,key=lambda x:abs(x[1]-price));return {"level_name":n,"price":l}

def build_liquidity_signal(product:str)->dict[str,Any]:
    product=product.upper();one=_candles(product,60);five=_candles(product,300);hourly=_candles(product,3600)
    if len(one)<40 or len(five)<40 or len(hourly)<60:raise ValueError(f"Not enough market data for {product}")
    sh,sl=_session_levels(hourly);hh,hl=_hourly_levels(hourly);fh,fl=_four_hour_levels(hourly)
    core_levels=[("session_high",sh),("session_low",sl),("hourly_high",hh),("hourly_low",hl)]
    all_levels=core_levels+[("four_hour_high",fh),("four_hour_low",fl)]
    price=one[-1]["close"];sweep=_sweep(five,core_levels)
    if not sweep:return {"product":product,"signal":"NO_TRADE","trade_ready":False,"price":price,"reasons":["No session/hourly liquidity sweep"],"strategy":"tjr_core_plus_4_filters_v1"}
    d=sweep["direction"];rev=_five_bos(five,sweep) or _inverse_fvg(five,d);cont,cont_reason=_continuation(one,d);target=_exit_draw(d,price,core_levels)
    proximity=_proximity_ok(sweep["sweep_price"],all_levels);fvg=_fvg_pullback(five,d);eq=_equilibrium_ok(d,price,sh,sl);trend=_trend_ok(d,hourly)
    ready=rev and cont and target is not None and proximity and fvg and eq and trend
    reasons=[f"{sweep['level_name']} swept","5m BOS/iFVG confirmed" if rev else "waiting for 5m BOS/iFVG",cont_reason,f"4H liquidity proximity: {proximity}",f"5m FVG pullback: {fvg}",f"equilibrium location: {eq}",f"HTF trend aligned: {trend}"]
    return {"product":product,"signal":d if ready else "NO_TRADE","direction":d,"trade_ready":ready,"price":price,"reasons":reasons,"liquidity_sweep":sweep,"take_profit":target,"filters":{"four_hour_liquidity_proximity":proximity,"five_minute_fvg_pullback":fvg,"equilibrium_location":eq,"higher_timeframe_trend_alignment":trend},"levels":dict(all_levels),"strategy":"tjr_core_plus_4_filters_v1"}
