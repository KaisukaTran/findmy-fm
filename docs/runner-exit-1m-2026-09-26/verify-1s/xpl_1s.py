import json, sys, time, numpy as np, datetime as dt
SP=r"C:/Users/ADMINI~1/AppData/Local/Temp/claude/D--FINDMY/55e6a651-ee4f-4c40-990d-f031dad1376c/scratchpad"
sys.path.insert(0,SP); sys.path.insert(0,"D:/FINDMY")
import walk1s
K=json.load(open(SP+"/xpl_1m.json")); now=int(time.time()*1000)
K=[k for k in K if k[6]<now]
m1={"ts":np.array([k[0] for k in K],dtype=np.int64)}
for i,f in ((1,"open"),(2,"high"),(3,"low"),(4,"close")): m1[f]=np.array([float(k[i]) for k in K])
AVG=0.09280176777477368; QTY=1645.9; TP=0.09859259808391956; FEE=0.001
cost=QTY*AVG*(1+FEE); v0p=QTY*TP*(1-FEE); v0=v0p-cost
n=len(K); s=int(np.nonzero(m1["high"]>=TP)[0][0]); dl=n  # deadline beyond data
iso=lambda ms: dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).strftime('%m-%d %H:%M:%S')
secs=walk1s.seconds_of_minute("XPLUSDT",int(m1["ts"][s]))
k0=next(i for i,r in enumerate(secs) if r[2]>=TP)
print("touch second",iso(secs[k0][0]),secs[k0], "min low after touch in m0", min(r[3] for r in secs[k0:]), "max high", max(r[2] for r in secs[k0:]))
for g in (2.0,3.0,5.0):
  for slip in (0.1,0.3):
    out=[]
    for model in ("1sP","1sO","T2"):
        kind,base,ets=walk1s.walk_1s("XPLUSDT",m1,s,n-1,n,dl,TP,AVG*1.03,g,model,touch_px=TP)
        sl=slip/100 if kind=="stop" else 0
        v4=QTY*base*(1-sl)*(1-FEE)-cost
        d=s+2; dp=float(m1["open"][d]); usd=min(v0p, v0/(g/100+0.032)); q=usd/(dp*(1+slip/100)*(1+FEE))
        k5,b5,e5=walk1s.walk_1s("XPLUSDT",m1,d,n-1,n,dl,dp,0.0,g,model)
        sl5=slip/100 if k5=="stop" else 0
        v5=v0+q*b5*(1-sl5)*(1-FEE)-usd
        out.append(f"{model}: V4a {v4:+.3f} ({kind} {iso(ets)} @{base:.5f}) V5 {v5:+.3f} ({k5} {iso(e5)} @{b5:.5f})")
    print(f"g{g} slip{slip}"); [print("   ",o) for o in out]
print("V0",round(v0,4))
