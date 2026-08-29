from __future__ import annotations
import json, threading, time
from datetime import datetime,timedelta,timezone
import requests
from main import app

BASE="https://api.exchange.coinbase.com"
HEADERS={"User-Agent":"quant-spot-breakout/4.0"}
PRODUCTS=["BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD","ADA-USD","AVAX-USD","LINK-USD","LTC-USD","BCH-USD","DOT-USD","UNI-USD","AAVE-USD","ATOM-USD","NEAR-USD","ICP-USD","FIL-USD","ARB-USD","OP-USD","SUI-USD"]
DAYS=365
FEE=.004
RISK=.005

def fetch(p,g,s,e):
 rows={};cur=s;step=timedelta(seconds=g*280)
 while cur<e:
  x=min(cur+step,e)
  r=requests.get(f"{BASE}/products/{p}/candles",params={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":x.isoformat().replace("+00:00","Z")},headers=HEADERS,timeout=15)
  r.raise_for_status()
  for z in r.json():rows[int(z[0])] = {"time":float(z[0]),"low":float(z[1]),"high":float(z[2]),"open":float(z[3]),"close":float(z[4]),"volume":float(z[5])}
  cur=x;time.sleep(.04)
 return [rows[k] for k in sorted(rows)]

def metrics(ts):
 eq=peak=1.;dd=0.;w=0
 for t in ts:eq*=1+t;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak);w+=t>0
 return {"trades":len(ts),"wins":w,"losses":len(ts)-w,"win_rate_pct":round(100*w/len(ts),2) if ts else 0.,"return_pct":round((eq-1)*100,2),"max_drawdown_pct":round(dd*100,2)}

def test(daily,hourly,start):
 ts=[];log=[];busy=0.
 for i in range(40,len(daily)-1):
  c=daily[i];entry_time=c["time"]+86400
  if entry_time<start or entry_time<=busy:continue
  resistance=max(x["high"] for x in daily[i-40:i])
  avg10=sum(x["volume"] for x in daily[i-10:i])/10
  vr=c["volume"]/avg10 if avg10 else 0.
  pre=daily[i-5:i];hi=max(x["high"] for x in pre);lo=min(x["low"] for x in pre)
  cons=(hi-lo)/max(lo,1e-9);near=(resistance-pre[-1]["close"])/resistance<=.08
  breakout=c["close"]/resistance-1
  if vr<2.0 or cons>.08 or not near or breakout<.01:continue
  future=[x for x in hourly if x["time"]>=entry_time]
  if not future:continue
  ep=future[0]["open"];stop=max(ep*.92,resistance*.975);rf=(ep-stop)/ep
  if rf<=0:continue
  exposure=min(1.,RISK/rf);peak=ep;xp=xt=None;reason="open";max_hold=entry_time+45*86400
  for x in future:
   if x["time"]>max_hold:xp=x["open"];xt=x["time"];reason="45d_time_stop";break
   peak=max(peak,x["high"])
   if peak>=ep*1.12:stop=max(stop,peak*.92)
   if x["low"]<=stop:xp=stop;xt=x["time"];reason="stop_or_trail";break
  if xp is None:xp=future[-1]["close"];xt=future[-1]["time"];reason="end_of_test"
  net=((xp-ep)/ep)*exposure-2*FEE*exposure;ts.append(net);busy=xt
  log.append({"entry_time":int(entry_time),"entry":round(ep,8),"resistance":round(resistance,8),"breakout_pct":round(breakout*100,2),"volume_ratio":round(vr,2),"consolidation_pct":round(cons*100,2),"exit":round(xp,8),"exit_reason":reason,"net_return_pct":round(net*100,2)})
 return metrics(ts),log

def run():
 now=datetime.now(timezone.utc).replace(second=0,microsecond=0);start=now-timedelta(days=DAYS);res=[];all_returns=[]
 for p in PRODUCTS:
  try:
   daily=fetch(p,86400,start-timedelta(days=60),now);hourly=fetch(p,3600,start-timedelta(days=2),now)
   m,trades=test(daily,hourly,start.timestamp());res.append({"product":p,"confirmed_breakout":m,"trade_log":trades})
   all_returns.extend([t["net_return_pct"]/100 for t in trades])
  except Exception as e:res.append({"product":p,"error":f"{type(e).__name__}: {e}"})
 return {"period_days":DAYS,"products":PRODUCTS,"aggregate_trade_metrics":metrics(all_returns),"results":res,"strategy":"confirmed_breakout_second_test","risk_per_trade_pct":0.5,"fee_bps_per_side":40.,"rules":["spot long-only","20-asset universe","prior 40-day high resistance","5-day consolidation <=8% and prior close within 8% of resistance","daily breakout volume >=2x prior 10-day average","daily close >=1% above resistance","entry at next available hourly open","risk 0.5% per trade, no leverage","initial stop tighter of 8% below entry or 2.5% below resistance","trail 8% after +12%","45-day maximum hold","fees included"]}
state={"status":"not_started","result":None,"error":None}
def worker():
 state["status"]="running"
 try:
  r=run();state["result"]=r;state["status"]="complete";print("CONFIRMED_BREAKOUT_SECOND_BACKTEST="+json.dumps(r),flush=True)
 except Exception as e:state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("CONFIRMED_BREAKOUT_SECOND_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def startup():threading.Thread(target=worker,daemon=True,name="confirmed-breakout-second-test").start()
@app.get("/compare-strategies")
def status():return state
