import subprocess,sys,json
from pathlib import Path
steps=[]
for label in ["before","after"]:
 steps.append(("profile-"+label,[sys.executable,"scripts/semantic_profile.py","--output","measurements/semperf/query-profile-"+label+"-warm.json","--","scratch/bin/agrep-"+label,"search","-s","-n","8","--","why does Metal attention produce NaNs?"]))
steps.append(("profile-mixed-cpu",[sys.executable,"scripts/semantic_profile.py","--output","measurements/semperf/profile-final-cpu-mixed.json","--","scratch/bin/semantic-after","scratch/model","cpu","batch","1024","1","fixtures/semantic-mixed.json"]))
for label in ["before","after"]:
 steps.append(("query-"+label,[sys.executable,"scripts/semantic_queries.py","--binary","scratch/bin/agrep-"+label,"--home","scratch/scale-home","--data","scratch/scale-data","--model","scratch/model","--output","measurements/semperf/query-"+label+".jsonl"]))
for name,command in steps:
 print("START "+name,flush=True);p=subprocess.run(command);print("END "+name+" "+str(p.returncode),flush=True)
 with Path("measurements/semperf/postrestart-exits.jsonl").open("a") as f:f.write(json.dumps(dict(step=name,exit=p.returncode))+"\n")
 if name.startswith("profile") and p.returncode:raise SystemExit(p.returncode)
