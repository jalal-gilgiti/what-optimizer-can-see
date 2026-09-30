#!/usr/bin/env python3
"""PostgreSQL scalar COST decision-reach study with richer natural alternatives."""
import csv,hashlib,json,os,platform,random,statistics,subprocess,sys,time
from pathlib import Path
import psycopg2

SOURCE=Path(__file__).resolve().parents[1]; HERE=Path(os.environ.get("STUDY_OUTPUT_DIR",SOURCE)).resolve()
PG_BIN=Path(os.environ.get("PG_BIN","/usr/lib/postgresql/14/bin")); PG_LIB=Path(os.environ.get("PG_LIB","/usr/lib/x86_64-linux-gnu")); PG_SHARE=Path(os.environ.get("PG_SHARE","/usr/share/postgresql/14")); PG_PKGLIB=Path(os.environ.get("PG_PKGLIB","/usr/lib/postgresql/14/lib"))
RUNTIME=Path(os.environ.get("STUDY_RUNTIME_DIR","/tmp/pg-cost-grid-runtime")); DATA_DIR=Path("/tmp/pg-cost-grid-data"); SOCKET=Path("/tmp/pg-cost-grid-socket"); PORT=55450
COSTS=[1,10,100,1000,10000,100000]; SELECTIVITIES=[("s001",100), ("s010",1000), ("s050",5000)]; N=10000; SEED=3604
UDF_BODY='''\nresult = int(value) & 0x7fffffff\nfor j in range(10):\n    result = (result * 1103515245 + 12345 + j) & 0x7fffffff\nGD["calls"] = GD.get("calls", 0) + 1\nGD["work"] = GD.get("work", 0) + 10\nreturn (result >= 0)\n'''
QUERIES={
 "D1_QUALIFIERS":"SELECT count(*)::bigint,sum(id)::numeric FROM fact WHERE bucket < {threshold} AND opaque_pred(id)",
 "D2_INDEXED":"SELECT count(*)::bigint,sum(id)::numeric FROM fact WHERE bucket < {threshold} AND opaque_pred(id)",
 "D3_JOIN":"SELECT count(*)::bigint,sum(f.id)::numeric FROM fact f JOIN dim d ON d.id=f.dim_id WHERE d.bucket < {threshold_dim} AND opaque_pred(d.id)"
}
def connect(db="postgres"): return psycopg2.connect(host=str(SOCKET),port=PORT,dbname=db,user=os.environ.get("USER","experiment"),connect_timeout=5)
def qtl(xs,p): ys=sorted(xs); z=(len(ys)-1)*p; a=int(z); b=min(a+1,len(ys)-1); return ys[a]*(b-z)+ys[b]*(z-a)
def stats(xs,seed):
 mean=statistics.mean(xs); sd=statistics.stdev(xs) if len(xs)>1 else 0; q1,q3=qtl(xs,.25),qtl(xs,.75); rng=random.Random(seed); meds=[statistics.median(xs[rng.randrange(len(xs))] for _ in xs) for _ in range(10000)]
 return dict(n=len(xs),median_ms=statistics.median(xs),mean_ms=mean,iqr_ms=q3-q1,std_ms=sd,cv=sd/mean if mean else 0,min_ms=min(xs),max_ms=max(xs),ci_low_ms=qtl(meds,.025),ci_high_ms=qtl(meds,.975))
def nodes(n):
 yield n
 for c in n.get("Plans",[]): yield from nodes(c)
def signature(plan):
 p=plan["Plan"]; obj=[(n.get("Node Type"),n.get("Relation Name"),n.get("Index Name"),n.get("Join Type"),n.get("Filter"),n.get("Index Cond")) for n in nodes(p)]; raw=json.dumps(obj,sort_keys=True,separators=(",",":")); return hashlib.sha256(raw.encode()).hexdigest()," > ".join(x[0] for x in obj)
def output_hash(rows): return hashlib.sha256(json.dumps([[str(v) if v is not None else None for v in r] for r in rows],sort_keys=True,separators=(",",":")).encode()).hexdigest()
def setup(c):
 c.autocommit=True
 with c.cursor() as x:
  x.execute("CREATE EXTENSION plpython3u")
  x.execute(f"CREATE FUNCTION opaque_pred(value bigint) RETURNS boolean LANGUAGE plpython3u IMMUTABLE STRICT PARALLEL UNSAFE COST 1 AS $PY${UDF_BODY}$PY$")
  x.execute('''CREATE FUNCTION reset_metrics() RETURNS void LANGUAGE plpython3u AS $$ GD["calls"]=0; GD["work"]=0 $$''')
  x.execute('''CREATE FUNCTION read_metrics() RETURNS text LANGUAGE plpython3u AS $$ import json; return json.dumps({"calls":GD.get("calls",0),"work":GD.get("work",0)}) $$''')
  x.execute(f"CREATE TABLE fact AS SELECT i::bigint id,(i%10000)::int bucket,(i%1000)::int dim_id FROM generate_series(0,{N-1}) i")
  x.execute("CREATE TABLE dim AS SELECT i::int id,(i%1000)::int bucket FROM generate_series(0,999) i")
  x.execute("ALTER TABLE dim ADD PRIMARY KEY(id); CREATE INDEX fact_bucket_idx ON fact(bucket); CREATE INDEX fact_dim_idx ON fact(dim_id); CREATE INDEX dim_bucket_idx ON dim(bucket); ANALYZE fact; ANALYZE dim")
def set_session(c,cost):
 with c.cursor() as x:
  x.execute("SET jit=off; SET max_parallel_workers_per_gather=0; SET enable_seqscan=on; SET enable_indexscan=on; SET enable_bitmapscan=on; SET enable_hashjoin=on; SET enable_mergejoin=on; SET enable_nestloop=on")
  x.execute(f"ALTER FUNCTION opaque_pred(bigint) COST {cost}")
def query(family,threshold):
 return QUERIES[family].format(threshold=threshold,threshold_dim=max(1,threshold//10))
def run_one(c,family,sel,threshold,cost,analyze):
 set_session(c,cost); sql=query(family,threshold)
 with c.cursor() as x:
  x.execute("SELECT reset_metrics()")
  opts="ANALYZE TRUE, TIMING FALSE, SUMMARY TRUE, COSTS TRUE, FORMAT JSON" if analyze else "COSTS TRUE, VERBOSE TRUE, FORMAT JSON"
  x.execute(f"EXPLAIN ({opts}) {sql}"); plan=x.fetchone()[0][0]; x.execute("SELECT read_metrics()"); m=json.loads(x.fetchone()[0])
 return plan,m
def main():
 for p in [HERE/"raw.csv",HERE/"normalized.csv",HERE/"planning_grid.csv",HERE/"semantic_validation.csv"]:
  if p.exists(): raise RuntimeError(f"refusing to overwrite {p}")
 if RUNTIME.exists() or DATA_DIR.exists() or SOCKET.exists(): raise RuntimeError("runtime/data/socket exists")
 data=DATA_DIR; logs=RUNTIME/"logs"; plans=HERE/"plans"; data.mkdir(parents=True); logs.mkdir(parents=True); plans.mkdir(exist_ok=True); SOCKET.mkdir(mode=0o700)
 env=os.environ.copy(); env["LD_LIBRARY_PATH"]=str(PG_LIB)+(":"+env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
 log=(logs/"postgres.log").open("x"); proc=None; c=None; raw=[]; grid=[]; sem=[]
 try:
  subprocess.run([str(PG_BIN/"initdb"),"-D",str(data),"-L",str(PG_SHARE),"--encoding=UTF8","--locale=C.UTF-8","--auth-local=trust","--auth-host=reject",f"--username={os.environ.get('USER','experiment')}"],env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
  with (data/"postgresql.conf").open("a") as f: f.write(f"\nlisten_addresses=''\nport={PORT}\nunix_socket_directories='{SOCKET}'\nunix_socket_permissions=0700\ndynamic_library_path='{PG_PKGLIB}'\njit=off\nmax_parallel_workers=0\nmax_parallel_workers_per_gather=0\n")
  proc=subprocess.Popen([str(PG_BIN/"postgres"),"-D",str(data)],env=env,stdout=log,stderr=subprocess.STDOUT,text=True)
  for _ in range(200):
   try: a=connect(); a.close(); break
   except Exception: time.sleep(.1)
  else: raise RuntimeError("startup timeout")
  a=connect(); a.autocommit=True
  with a.cursor() as x:x.execute("CREATE DATABASE task36_cost")
  a.close(); c=connect("task36_cost"); setup(c)
  # Full predefined planning grid before timing.
  for fam in QUERIES:
   for sel,thr in SELECTIVITIES:
    for cost in COSTS:
     plan,m=run_one(c,fam,sel,thr,cost,False); sig,label=signature(plan); root=plan["Plan"]
     grid.append(dict(family=fam,selectivity=sel,threshold=thr,cost=cost,plan_signature=sig,plan_nodes=label,estimated_total_cost=root["Total Cost"],estimated_rows=root["Plan Rows"],filter_text=" | ".join(str(n.get("Filter")) for n in nodes(root) if n.get("Filter")),udf_calls_during_explain=m["calls"]))
     (plans/f"{fam}_{sel}_cost{cost}.json").write_text(json.dumps(plan,indent=2,sort_keys=True)+"\n")
  # Semantic gate per cell.
  for fam in QUERIES:
   for sel,thr in SELECTIVITIES:
    expected=None
    for cost in COSTS:
     set_session(c,cost)
     with c.cursor() as x:x.execute(query(fam,thr)); rows=x.fetchall()
     h=output_hash(rows); expected=expected or rows; valid=rows==expected; sem.append(dict(family=fam,selectivity=sel,cost=cost,semantic_valid=valid,output_hash=h,aggregate_count=rows[0][0],aggregate_sum=rows[0][1]))
     if not valid: raise RuntimeError("semantic mismatch")
  cells=[(f,s,t,cost) for f in QUERIES for s,t in SELECTIVITIES for cost in COSTS]; rng=random.Random(SEED)
  for phase,blocks in [("warmup",3),("measured",10)]:
   for block in range(1,blocks+1):
    order=cells[:]; rng.shuffle(order)
    for pos,(fam,sel,thr,cost) in enumerate(order,1):
     plan,m=run_one(c,fam,sel,thr,cost,True); sig,label=signature(plan)
     raw.append(dict(phase=phase,block=block,order_position=pos,family=fam,selectivity=sel,threshold=thr,cost=cost,plan_signature=sig,plan_nodes=label,planning_ms=plan.get("Planning Time"),execution_ms=plan.get("Execution Time"),udf_calls=m["calls"],exact_work=m["work"],estimated_total_cost=plan["Plan"]["Total Cost"]))
  norm=[]
  for i,(fam,sel,thr,cost) in enumerate(cells):
   rs=[r for r in raw if r["phase"]=="measured" and (r["family"],r["selectivity"],r["cost"])==(fam,sel,cost)]; st=stats([float(r["execution_ms"]) for r in rs],SEED+i); planset=";".join(sorted({r["plan_nodes"] for r in rs})); calls=sorted({int(r["udf_calls"]) for r in rs})
   norm.append(dict(family=fam,selectivity=sel,threshold=thr,cost=cost,plan_nodes=planset,udf_calls=";".join(map(str,calls)),semantic_valid=True,high_variance=st["cv"]>.10,**st))
 finally:
  if c:c.close()
  if proc:
   subprocess.run([str(PG_BIN/"pg_ctl"),"-D",str(data),"-m","fast","stop"],env=env,check=False,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
   try:proc.wait(10)
   except:proc.terminate()
  log.close()
 def dump(name,rows):
  with (HERE/name).open("x",newline="",encoding="utf-8") as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 dump("planning_grid.csv",grid);dump("semantic_validation.csv",sem);dump("raw.csv",raw);dump("normalized.csv",norm)
 version=subprocess.run([str(PG_BIN/"postgres"),"--version"],env=env,text=True,capture_output=True).stdout.strip(); (HERE/"environment.json").write_text(json.dumps(dict(postgresql=version,python=sys.version,platform=platform.platform(),costs=COSTS,selectivities=SELECTIVITIES,rows=N,seed=SEED),indent=2)+"\n")
 print(json.dumps(dict(planning_cells=len(grid),runtime_cells=len(norm),plan_signatures=len({r['plan_signature'] for r in grid}),high_variance=sum(r['high_variance'] for r in norm))))
if __name__=="__main__":main()
