from __future__ import annotations
import json, threading, time
from datetime import datetime,timedelta,timezone
import requests
from main import app

PUBLIC_BASE="https://api.exchange.coinbase.com";HEADERS={"User-Agent":"quant-bot-abtest/5.0"};PRODUCTS=["BTC-USD","ETH-USD"];FEE_BPS=40.0;TEST_DAYS=30;STRICT_STOP_PCT=.0075

def _fetch(product,g,start,end):
    rows={};cur=start;step=timedelta(seconds=g*280)
    while cur<end:
        e=min(cur+step,end);p={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":e.isoformat().replace("+00:00","Z")}
        r=requests.get(f"{PUBLIC_BASE}/products/{product}/candles",params=p,headers=HEADERS,timeout=15);r.raise_for_status()
        for x in r.json():rows[int(x[0])]={"time":float(x[0]),"low":float(x[1]),"high":float(x[2]),"open":float(x[3]),"close":float(x[4]),"volume":float(x[5])}
        cur=e;time.sleep(.07)
    return [rows[k] for k in sorted(rows)]

def _prev_day(h,t):
    d=int(t//86400);a=[x for x in h if int(x["time"]//86400)==d-1]
    return (max(x["high"] for x in a),min(x["low"] for x in a)) if a else None

def _hlevels(h,t,n=12):
    a=[x for x in h if x["time"]<t][-n:]
    return (max(x["high"] for x in a),min(x["low"] for x in a)) if len(a)==n else None

def _four_levels(h,t):
    prev=[x for x in h if x["time"]<t];b={}
    for x in prev:b.setdefault(int(x["time"]//14400),[]).append(x)
    vals=[]
    for k in sorted(b)[-10:]:
        a=b[k];vals.append((max(x["high"] for x in a),min(x["low"] for x in a)))
    return (max(x[0] for x in vals),min(x[1] for x in vals)) if vals else None

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
    return o[i]["close"]>max(x["high"] for x in p) if d=="BULLISH" else o[i]["close"]<min(x["low"] for x in p)

def _entry(o,start,d,minutes=45):
    opp="BEARISH" if d=="BULLISH" else "BULLISH";seen=False
    for i,c in enumerate(o):
        if c["time"]<start or c["time"]>start+minutes*60:continue
        if not seen and _bos1(o,i,opp):seen=True;continue
        if seen and _bos1(o,i,d):return c
    return None

def _target(entry,sh,sl,hh,hl):
    a=[x for x in (sh,sl,hh,hl) if x>entry];return min(a,key=lambda x:abs(x-entry)) if a else None

def _fvg_touch(f,t,look=12):
    hist=[x for x in f if x["time"]<=t][-look:]
    if len(hist)<3:return False
    cur=hist[-1]
    for i in range(2,len(hist)):
        a,c=hist[i-2],hist[i]
        if a["high"]<c["low"] and cur["low"]<=c["low"] and cur["high"]>=a["high"]:return True
    return False

def _equilibrium_ok(price,sh,sl):return price<=(sh+sl)/2

def _trend_ok(h,t):
    a=[x["close"] for x in h if x["time"]<t]
    return len(a)>=50 and sum(a[-20:])/20>sum(a[-50:])/50

def _momentum_ok(h,t):
    a=[x["close"] for x in h if x["time"]<=t]
    if len(a)<50:return False
    s20=sum(a[-20:])/20;s50=sum(a[-50:])/50;ret6=a[-1]/a[-7]-1
    return a[-1]>s20>s50 and ret6>.002

def _volume_ok(f,t):
    a=[x for x in f if x["time"]<=t]
    if len(a)<25:return False
    vols=[x["volume"] for x in a];avg20=sum(vols[-21:-1])/20;recent=sum(vols[-3:])/3;prior=sum(vols[-6:-3])/3;c=a[-1]
    return c["close"]>c["open"] and vols[-1]>=avg20*1.10 and recent>prior

def _metrics(trades):
    eq=peak=1.;dd=0.;wins=0
    for t in trades:
        eq*=1+t["net"];peak=max(peak,eq);dd=max(dd,(peak-eq)/peak);wins+=t["net"]>0
    return {"trades":len(trades),"wins":wins,"losses":len(trades)-wins,"win_rate_pct":round(100*wins/len(trades),2) if trades else 0.,"return_pct":round(100*(eq-1),2),"max_drawdown_pct":round(100*dd,2)}

def _test(one,five,hourly,start,extra=False):
    fee=FEE_BPS/10000.;trades=[];busy=0.
    for i in range(10,len(five)-10):
        c=five[i]
        if c["time"]<start or c["time"]<=busy:continue
        s=_prev_day(hourly,c["time"]);hl=_hlevels(hourly,c["time"])
        if not s or not hl:continue
        sh,sl=s;hh,ll=hl;swept=next((l for l in (sl,ll) if c["low"]<l and c["close"]>l),None)
        if swept is None:continue
        four=_four_levels(hourly,c["time"])
        if not four:continue
        _,fl=four
        if min(abs(c["low"]-sl)/sl,abs(c["low"]-ll)/ll,abs(c["low"]-fl)/fl)>.0075:continue
        if not _trend_ok(hourly,c["time"]):continue
        bi=_bos5(five,i,"BULLISH");ii=_ifvg(five,i,"BULLISH");ci=min([x for x in (bi,ii) if x is not None],default=None)
        if ci is None:continue
        e=_entry(one,five[ci]["time"],"BULLISH")
        if e is None:continue
        if not _fvg_touch(five,e["time"]) or not _equilibrium_ok(e["close"],sh,sl):continue
        if extra and (not _momentum_ok(hourly,e["time"]) or not _volume_ok(five,e["time"])):continue
        ep=e["close"];tp=_target(ep,sh,sl,hh,ll)
        if tp is None:continue
        stop=max(c["low"],ep*(1-STRICT_STOP_PCT)) if extra else c["low"]
        if stop>=ep:continue
        xp=xt=None
        for x in one:
            if x["time"]<=e["time"]:continue
            if x["low"]<=stop:xp=stop;xt=x["time"];break
            if x["high"]>=tp:xp=tp;xt=x["time"];break
        if xp is None:xp=one[-1]["close"];xt=one[-1]["time"]
        trades.append({"net":xp/ep-1-2*fee});busy=xt
    return _metrics(trades)

def run_comparison():
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0);start=now-timedelta(days=TEST_DAYS);results=[]
    for p in PRODUCTS:
        one=_fetch(p,60,start-timedelta(hours=2),now);five=_fetch(p,300,start-timedelta(hours=4),now);hourly=_fetch(p,3600,start-timedelta(days=4),now)
        results.append({"product":p,"tjr_plus_4_filters":_test(one,five,hourly,start.timestamp(),False),"tjr_plus_8_rules":_test(one,five,hourly,start.timestamp(),True)})
    return {"period_days":TEST_DAYS,"fee_bps_per_side":FEE_BPS,"products":PRODUCTS,"results":results,"new_rules":["strong bullish momentum only","rising 5m volume >= 110% of 20-bar average","strict 0.75% maximum stop distance","BTC/ETH only"],"note":"Long-only historical simulation matching Coinbase spot. New version layers momentum, rising-volume, strict-stop, and major-coin rules on the existing four-filter TJR setup."}

state={"status":"not_started","result":None,"error":None}
def _worker():
    state["status"]="running"
    try:
        r=run_comparison();state["result"]=r;state["status"]="complete";print("EIGHT_RULE_TEST="+json.dumps(r),flush=True)
    except Exception as e:state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("EIGHT_RULE_TEST_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def start_comparison():threading.Thread(target=_worker,daemon=True,name="eight-rule-test").start()
@app.get("/compare-strategies")
def compare_status():return state
