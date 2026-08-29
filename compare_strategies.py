from __future__ import annotations
import json,threading,time
from datetime import datetime,timedelta,timezone
import requests
from main import app
BASE="https://api.exchange.coinbase.com";HEADERS={"User-Agent":"quant-final-breakout/1.0"}
PRODUCTS=["BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD","ADA-USD","AVAX-USD","LINK-USD","LTC-USD","BCH-USD","DOT-USD","UNI-USD","AAVE-USD","ATOM-USD","NEAR-USD","ICP-USD","FIL-USD","ARB-USD","OP-USD","SUI-USD"]
DAYS=1095;FEE=.004;RISK=.005;SLIPPAGE=.001

def fetch(p,g,s,e):
 rows={};cur=s;step=timedelta(seconds=g*280)
 while cur<e:
  x=min(cur+step,e);r=requests.get(f"{BASE}/products/{p}/candles",params={"granularity":g,"start":cur.isoformat().replace("+00:00","Z"),"end":x.isoformat().replace("+00:00","Z")},headers=HEADERS,timeout=20);r.raise_for_status()
  for z in r.json():rows[int(z[0])] = {"time":float(z[0]),"low":float(z[1]),"high":float(z[2]),"open":float(z[3]),"close":float(z[4]),"volume":float(z[5])}
  cur=x;time.sleep(.03)
 return [rows[k] for k in sorted(rows)]

def metrics(ts):
 eq=peak=1.;dd=0.;w=0
 for t in ts:eq*=1+t;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak);w+=t>0
 return {"trades":len(ts),"wins":w,"losses":len(ts)-w,"win_rate_pct":round(100*w/len(ts),2) if ts else 0.,"return_pct":round((eq-1)*100,2),"max_drawdown_pct":round(dd*100,2)}

def test(daily,hourly,start):
 returns=[];log=[];busy=0.
 for i in range(40,len(daily)-1):
  c=daily[i];et=c["time"]+86400
  if et<start or et<=busy:continue
  res=max(x["high"] for x in daily[i-40:i]);avg10=sum(x["volume"] for x in daily[i-10:i])/10;vr=c["volume"]/avg10 if avg10 else 0.
  pre=daily[i-5:i];hi=max(x["high"] for x in pre);lo=min(x["low"] for x in pre);cons=(hi-lo)/max(lo,1e-9);near=(res-pre[-1]["close"])/res<=.08;bp=c["close"]/res-1
  if vr<2 or cons>.08 or not near or bp<.01:continue
  future=[x for x in hourly if x["time"]>=et]
  if not future:continue
  ep=future[0]["open"]*(1+SLIPPAGE);stop=max(ep*.92,res*.975);rf=(ep-stop)/ep
  if rf<=0:continue
  exposure=min(1.,RISK/rf);peak=ep;xp=xt=None;reason="open";mae=0.;mfe=0.;maxhold=et+45*86400
  for x in future:
   mae=max(mae,(ep-x["low"])/ep);mfe=max(mfe,(x["high"]-ep)/ep);peak=max(peak,x["high"])
   if x["time"]>maxhold:xp=x["open"]*(1-SLIPPAGE);xt=x["time"];reason="45d_time_stop";break
   if peak>=ep*1.12:stop=max(stop,peak*.92)
   if x["low"]<=stop:xp=stop*(1-SLIPPAGE);xt=x["time"];reason="stop_or_trail";break
  if xp is None:xp=future[-1]["close"]*(1-SLIPPAGE);xt=future[-1]["time"];reason="end_of_test"
  net=((xp-ep)/ep)*exposure-2*FEE*exposure;returns.append(net);busy=xt
  log.append({"entry_time":int(et),"entry":round(ep,8),"resistance":round(res,8),"breakout_pct":round(bp*100,2),"volume_ratio":round(vr,2),"consolidation_pct":round(cons*100,2),"exit":round(xp,8),"exit_reason":reason,"mae_pct":round(mae*100,2),"mfe_pct":round(mfe*100,2),"net_return_pct":round(net*100,2)})
 return metrics(returns),log

def run():
 now=datetime.now(timezone.utc).replace(second=0,microsecond=0);start=now-timedelta(days=DAYS);results=[];allr=[]
 for p in PRODUCTS:
  try:
   daily=fetch(p,86400,start-timedelta(days=60),now);hourly=fetch(p,3600,start-timedelta(days=2),now);m,tr=test(daily,hourly,start.timestamp());results.append({"product":p,"final_confirmed_breakout":m,"trade_log":tr});allr.extend([x["net_return_pct"]/100 for x in tr])
  except Exception as e:results.append({"product":p,"error":f"{type(e).__name__}: {e}"})
 return {"period_days":DAYS,"approx_years":3,"products":PRODUCTS,"aggregate_trade_metrics":metrics(allr),"results":results,"strategy":"final_confirmed_breakout_3y","risk_per_trade_pct":.5,"fee_bps_per_side":40.,"slippage_bps_per_side":10.,"rules":["spot long-only","20 assets","40-day resistance","5-day consolidation <=8%, prior close within 8% of resistance","volume >=2x prior 10-day average","daily close >=1% above resistance","next-hour-open entry","0.5% planned risk, no leverage","8%/2.5%-below-resistance stop","8% trail after +12%","45-day max hold","0.4% fee each side plus 0.1% slippage each side","MAE/MFE recorded"]}
state={"status":"not_started","result":None,"error":None}
def worker():
 state["status"]="running"
 try:r=run();state["result"]=r;state["status"]="complete";print("FINAL_CONFIRMED_BREAKOUT_3Y="+json.dumps(r),flush=True)
 except Exception as e:state["status"]="failed";state["error"]=f"{type(e).__name__}: {e}";print("FINAL_CONFIRMED_BREAKOUT_3Y_ERROR="+state["error"],flush=True)
@app.on_event("startup")
def startup():threading.Thread(target=worker,daemon=True,name="final-confirmed-breakout-3y").start()
@app.get("/compare-strategies")
def status():return state
