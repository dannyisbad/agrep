#!/usr/bin/env python3
"""Capture one synchronized semantic diagnostic, including load and raw output."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from semantic_conditions import wait_for_load

ap=argparse.ArgumentParser()
ap.add_argument("--output",type=Path,required=True)
ap.add_argument("--max-load",type=float,default=os.cpu_count())
ap.add_argument("command",nargs=argparse.REMAINDER)
a=ap.parse_args()
command=a.command[1:] if a.command[:1]==["--"] else a.command
assert command and os.getpriority(os.PRIO_PROCESS,0)>=10
assert os.environ.get("AGREP_HOME") and os.environ.get("AGREP_DATA_DIR")
waited=wait_for_load(a.output,a.max_load)
env=dict(os.environ,AGREP_SEM_PROFILE="1",AGREP_QUERY_TIMING="1",RAYON_NUM_THREADS="6",VECLIB_MAXIMUM_THREADS="1",TOKENIZERS_PARALLELISM="false",AGREP_NO_DAEMON="1")
record=dict(argv=command,uptime=subprocess.check_output(["uptime"],text=True).strip(),load_before=os.getloadavg(),waited_s=waited)
start=time.perf_counter()
p=subprocess.run(command,env=env,capture_output=True,text=True)
record.update(exit=p.returncode,wall_s=time.perf_counter()-start,load_after=os.getloadavg(),stdout=p.stdout,stderr=p.stderr)
a.output.write_text(json.dumps(record,indent=2)+"\n")
print(json.dumps({k:v for k,v in record.items() if k not in ("stdout","stderr")}))
raise SystemExit(p.returncode)
