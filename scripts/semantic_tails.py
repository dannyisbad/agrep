#!/usr/bin/env python3
"""Full-process tail attribution. Commit numeric aggregates, never query/output text."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import time
from semantic_conditions import wait_for_load

parser = argparse.ArgumentParser(description=__doc__)
for key in ["binary", "home", "data", "model", "output"]:
    parser.add_argument("--" + key, type=Path, required=True)
parser.add_argument("--compare-binary", type=Path)
parser.add_argument("--rounds", type=int, default=2)
parser.add_argument("--no-auto", action="store_true")
parser.add_argument("--max-load", type=float)
args = parser.parse_args()
assert os.getpriority(os.PRIO_PROCESS, 0) >= 10
queries = json.loads(Path("crates/agrep-core/tests/fixtures/semantic-eval.json").read_text())["texts"][:10]
env = dict(os.environ, HOME=str(args.home.absolute()), AGREP_HOME=str(args.home.absolute()),
           AGREP_DATA_DIR=str(args.data.absolute()), AGREP_MODEL_DIR=str(args.model.absolute()),
           AGREP_PROFILE="compact", AGREP_QUERY_TIMING="1", RAYON_NUM_THREADS="6",
           VECLIB_MAXIMUM_THREADS="1", TOKENIZERS_PARALLELISM="false")
engines = [("before", args.binary)]
if args.compare_binary:
    engines.append(("after", args.compare_binary))
hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for _, path in engines}

def model_stamp():
    return [(p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(args.model.iterdir()) if p.is_file()]

with args.output.open("a") as log:
    for round_no in range(args.rounds):
        for query_id, query in enumerate(queries):
            for mode_id, mode in enumerate(["semantic", "hybrid", "keyword"]):
                ordered = engines if (round_no + query_id + mode_id) % 2 == 0 else engines[::-1]
                for variant, binary in ordered:
                    waited = wait_for_load(args.output, args.max_load) if args.max_load is not None else 0
                    cmd = [str(binary.absolute())] + (["--no-auto"] if args.no_auto else [])
                    cmd += ["search", "-n", "8"]
                    cmd += ["-s"] if mode == "semantic" else ["--lexical"] if mode == "keyword" else []
                    cmd += ["--", query]
                    record = dict(round=round_no, query_id=query_id, mode=mode, variant=variant,
                                  binary_sha256=hashes[str(binary)], no_auto=args.no_auto,
                                  start_load_limit=args.max_load, waited_s=waited,
                                  uptime=subprocess.check_output(["uptime"], text=True).strip(),
                                  load_before=os.getloadavg())
                    stamp = model_stamp()
                    before = resource.getrusage(resource.RUSAGE_CHILDREN)
                    start = time.perf_counter()
                    child = subprocess.run(cmd, env=env, capture_output=True, text=True)
                    wall_ms = (time.perf_counter() - start) * 1000
                    after = resource.getrusage(resource.RUSAGE_CHILDREN)
                    phases = []
                    for line in child.stderr.splitlines():
                        try:
                            value = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(value, dict):
                            phases.append(value)
                    count = re.search(r"([0-9,]+)\+? matches", child.stderr)
                    record.update(wall_ms=wall_ms, exit=child.returncode, load_after=os.getloadavg(),
                                  phases=phases, meaning_rows=child.stdout.count("meaning match"),
                                  result_floor=int(count[1].replace(",", "")) if count else None,
                                  model_files_changed=stamp != model_stamp(),
                                  child_cpu_ms=(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime) * 1000,
                                  child_major_faults=after.ru_majflt - before.ru_majflt,
                                  child_minor_faults=after.ru_minflt - before.ru_minflt)
                    log.write(json.dumps(record) + "\n")
                    log.flush()
                    if child.returncode not in (0, 1) or mode == "semantic" and not record["meaning_rows"]:
                        raise SystemExit("failed aggregate sample retained; inspect local process output")
