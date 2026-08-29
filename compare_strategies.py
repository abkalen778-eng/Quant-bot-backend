from __future__ import annotations
import json, threading, time
from datetime import datetime,timedelta,timezone
import requests
from main import app

BASE="https://api.exchange.coinbase.com"
HEADERS={"User-Agent":"quant-spot-breakout/2.0"}
PRODUCTS=["BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD","ADA-USD","AVAX-USD","LINK-USD"]
DAYS=180
FEE=.004
RISK=.005


def fetch(p,g,s,e):
 rows={};cur=s;step=timedelta(seconds=g*280)
 while cur<e:
  x=min(cur+step,e)
  r=requests.get(f"{BASE}/products/{p}/candles",params={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":x.isoformat().replace("+00:00","Z")},headers=HEADERS,timeout=15)
  r.raise_for_status()
  for z in r.json():
   rows[int(z[0])] = {"time":float(z[0]),"low":float(z[1]),"high":float(z[2]),"open":float(z[3]),"close":float(z[4]),"volume":float(z[5])}
  cur=x;time.sleep(.06)
 return [rows[k] for k in sorted(rows)]


def metrics(ts):
 eq=peak=1.;dd=0.;w=0
 for t in ts:
  eq*=1+t;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak);w+=t>0
 return {"trades":len(ts),"wins":w,"losses":len(ts)-w,"win_rate_pct":round(100*w/len(ts),2) if ts else 0.,"return_pct":round((eq-1)*100,2),"max_drawdown_pct":round(dd*100,2)}


def test(daily,hourly,start):
 ts=[];trade_log=[];busy=0.
 for i in range(40,len(daily)):
  c=daily[i]
  if c["time"]<start or c["time"]<=busy:continue

  prior40=daily[i-40:i]
  resistance=max(x["high"] for x in prior40)
  avg10=sum(x["volume"] for x in daily[i-10:i])/10
  vol_ratio=c["volume"]/avg10 if avg10 else 0.

  # Looser consolidation requirement to catch more valid breakout setups.
  pre=daily[i-5:i]
  pre_high=max(x["high"] for x in pre);pre_low=min(x["low"] for x in pre)
  consolidation=(pre_high-pre_low)/max(pre_low,1e-9)
  near_resistance=(resistance-pre[-1]["close"])/resistance <= .08

  # v2: accept 1.5x volume and a close above the prior 40-day high.
  if vol_ratio<1.5 or consolidation>.08 or not near_resistance or c["close"]<=resistance:continue

  ep=c["close"]
  initial_stop=max(ep*.92,resistance*.975)
  risk_frac=(ep-initial_stop)/ep
  if risk_frac<=0:continue

  exposure=min(1.0,RISK/risk_frac)
  entry_time=c["time"]+86400
  peak=ep;stop=initial_stop;xp=None;xt=None;reason="open"
  max_hold=entry_time+45*86400

  for x in hourly:
   if x["time"]<entry_time:continue
   if x["time"]>max_hold:
    xp=x["open"];xt=x["time"];reason="45d_time_stop";break
   peak=max(peak,x["high"])
   # Start protecting gains earlier while still allowing large spot moves to run.
   if peak>=ep*1.12:stop=max(stop,peak*.92)
   if x["low"]<=stop:
    xp=stop;xt=x["time"];reason="stop_or_trail";break

  if xp is None:
   future=[x for x in hourly if x["time"]>=entry_time]
   if not future:continue
   xp=future[-1]["close"];xt=future[-1]["time"];reason="end_of_test"

  gross=((xp-ep)/ep)*exposure
  net=gross-2*FEE*exposure
  ts.append(net);busy=xt
  trade_log.append({"entry_time":int(entry_time),"entry":round(ep,8),"resistance":round(resistance,8),"volume_ratio":round(vol_ratio,2),"consolidation_pct":round(consolidation*100,2),"initial_stop":round(initial_stop,8),"exit":round(xp,8),"exit_reason":reason,"net_return_pct":round(net*100,2)})
 return metrics(ts),trade_log


def run():
 now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
 start=now-timedelta(days=DAYS)
 res=[]
 for p in PRODUCTS:
  try:
   daily=fetch(p,86400,start-timedelta(days=55),now)
   hourly=fetch(p,3600,start-timedelta(days=2),now)
   m,trades=test(daily,hourly,start.timestamp())
   res.append({"product":p,"high_volume_spot_breakout_v2":m,"trade_log":trades})
  except Exception as e:
   res.append({"product":p,"error":f"{type(e).__name__}: {e}"})
 return {"period_days":DAYS,"products":PRODUCTS,"results":res,"strategy":"high_volume_spot_breakout_v2","risk_per_trade_pct":0.5,"fee_bps_per_side":40.0,"rules":["spot long-only","prior 40-day high defines resistance","previous 5 days consolidate within 8% range and finish within 8% of resistance","breakout daily candle volume >= 1.5x prior 10-day average","buy after completed daily close above resistance","initial stop is tighter of 8% below entry or 2.5% below old resistance","risk 0.5% of account per trade with no leverage","after +12% gain trail 8% below peak","45-day maximum hold","fees included"]}

state={"status":"not_started","result":None,"error":None}
def worker():
 state["status"]="running"
 try:
  r=run();state["result"]=r;state["status"]="complete";print("HIGH_VOLUME_SPOT_BREAKOUT_V2_BACKTEST="+json.dumps(r),flush=True)
 except Exception as e:
  state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("HIGH_VOLUME_SPOT_BREAKOUT_V2_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def startup():threading.Thread(target=worker,daemon=True,name="high-volume-spot-breakout-v2-test").start()
@app.get("/compare-strategies")
def status():return state
