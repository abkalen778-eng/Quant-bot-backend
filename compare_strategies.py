from __future__ import annotations
import json, threading, time
from datetime import datetime,timedelta,timezone
import requests
from main import app

BASE="https://api.exchange.coinbase.com"
HEADERS={"User-Agent":"quant-spot-breakout/3.0"}
PRODUCTS=["BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD","ADA-USD","AVAX-USD","LINK-USD"]
DAYS=365
FEE=.004
RISK=.005

VARIANTS={
 "v3_volume_2x":{"volume":2.0,"confirm":0.0,"trend":False},
 "v3_volume_2x_confirm":{"volume":2.0,"confirm":0.01,"trend":False},
 "v3_volume_2x_confirm_trend":{"volume":2.0,"confirm":0.01,"trend":True},
}

def fetch(p,g,s,e):
 rows={};cur=s;step=timedelta(seconds=g*280)
 while cur<e:
  x=min(cur+step,e)
  r=requests.get(f"{BASE}/products/{p}/candles",params={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":x.isoformat().replace("+00:00","Z")},headers=HEADERS,timeout=15)
  r.raise_for_status()
  for z in r.json():
   rows[int(z[0])] = {"time":float(z[0]),"low":float(z[1]),"high":float(z[2]),"open":float(z[3]),"close":float(z[4]),"volume":float(z[5])}
  cur=x;time.sleep(.05)
 return [rows[k] for k in sorted(rows)]

def sma(vals,n):
 return sum(vals[-n:])/n if len(vals)>=n else None

def metrics(ts):
 eq=peak=1.;dd=0.;w=0
 for t in ts:
  eq*=1+t;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak);w+=t>0
 return {"trades":len(ts),"wins":w,"losses":len(ts)-w,"win_rate_pct":round(100*w/len(ts),2) if ts else 0.,"return_pct":round((eq-1)*100,2),"max_drawdown_pct":round(dd*100,2)}

def test(daily,hourly,start,cfg):
 ts=[];trade_log=[];busy=0.
 for i in range(200,len(daily)-1):
  c=daily[i]
  signal_close_time=c["time"]+86400
  if signal_close_time<start or signal_close_time<=busy:continue

  prior40=daily[i-40:i]
  resistance=max(x["high"] for x in prior40)
  avg10=sum(x["volume"] for x in daily[i-10:i])/10
  vol_ratio=c["volume"]/avg10 if avg10 else 0.

  pre=daily[i-5:i]
  pre_high=max(x["high"] for x in pre);pre_low=min(x["low"] for x in pre)
  consolidation=(pre_high-pre_low)/max(pre_low,1e-9)
  near_resistance=(resistance-pre[-1]["close"])/resistance <= .08
  confirmed=c["close"] >= resistance*(1+cfg["confirm"])

  trend_ok=True
  if cfg["trend"]:
   prior_closes=[x["close"] for x in daily[:i]]
   ma50=sma(prior_closes,50);ma200=sma(prior_closes,200)
   prev50=sma(prior_closes[:-10],50) if len(prior_closes)>=60 else None
   trend_ok=bool(ma50 and ma200 and prev50 and pre[-1]["close"]>ma50>ma200 and ma50>prev50)

  if vol_ratio<cfg["volume"] or consolidation>.08 or not near_resistance or not confirmed or not trend_ok:continue

  entry_time=signal_close_time
  future=[x for x in hourly if x["time"]>=entry_time]
  if not future:continue
  ep=future[0]["open"]
  initial_stop=max(ep*.92,resistance*.975)
  risk_frac=(ep-initial_stop)/ep
  if risk_frac<=0:continue

  exposure=min(1.0,RISK/risk_frac)
  peak=ep;stop=initial_stop;xp=None;xt=None;reason="open"
  max_hold=entry_time+45*86400

  for x in future:
   if x["time"]>max_hold:
    xp=x["open"];xt=x["time"];reason="45d_time_stop";break
   peak=max(peak,x["high"])
   if peak>=ep*1.12:stop=max(stop,peak*.92)
   if x["low"]<=stop:
    xp=stop;xt=x["time"];reason="stop_or_trail";break

  if xp is None:
   xp=future[-1]["close"];xt=future[-1]["time"];reason="end_of_test"

  gross=((xp-ep)/ep)*exposure
  net=gross-2*FEE*exposure
  ts.append(net);busy=xt
  trade_log.append({"entry_time":int(entry_time),"entry":round(ep,8),"signal_close":round(c["close"],8),"resistance":round(resistance,8),"breakout_pct":round((c["close"]/resistance-1)*100,2),"volume_ratio":round(vol_ratio,2),"consolidation_pct":round(consolidation*100,2),"initial_stop":round(initial_stop,8),"exit":round(xp,8),"exit_reason":reason,"net_return_pct":round(net*100,2)})
 return metrics(ts),trade_log

def run():
 now=datetime.now(timezone.utc).replace(second=0,microsecond=0)
 start=now-timedelta(days=DAYS)
 res=[]
 for p in PRODUCTS:
  try:
   daily=fetch(p,86400,start-timedelta(days=220),now)
   hourly=fetch(p,3600,start-timedelta(days=2),now)
   row={"product":p}
   for name,cfg in VARIANTS.items():
    m,trades=test(daily,hourly,start.timestamp(),cfg)
    row[name]=m;row[name+"_trade_log"]=trades
   res.append(row)
  except Exception as e:
   res.append({"product":p,"error":f"{type(e).__name__}: {e}"})
 return {"period_days":DAYS,"products":PRODUCTS,"results":res,"strategy":"high_volume_spot_breakout_v3_grid","risk_per_trade_pct":0.5,"fee_bps_per_side":40.0,"variants":VARIANTS,"rules":["spot long-only","prior 40-day high defines resistance","previous 5 days consolidate within 8% range and finish within 8% of resistance","variant volume threshold is 2x prior 10-day average","confirmation variant requires daily close >=1% above resistance","trend variant additionally requires prior-day price > 50-day SMA > 200-day SMA with rising 50-day SMA","signal uses completed daily candle only; entry uses next available hourly open","initial stop is tighter of 8% below entry or 2.5% below old resistance","risk 0.5% of account per trade with no leverage","after +12% gain trail 8% below peak","45-day maximum hold","fees included"]}

state={"status":"not_started","result":None,"error":None}
def worker():
 state["status"]="running"
 try:
  r=run();state["result"]=r;state["status"]="complete";print("HIGH_VOLUME_SPOT_BREAKOUT_V3_BACKTEST="+json.dumps(r),flush=True)
 except Exception as e:
  state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("HIGH_VOLUME_SPOT_BREAKOUT_V3_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def startup():threading.Thread(target=worker,daemon=True,name="high-volume-spot-breakout-v3-test").start()
@app.get("/compare-strategies")
def status():return state
