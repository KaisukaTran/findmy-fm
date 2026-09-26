import sys, pickle, glob, statistics as st, datetime as dt
sys.path.insert(0,r"C:/Users/ADMINI~1/AppData/Local/Temp/claude/D--FINDMY/55e6a651-ee4f-4c40-990d-f031dad1376c/scratchpad")
from common import boot, SP
R=[]
for f in glob.glob(SP+"/events2/*.pkl"): R+=pickle.load(open(f,"rb"))
P=[r for r in R if not r.get("mismatch") and r["bound"]=="PESS" and r["fixset"]=="orig"]
day=lambda ms: dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).strftime("%Y-%m-%d")
def dedup(E):
    seen={}
    for r in E:
        k=(r["sym"],r["touch_hour"])
        if k not in seen or r["start"]<seen[k]["start"]: seen[k]=r
    return list(seen.values())
def cell(E,var,g,slip,mode):
    k=(var,g,slip,mode)
    d=[((r["res"][k][0] if k in r["res"] else r["v0"])-r["v0"]) for r in E]
    lo,hi=boot([(r["sym"],x) for r,x in zip(E,d)])
    viol=sum(1 for r,x in zip(E,d) if x+r["v0"]<0)
    return f"{st.mean(d):+.3f} [{lo:+.3f},{hi:+.3f}] v{viol}"
print("PESS ladder N",len(P),"V0 mean %+.3f"%st.mean(r["v0"] for r in P))
print("| variant | g | slip | PESS-order (Pf / P for V5) | OPT-order (Of) |")
for var in ("V4a","V5"):
    for g in (2.0,3.0,5.0):
        for slip in (0.1,0.3):
            pm="Pf" if var=="V4a" else "P"
            print(f"| {var} | {g:g} | {slip} | {cell(P,var,g,slip,pm)} | {cell(P,var,g,slip,'Of')} |")
D=dedup(P); X=[r for r in P if day(r["touch_ts"])!="2025-10-10"]
print("dedup N",len(D),"V0 %+.3f"%st.mean(r["v0"] for r in D),"; ex-crash N",len(X),"V0 %+.3f"%st.mean(r["v0"] for r in X))
for lab,E in (("dedup",D),("ex-crash",X)):
    for var in ("V4a","V5"):
        for slip in (0.1,0.3):
            pm="Pf" if var=="V4a" else "P"
            print(f"| {lab} {var} | 2 | {slip} | {cell(E,var,2.0,slip,pm)} | {cell(E,var,2.0,slip,'Of')} |")
