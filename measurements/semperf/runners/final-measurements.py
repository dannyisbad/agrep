import json,subprocess,sys
from pathlib import Path
python=sys.executable
steps=[("cpu",[python,"scratch/final-cpu.py"])]
for label in ["before","after"]:
 steps.append(("query-"+label,[python,"scripts/semantic_queries.py","--binary","scratch/bin/agrep-"+label,"--home","scratch/scale-home","--data","scratch/scale-data","--model","scratch/model","--output","measurements/semperf/query-"+label+".jsonl"]))
for name,command in steps:
 print("START "+name,flush=True)
 p=subprocess.run(command)
 print("END "+name+" "+str(p.returncode),flush=True)
 with Path("measurements/semperf/final-step-exits.jsonl").open("a") as f:f.write(json.dumps(dict(step=name,exit=p.returncode))+"\n")
 if name=="cpu" and p.returncode:raise SystemExit(p.returncode)
