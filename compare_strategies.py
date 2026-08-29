from __future__ import annotations
import json, threading, time
from datetime import datetime,timedelta,timezone
from typing import Any
import requests
from main import app

PUBLIC_BASE="https://api.exchange.coinbase.com"; HEADERS={"User-Agent":"quant-bot-abtest/3.0"}; PRODUCTS=["BTC-USD","ETH-USD","SOL-USD"]; FEE_BPS=40.0; TEST_DAYS=30

def _fetch(product,g,start,end):
    rows={}; cur=start; step=timedelta(seconds=g*280)
    while cur<end:
        e=min(cur+step,end); p={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":e.isoformat().replace("+00:00","Z")}
        r=requests.get(f"{PUBLIC_BASE}/products/{product}/candles",params=p,headers=HEADERS,timeout=15); r.raise_for_status()
        for x in r.json(): rows[int(x[0])]={"time":float(x[0]),"low":float(x[1]),"high":float(x[2]),"open":float(x[3]),"close":float(x[4]),"volume":float(x[5])}
        cur=e; time.sleep(.07)
    return [rows[k] for k in sorted(rows)]

def _prev_day(h,t):
    d=int(t//86400); a=[x for x in h if int(x["time"]//86400)==d-1]
    return (max(x["high"] for x in a),min(x["low"] for x in a)) if a else None

def _hlevels(h,t,n=12):
    a=[x for x in h if x["time"]<t][-n:]
    return (max(x["high"] for x in a),min(x["low"] for x in a)) if len(a)==n else None

def _bos5(f,i,d,maxbars=8):
    pre=f[max(0,i-4):i]
    if len(pre)<3:return None
    level=max(x["high"] for x in pre) if d=="BULLISH" else min(x["low"] for x in pre)
    for j in range(i+1,min(len(f),i+1+maxbars)):
        if (d=="BULLISH" and f[j]["close"]>level) or (d=="BEARISH" and f[j]["close"]<level):return j
    return None

def _ifvg(f,i,d,maxbars=8):
    end=min(len(f),i+1+maxbars)
    for j in range(max(2,i),end):
        a,c=f[j-2],f[j]
        if d=="BULLISH" and a["low"]>c["high"]:
            for k in range(j+1,end):
                if f[k]["close"]>a["low"]:return k
        if d=="BEARISH" and a["high"]<c["low"]:
            for k in range(j+1,end):
                if f[k]["close"]<a["high"]:return k
    return None

def _bos1(o,i,d,n=5):
    if i<n:return False
    p=o[i-n:i]
    return o[i]["close"]>(max(x["high"] for x in p)) if d=="BULLISH" else o[i]["close"]<(min(x["low"] for x in p))

def _entry(o,start,d,minutes=45):
    opp="BEARISH" if d=="BULLISH" else "BULLISH"; seen=False
    for i,c in enumerate(o):
        if c["time"]<start or c["time"]>start+minutes*60:continue
        if not seen and _bos1(o,i,opp):seen=True;continue
        if seen and _bos1(o,i,d):return c
    return None

def _target(entry,sh,sl,hh,hl):
    a=[x for x in (sh,sl,hh,hl) if x>entry]; return min(a,key=lambda x:abs(x-entry)) if a else None

def _fvg_touch(f,t,look=12):
    hist=[x for x in f if x["time"]<=t][-look:]
    if len(hist)<3:return False
    cur=hist[-1]
    for i in range(2,len(hist)):
        a,c=hist[i-2],hist[i]
        if a["high"]<c["low"] and cur["low"]<=c["low"] and cur["high"]>=a["high"]:return True
    return False

def _four_levels(h,t):
    prev=[x for x in h if x["time"]<t]
    buckets={}
    for x in prev:
        b=int(x["time"]//14400); buckets.setdefault(b,[]).append(x)
    vals=[]
    for b in sorted(buckets)[-10:]:
        a=buckets[b]; vals.append((max(x["high"] for x in a),min(x["low"] for x in a)))
    return (max(x[0] for x in vals),min(x[1] for x in vals)) if vals else None

def _metrics(trades):
    eq=peak=1.; dd=0.; wins=0
    for t in trades:
        eq*=1+t["net"]; peak=max(peak,eq); dd=max(dd,(peak-eq)/peak); wins+=t["net"]>0
    return {"trades":len(trades),"wins":wins,"losses":len(trades)-wins,"win_rate_pct":round(100*wins/len(trades),2) if trades else 0.,"return_pct":round(100*(eq-1),2),"max_drawdown_pct":round(100*dd,2)}

def _test(one,five,hourly,start,enhanced=False):
    fee=FEE_BPS/10000.; trades=[]; busy=0.
    for i in range(10,len(five)-10):
        c=five[i]
        if c["time"]<start or c["time"]<=busy:continue
        s=_prev_day(hourly,c["time"]); hl=_hlevels(hourly,c["time"])
        if not s or not hl:continue
        sh,sl=s; hh,ll=hl
        swept=next((l for l in (sl,ll) if c["low"]<l and c["close"]>l),None)
        if swept is None:continue
        if enhanced:
            four=_four_levels(hourly,c["time"])
            if four:
                _,fl=four
                if min(abs(c["low"]-sl)/sl,abs(c["low"]-ll)/ll,abs(c["low"]-fl)/fl)>.0075:continue
        bi=_bos5(five,i,"BULLISH"); ii=_ifvg(five,i,"BULLISH"); ci=min([x for x in (bi,ii) if x is not None],default=None)
        if ci is None:continue
        e=_entry(one,five[ci]["time"],"BULLISH")
        if e is None:continue
        if enhanced and not _fvg_touch(five,e["time"]):continue
        ep=e["close"]; tp=_target(ep,sh,sl,hh,ll); stop=c["low"]
        if tp is None or stop>=ep:continue
        future=[x for x in one if x["time"]>e["time"]]; xp=xt=None
        for x in future:
            if x["low"]<=stop:xp=stop;xt=x["time"];break
            if x["high"]>=tp:xp=tp;xt=x["time"];break
        if xp is None:xp=one[-1]["close"];xt=one[-1]["time"]
        trades.append({"net":xp/ep-1-2*fee});busy=xt
    return _metrics(trades)

def run_comparison():
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0); start=now-timedelta(days=TEST_DAYS); results=[]
    for p in PRODUCTS:
        one=_fetch(p,60,start-timedelta(hours=2),now); five=_fetch(p,300,start-timedelta(hours=4),now); hourly=_fetch(p,3600,start-timedelta(days=3),now)
        results.append({"product":p,"tjr_core":_test(one,five,hourly,start.timestamp(),False),"tjr_plus_filters":_test(one,five,hourly,start.timestamp(),True)})
    return {"period_days":TEST_DAYS,"fee_bps_per_side":FEE_BPS,"results":results,"enhanced_filters":["0.75% proximity to session/hourly/4h liquidity","5m FVG pullback touch after core confirmation"],"note":"Long-only historical simulation. Same TJR core entry/exit logic in both variants; enhanced version adds filters only."}

state={"status":"not_started","result":None,"error":None}
def _worker():
    state["status"]="running"
    try:
        r=run_comparison();state["result"]=r;state["status"]="complete";print("FILTER_AB_TEST="+json.dumps(r),flush=True)
    except Exception as e:state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("FILTER_AB_TEST_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def start_comparison():threading.Thread(target=_worker,daemon=True,name="filter-ab-test").start()
@app.get("/compare-strategies")
def compare_status():return state
