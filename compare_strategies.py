from __future__ import annotations
import json, threading, time
from datetime import datetime,timedelta,timezone
import requests
from main import app

PUBLIC_BASE="https://api.exchange.coinbase.com";HEADERS={"User-Agent":"quant-bot-trend-volume/1.0"};PRODUCTS=["BTC-USD","ETH-USD"];FEE_BPS=40.0;TEST_DAYS=30;STOP_PCT=.0075;TAKE_PROFIT_PCT=.015

def _fetch(product,g,start,end):
    rows={};cur=start;step=timedelta(seconds=g*280)
    while cur<end:
        e=min(cur+step,end);p={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":e.isoformat().replace("+00:00","Z")}
        r=requests.get(f"{PUBLIC_BASE}/products/{product}/candles",params=p,headers=HEADERS,timeout=15);r.raise_for_status()
        for x in r.json():rows[int(x[0])]={"time":float(x[0]),"low":float(x[1]),"high":float(x[2]),"open":float(x[3]),"close":float(x[4]),"volume":float(x[5])}
        cur=e;time.sleep(.07)
    return [rows[k] for k in sorted(rows)]

def _momentum_ok(hourly,t):
    a=[x["close"] for x in hourly if x["time"]<=t]
    if len(a)<50:return False
    s20=sum(a[-20:])/20;s50=sum(a[-50:])/50;ret6=a[-1]/a[-7]-1
    return a[-1]>s20>s50 and ret6>.002

def _volume_ok(five,i):
    if i<20:return False
    vols=[x["volume"] for x in five[i-20:i]];c=five[i]
    avg=sum(vols)/20
    return c["close"]>c["open"] and c["volume"]>=avg*1.10

def _metrics(trades):
    eq=peak=1.;dd=0.;wins=0
    for t in trades:
        eq*=1+t["net"];peak=max(peak,eq);dd=max(dd,(peak-eq)/peak);wins+=t["net"]>0
    return {"trades":len(trades),"wins":wins,"losses":len(trades)-wins,"win_rate_pct":round(100*wins/len(trades),2) if trades else 0.,"return_pct":round(100*(eq-1),2),"max_drawdown_pct":round(100*dd,2)}

def _test(one,five,hourly,start):
    fee=FEE_BPS/10000.;trades=[];busy=0.
    for i in range(20,len(five)-1):
        c=five[i]
        if c["time"]<start or c["time"]<=busy:continue
        if not _momentum_ok(hourly,c["time"]) or not _volume_ok(five,i):continue
        ep=c["close"];stop=ep*(1-STOP_PCT);tp=ep*(1+TAKE_PROFIT_PCT);xp=xt=None
        for x in one:
            if x["time"]<=c["time"]:continue
            if x["low"]<=stop:xp=stop;xt=x["time"];break
            if x["high"]>=tp:xp=tp;xt=x["time"];break
        if xp is None:xp=one[-1]["close"];xt=one[-1]["time"]
        trades.append({"net":xp/ep-1-2*fee});busy=xt
    return _metrics(trades)

def run_comparison():
    now=datetime.now(timezone.utc).replace(second=0,microsecond=0);start=now-timedelta(days=TEST_DAYS);results=[]
    for p in PRODUCTS:
        one=_fetch(p,60,start-timedelta(hours=2),now);five=_fetch(p,300,start-timedelta(hours=4),now);hourly=_fetch(p,3600,start-timedelta(days=4),now)
        results.append({"product":p,"trend_volume_strategy":_test(one,five,hourly,start.timestamp())})
    return {"period_days":TEST_DAYS,"fee_bps_per_side":FEE_BPS,"products":PRODUCTS,"results":results,"strategy":"trend_volume_v1_no_tjr","rules":["BTC/ETH only","hourly price > 20h SMA > 50h SMA","6h momentum > 0.2%","bullish 5m candle with volume >= 110% of prior 20-bar average","0.75% stop loss","1.5% take profit"],"note":"Standalone long-only trend/volume simulation. TJR/liquidity rules are not used."}

state={"status":"not_started","result":None,"error":None}
def _worker():
    state["status"]="running"
    try:
        r=run_comparison();state["result"]=r;state["status"]="complete";print("NO_TJR_BACKTEST="+json.dumps(r),flush=True)
    except Exception as e:state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("NO_TJR_BACKTEST_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def start_comparison():threading.Thread(target=_worker,daemon=True,name="no-tjr-backtest").start()
@app.get("/compare-strategies")
def compare_status():return state
