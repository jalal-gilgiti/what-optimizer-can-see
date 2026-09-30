#!/usr/bin/env python3
"""Separate 30-trial blocks for primary COST cells whose CV exceeded 0.10."""
import csv,importlib.util,json,os,random,subprocess,time
from pathlib import Path
import psycopg2
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("base",HERE/"run_experiment.py"); base=importlib.util.module_from_spec(spec); spec.loader.exec_module(base)
def main():
 out=HERE/"diagnostic30_raw.csv"; normout=HERE/"diagnostic30_normalized.csv"
 if out.exists() or normout.exists(): raise RuntimeError("refusing to overwrite diagnostics")
 primary=list(csv.DictReader((HERE/"normalized.csv").open())); high=[r for r in primary if r["high_variance"]=="True"]
 env=os.environ.copy(); env["LD_LIBRARY_PATH"]=str(base.PG_LIB)+(":"+env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
 data=Path("/tmp/pg-task36-cost-diag-data"); socket=Path("/tmp/pg-task36-cost-diag-socket"); port=55451
 if data.exists() or socket.exists(): raise RuntimeError("diagnostic temp path exists")
 data.mkdir(); socket.mkdir(mode=0o700)
 subprocess.run([str(base.PG_BIN/"initdb"),"-D",str(data),"-L",str(base.PG_SHARE),"--encoding=UTF8","--locale=C.UTF-8","--auth-local=trust","--auth-host=reject",f"--username={os.environ.get('USER','experiment')}"],env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
 with (data/"postgresql.conf").open("a") as f:f.write(f"\nlisten_addresses=''\nport={port}\nunix_socket_directories='{socket}'\nunix_socket_permissions=0700\ndynamic_library_path='{base.PG_PKGLIB}'\njit=off\nmax_parallel_workers=0\nmax_parallel_workers_per_gather=0\n")
 log=(base.RUNTIME/"logs/diagnostic30_postgres_attempt2.log").open("x"); proc=subprocess.Popen([str(base.PG_BIN/"postgres"),"-D",str(data)],env=env,stdout=log,stderr=subprocess.STDOUT,text=True); c=None
 try:
  for _ in range(200):
   try:
    c=psycopg2.connect(host=str(socket),port=port,dbname="postgres",user=os.environ.get("USER","experiment"),connect_timeout=5);c.autocommit=True;break
   except Exception:time.sleep(.1)
  if c is None:raise RuntimeError("diagnostic server startup timeout")
  with c.cursor() as x:x.execute("CREATE DATABASE task36_cost")
  c.close(); c=psycopg2.connect(host=str(socket),port=port,dbname="task36_cost",user=os.environ.get("USER","experiment"),connect_timeout=5);c.autocommit=True;base.setup(c)
  cells=[(r["family"],r["selectivity"],int(r["threshold"]),int(r["cost"])) for r in high]; rng=random.Random(base.SEED+900); raw=[]
  for block in range(1,31):
   order=cells[:];rng.shuffle(order)
   for pos,(fam,sel,thr,cost) in enumerate(order,1):
    plan,m=base.run_one(c,fam,sel,thr,cost,True);sig,label=base.signature(plan);raw.append(dict(block=block,order_position=pos,family=fam,selectivity=sel,threshold=thr,cost=cost,plan_signature=sig,plan_nodes=label,execution_ms=plan.get("Execution Time"),planning_ms=plan.get("Planning Time"),udf_calls=m["calls"],exact_work=m["work"]))
 finally:
  if c:c.close()
  subprocess.run([str(base.PG_BIN/"pg_ctl"),"-D",str(data),"-m","fast","stop"],env=env,check=False,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True);proc.wait(10);log.close()
 summaries=[]
 for i,(fam,sel,thr,cost) in enumerate(cells):
  rs=[r for r in raw if (r["family"],r["selectivity"],r["cost"])==(fam,sel,cost)]; summaries.append(dict(family=fam,selectivity=sel,threshold=thr,cost=cost,**base.stats([float(r["execution_ms"]) for r in rs],base.SEED+1000+i)))
 for path,rows in [(out,raw),(normout,summaries)]:
  with path.open("x",newline="",encoding="utf-8") as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 print(json.dumps(dict(high_variance_primary_cells=len(cells),diagnostic_trials=len(raw))))
if __name__=="__main__":main()
