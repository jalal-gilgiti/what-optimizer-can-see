#!/usr/bin/env python3
import csv
from pathlib import Path
import matplotlib.pyplot as plt

HERE=Path(__file__).resolve().parent
rows=list(csv.DictReader((HERE/"normalized.csv").open()))
work=[r for r in rows if r["case"].startswith("work_")]
x=[int(r["work_units_per_call"]) for r in work]; y=[float(r["median_ms"]) for r in work]
lo=[v-float(r["ci_low_ms"]) for v,r in zip(y,work)]; hi=[float(r["ci_high_ms"])-v for v,r in zip(y,work)]
plt.rcParams.update({"font.size":9,"pdf.fonttype":42,"ps.fonttype":42})
fig,(ax,zoom)=plt.subplots(1,2,figsize=(7.1,3.0),gridspec_kw={"width_ratios":[1.25,1]})
native=next(float(r["median_ms"]) for r in rows if r["case"]=="native")
for panel in (ax,zoom):
    panel.errorbar(x,y,yerr=[lo,hi],marker="o",capsize=3,label="Python callback (95% bootstrap CI)")
    panel.axhline(native,color="0.45",ls="--",lw=1,label="native expression")
    panel.grid(True,which="both",alpha=.22)
ax.set_xscale("symlog",linthresh=1)
ax.set_xticks([0,1,10,100,1000]); ax.set_xticklabels(["0","1","10","100","1000"])
ax.set_xlim(-.1,1400); ax.set_title("Full work range",fontsize=9)
zoom.set_xlim(-.3,10.3); zoom.set_ylim(0,20); zoom.set_xticks([0,1,10]); zoom.set_title("Low-work detail",fontsize=9)
ax.set_ylabel("Median runtime (ms), 10,000 invocations")
fig.supxlabel("Exact procedural work units per invocation",fontsize=9,y=.01)
ax.legend(frameon=False,fontsize=8,loc="upper left")
fig.tight_layout(rect=(0,.04,1,1))
for ext in ("pdf","svg","png"): fig.savefig(HERE/f"work_vs_runtime.{ext}",dpi=220,bbox_inches="tight")
