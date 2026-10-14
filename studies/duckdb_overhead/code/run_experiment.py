#!/usr/bin/env python3
"""Fixed-invocation DuckDB Python-UDF overhead diagnostic."""
import csv,hashlib,json,platform,random,statistics,sys,time
from pathlib import Path
import duckdb
from duckdb.typing import BIGINT

HERE=Path(__file__).resolve().parent; INVOCATIONS=10000; SEED=3605; RESAMPLES=10000
WORK=[0,1,10,100,1000]; counter={"calls":0,"work":0}
def kernel(v,units):
    counter["calls"]+=1; x=int(v)&0x7fffffff
    for j in range(units): x=(x*1103515245+12345+j)&0x7fffffff
    counter["work"]+=units; return x
def make(units): return lambda v: kernel(v,units)
def qtl(xs,p):
    ys=sorted(xs); z=(len(ys)-1)*p; a=int(z); b=min(a+1,len(ys)-1); return ys[a]*(b-z)+ys[b]*(z-a)
def summarize(xs,seed):
    mean=statistics.mean(xs); sd=statistics.stdev(xs) if len(xs)>1 else 0; rng=random.Random(seed); meds=[]; n=len(xs)
    for _ in range(RESAMPLES): meds.append(statistics.median(xs[rng.randrange(n)] for _ in range(n)))
    q1,q3=qtl(xs,.25),qtl(xs,.75)
    return dict(n=n,median_ms=statistics.median(xs),mean_ms=mean,iqr_ms=q3-q1,std_ms=sd,cv=sd/mean if mean else 0,min_ms=min(xs),max_ms=max(xs),ci_low_ms=qtl(meds,.025),ci_high_ms=qtl(meds,.975))
def main():
    for p in [HERE/"raw.csv",HERE/"normalized.csv",HERE/"summary.json"]:
        if p.exists(): raise RuntimeError(f"refusing to overwrite {p}")
    con=duckdb.connect(":memory:"); con.execute(f"CREATE TABLE inputs AS SELECT i::BIGINT AS i FROM range({INVOCATIONS}) t(i)")
    for u in WORK: con.create_function(f"work_{u}",make(u),[BIGINT],BIGINT,side_effects=True)
    cases=["native"]+[f"work_{u}" for u in WORK]; raw=[]; rng=random.Random(SEED)
    def run(case,phase,block,pos):
        counter.update(calls=0,work=0); sql="SELECT SUM((i*1103515245+12345)&2147483647) FROM inputs" if case=="native" else f"SELECT SUM({case}(i)) FROM inputs"
        t=time.perf_counter_ns(); result=con.execute(sql).fetchall(); ms=(time.perf_counter_ns()-t)/1e6
        units=None if case=="native" else int(case.split("_")[1]); expected_calls=0 if case=="native" else INVOCATIONS; expected_work=0 if case=="native" else INVOCATIONS*units
        valid=counter["calls"]==expected_calls and counter["work"]==expected_work
        h=hashlib.sha256(json.dumps(result,separators=(",",":"),default=str).encode()).hexdigest()
        raw.append(dict(phase=phase,block=block,order_position=pos,case=case,work_units_per_call="" if units is None else units,invocations=counter["calls"],exact_work=counter["work"],elapsed_ms=ms,semantic_counter_valid=valid,output_hash=h))
        if not valid: raise RuntimeError(f"counter mismatch {case}")
    for phase,blocks in [("warmup",3),("measured",10)]:
        for block in range(1,blocks+1):
            order=cases[:]; rng.shuffle(order)
            for pos,case in enumerate(order,1): run(case,phase,block,pos)
    norm=[]
    for i,case in enumerate(cases):
        xs=[r["elapsed_ms"] for r in raw if r["phase"]=="measured" and r["case"]==case]; s=summarize(xs,SEED+100+i); units="" if case=="native" else int(case.split("_")[1])
        norm.append(dict(case=case,work_units_per_call=units,invocations=0 if case=="native" else INVOCATIONS,runtime_per_invocation_us=s["median_ms"]*1000/INVOCATIONS,high_variance=s["cv"]>.10,diagnostic_n=0,diagnostic_median_ms="",diagnostic_cv="",**s))
    high=[r for r in norm if r["high_variance"]]
    if high:
        order=[r["case"] for r in high]
        for block in range(1,31):
            rng.shuffle(order)
            for pos,case in enumerate(order,1): run(case,"diagnostic30",block,pos)
        for i,r in enumerate(high):
            xs=[x["elapsed_ms"] for x in raw if x["phase"]=="diagnostic30" and x["case"]==r["case"]]; s=summarize(xs,SEED+500+i); r.update(diagnostic_n=30,diagnostic_median_ms=s["median_ms"],diagnostic_cv=s["cv"])
    med={r["case"]:r["median_ms"] for r in norm}; xs=WORK[1:]; ys=[med[f"work_{u}"] for u in xs]; xm=statistics.mean(xs); ym=statistics.mean(ys); slope=sum((x-xm)*(y-ym) for x,y in zip(xs,ys))/sum((x-xm)**2 for x in xs); intercept=ym-slope*xm; sst=sum((y-ym)**2 for y in ys); sse=sum((y-(intercept+slope*x))**2 for x,y in zip(xs,ys)); r2=1-sse/sst if sst else 0
    noop=med["work_0"]; native=med["native"]; fixed_share=noop/med["work_1"] if med["work_1"] else None
    classification="SUPPORTED" if fixed_share>=.5 and r2>=.9 else ("PARTIALLY SUPPORTED" if fixed_share>=.25 or r2>=.75 else "NOT SUPPORTED")
    summary=dict(classification=classification,invocations=INVOCATIONS,native_median_ms=native,noop_callback_median_ms=noop,fixed_callback_over_native_ms=noop-native,fixed_share_of_one_unit_runtime=fixed_share,ols_intercept_ms=intercept,incremental_ms_per_work_unit=slope,r_squared=r2,interpretation="An intercept/no-op comparison supports only a measured fixed Python-callback component; it does not identify undocumented DuckDB internals.")
    cols=list(raw[0]);
    with (HERE/"raw.csv").open("x",newline="",encoding="utf-8") as f: w=csv.DictWriter(f,fieldnames=cols); w.writeheader(); w.writerows(raw)
    cols=list(norm[0]);
    with (HERE/"normalized.csv").open("x",newline="",encoding="utf-8") as f: w=csv.DictWriter(f,fieldnames=cols); w.writeheader(); w.writerows(norm)
    (HERE/"summary.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n")
    (HERE/"environment.json").write_text(json.dumps(dict(platform=platform.platform(),python=sys.version,duckdb=duckdb.__version__,seed=SEED,bootstrap_resamples=RESAMPLES),indent=2,sort_keys=True)+"\n")
    print(json.dumps(summary,sort_keys=True))
if __name__=="__main__": main()
