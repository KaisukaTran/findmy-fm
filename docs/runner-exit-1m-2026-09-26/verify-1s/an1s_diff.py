import pickle, statistics as st, math, glob, sys, datetime as dt
SP=r"C:/Users/ADMINI~1/AppData/Local/Temp/claude/D--FINDMY/55e6a651-ee4f-4c40-990d-f031dad1376c/scratchpad"
D=pickle.load(open(SP+"/walk1s.pkl","rb")); meta=D["meta"]; rows=D["rows"]
R=[]
for f in glob.glob(SP+"/events2/*.pkl"): R+=pickle.load(open(f,"rb"))
P=[r for r in R if not r.get("mismatch") and r["bound"]=="PESS" and r["fixset"]=="orig"]
day=lambda ms: dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).strftime("%Y-%m-%d")
def pop(var,mode,excl=False):
    E=[r for r in P if not (excl and day(r["touch_ts"])=="2025-10-10")]
    k=(var,2.0,0.1,mode)
    return sum((r["res"][k][0] if k in r["res"] else r["v0"])-r["v0"] for r in E)/len(E), len(E)
def strata_N(excl):
    # recount stratum sizes in population (ex-crash if requested)
    key=lambda r,m: r["res"][("V4a",2.0,0.1,m)][0]
    E=[r for r in P if not (excl and day(r["touch_ts"])=="2025-10-10")]
    A=[r for r in P if abs(key(r,"Pf")-key(r,"Of"))>1e-9]; A.sort(key=lambda r:-abs(key(r,"Of")-key(r,"Pf")))
    A1ids={(r["sym"],r["start"]) for r in A[:60]}
    run=[r for r in P if ("V5",2.0,0.1,"P") in r["res"]]
    k5=lambda r,m:r["res"][("V5",2.0,0.1,m)][0]
    C=[r for r in run if abs(k5(r,"P")-k5(r,"Of"))>1e-9]; C.sort(key=lambda r:-abs(k5(r,"Of")-k5(r,"P")))
    C1ids={(r["sym"],r["start"]) for r in C[:30]}
    Eset={(r["sym"],r["start"]) for r in E}
    Aset={(r["sym"],r["start"]) for r in A}; Cset={(r["sym"],r["start"]) for r in C}; runset={(r["sym"],r["start"]) for r in run}
    return {"A1":len(A1ids&Eset),"A2":len((Aset-A1ids)&Eset),"B":len(Eset-Aset),
            "C1":len(C1ids&Eset),"C2":len((Cset-C1ids)&Eset),"D":len((runset-Cset)&Eset)}, len(E)
for excl in (False,True):
    Nh,N=strata_N(excl)
    print("=== ex-crash" if excl else "=== all", "N",N, Nh)
    for var,base,strata in (("V4a","Pf",("A1","A2","B")),("V4a","Of",("A1","A2","B")),("V5","P",("C1","C2","D"))):
        pb,_=pop(var,base,excl)
        for m in ("1sP","1sO","T2"):
            tot=0; v=0
            for h in strata:
                xs=[r["s1"][(var,m)][0]-r["m1"][(var,base)] for r in rows if r["stratum"]==h and not (excl and day(r["touch_ts"])=="2025-10-10")]
                if not xs: continue
                tot+=Nh[h]*st.mean(xs)
                if h not in ("A1","C1") and len(xs)>1: v+=Nh[h]**2*st.variance(xs)/len(xs)
            print(f"  {var} g2 s0.1: 1m-{base} pop {pb:+.3f} -> {m} {pb+tot/N:+.3f} ± {math.sqrt(v)/N:.3f} (1SE)")
