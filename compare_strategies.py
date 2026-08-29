from __future__ import annotations
import json, threading, time
from datetime import datetime,timedelta,timezone
import requests
from main import app

BASE="https://api.exchange.coinbase.com";HEADERS={"User-Agent":"quant-trend-pullback/1.0"};PRODUCTS=["BTC-USD","ETH-USD"];DAYS=30;FEE=.004;RISK=.005

def fetch(p,g,s,e):
 rows={};cur=s;step=timedelta(seconds=g*280)
 while cur<e:
  x=min(cur+step,e);r=requests.get(f"{BASE}/products/{p}/candles",params={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":x.isoformat().replace("+00:00","Z")},headers=HEADERS,timeout=15);r.raise_for_status()
  for z in r.json():rows[int(z[0])]={"time":float(z[0]),"low":float(z[1]),"high":float(z[2]),"open":float(z[3]),"close":float(z[4]),"volume":float(z[5])}
  cur=x;time.sleep(.07)
 return [rows[k] for k in sorted(rows)]

def ema(a,n):
 if not a:return []
 k=2/(n+1);out=[a[0]]
 for v in a[1:]:out.append(v*k+out[-1]*(1-k))
 return out

def atr(a,n=14):
 if len(a)<n+1:return None
 tr=[]
 for i in range(1,len(a)):tr.append(max(a[i]["high"]-a[i]["low"],abs(a[i]["high"]-a[i-1]["close"]),abs(a[i]["low"]-a[i-1]["close"])))
 return sum(tr[-n:])/n

def metrics(ts):
 eq=peak=1.;dd=0.;w=0
 for t in ts:eq*=1+t;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak);w+=t>0
 return {"trades":len(ts),"wins":w,"losses":len(ts)-w,"win_rate_pct":round(100*w/len(ts),2) if ts else 0.,"return_pct":round((eq-1)*100,2),"max_drawdown_pct":round(dd*100,2)}

def test(one,fifteen,hourly,four,start):
 closes4=[x["close"] for x in four];e200=ema(closes4,200);ts=[];busy=0.;cool=0.
 for i in range(55,len(hourly)-1):
  h=hourly[i];t=h["time"]
  if t<start or t<=busy or t<cool:continue
  fi=max((j for j,x in enumerate(four) if x["time"]<=t),default=-1)
  if fi<199 or four[fi]["close"]<=e200[fi]:continue
  hc=[x["close"] for x in hourly[:i+1]];e20=ema(hc,20)[-1];e50=ema(hc,50)[-1]
  if e20<=e50:continue
  a=atr(hourly[:i+1]);
  if not a:continue
  # pullback: hourly candle reaches 20/50 EMA zone but closes above the 50 EMA
  if h["low"]>max(e20,e50)*1.003 or h["close"]<e50:continue
  candidates=[(j,x) for j,x in enumerate(fifteen) if h["time"]<=x["time"]<h["time"]+3600]
  entry=None
  for j,c in candidates:
   if j<20:continue
   av=sum(x["volume"] for x in fifteen[j-20:j])/20
   prev=max(x["high"] for x in fifteen[max(0,j-3):j])
   if c["close"]>c["open"] and c["close"]>prev and c["volume"]>av:
    entry=c;break
  if not entry:continue
  ep=entry["close"];stop=ep-1.5*a;r=ep-stop
  if r<=0:continue
  tp=ep+2*r;xp=xt=None
  for x in one:
   if x["time"]<=entry["time"]:continue
   if x["low"]<=stop:xp=stop;xt=x["time"];break
   if x["high"]>=tp:xp=tp;xt=x["time"];break
  if xp is None:xp=one[-1]["close"];xt=one[-1]["time"]
  gross=(xp-ep)/r*RISK
  # approximate round-trip Coinbase fee drag on full position sized from stop distance
  exposure=RISK/(r/ep);net=gross-2*FEE*exposure
  ts.append(net);busy=xt
  if xp<=stop:cool=xt+3600
 return metrics(ts)

def run():
 now=datetime.now(timezone.utc).replace(second=0,microsecond=0);start=now-timedelta(days=DAYS);res=[]
 for p in PRODUCTS:
  one=fetch(p,60,start-timedelta(hours=2),now);fifteen=fetch(p,900,start-timedelta(days=2),now);hourly=fetch(p,3600,start-timedelta(days=5),now);four=fetch(p,21600,start-timedelta(days=40),now)
  res.append({"product":p,"quant_trend_pullback_v1":test(one,fifteen,hourly,four,start.timestamp())})
 return {"period_days":DAYS,"products":PRODUCTS,"results":res,"strategy":"quant_trend_pullback_v1","risk_per_trade_pct":0.5,"rules":["4H close above 200 EMA","1H 20 EMA above 50 EMA","pullback into 1H 20/50 EMA zone","15m bullish break above recent highs with above-average volume","1.5 ATR stop","2R target","1h cooldown after stopped trade","fees included","spot long-only"]}
state={"status":"not_started","result":None,"error":None}
def worker():
 state["status"]="running"
 try:
  r=run();state["result"]=r;state["status"]="complete";print("TREND_PULLBACK_BACKTEST="+json.dumps(r),flush=True)
 except Exception as e:state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("TREND_PULLBACK_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def startup():threading.Thread(target=worker,daemon=True,name="trend-pullback-test").start()
@app.get("/compare-strategies")
def status():return state
