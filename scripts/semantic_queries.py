#!/usr/bin/env python3
"""Full-exit, interleaved semantic/hybrid/keyword samples on an isolated fixture."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time
from semantic_conditions import wait_for_load

ap = argparse.ArgumentParser()
ap.add_argument("--binary", type=Path, required=True)
ap.add_argument("--reference-cli", type=Path)
ap.add_argument("--home", type=Path, required=True)
ap.add_argument("--data", type=Path, required=True)
ap.add_argument("--model", type=Path, required=True)
ap.add_argument("--output", type=Path, required=True)
ap.add_argument("--rounds", type=int, default=2)
ap.add_argument("--profile", action="store_true")
ap.add_argument("--max-load", type=float, default=os.cpu_count())
ap.add_argument("--allow-contended", action="store_true", help="Record explicitly contended context timings without waiting; never use them to claim a matched-host speedup")
a = ap.parse_args()
if a.allow_contended:
    a.max_load = None
assert a.rounds > 0 and os.getpriority(os.PRIO_PROCESS, 0) >= 10
env = dict(os.environ, HOME=str(a.home.resolve()), AGREP_HOME=str(a.home.resolve()), AGREP_DATA_DIR=str(a.data.resolve()), AGREP_MODEL_DIR=str(a.model.resolve()), AGREP_PROFILE="compact", RAYON_NUM_THREADS="6", VECLIB_MAXIMUM_THREADS="1", TOKENIZERS_PARALLELISM="false", AGREP_QUERY_TIMING="1")
if a.profile:
    env["AGREP_SEM_PROFILE"] = "1"
queries = json.loads(Path("crates/agrep-core/tests/fixtures/semantic-eval.json").read_text())["texts"][:10]
samples = {mode: [] for mode in ["semantic", "hybrid", "keyword"]}
keyword_parts = {mode: [] for mode in samples}
with a.output.open("a") as out:
    for round_no in range(a.rounds):
        for query in queries:
            for mode in samples:
                waited = wait_for_load(a.output,a.max_load)
                argv = [str(a.binary.absolute())]
                if a.reference_cli:
                    argv.append(str(a.reference_cli.resolve()))
                argv += ["search", "-n", "8"]
                argv += ["-s"] if mode == "semantic" else ["--lexical"] if mode == "keyword" else []
                argv += ["--", query]
                record = dict(round=round_no, mode=mode, argv=argv, uptime=subprocess.check_output(["uptime"], text=True).strip(), load_before=os.getloadavg(), waited_s=waited, start_load_limit=a.max_load, measurement_context="contended" if a.allow_contended else "load-gated")
                start = time.perf_counter()
                process = subprocess.run(argv, env=env, capture_output=True, text=True)
                record.update(wall_ms=(time.perf_counter()-start)*1000, exit=process.returncode, stdout=process.stdout, stderr=process.stderr, load_after=os.getloadavg())
                record["result_marker"] = "~semantic" if a.reference_cli else "meaning match"
                record["meaning_rows"] = process.stdout.lower().count(record["result_marker"])
                for line in process.stderr.splitlines():
                    try:
                        timing=json.loads(line)
                    except (ValueError,TypeError):
                        continue
                    if isinstance(timing,dict):
                        for key in ["keyword_ms","query_prepare_ms"]:
                            if key in timing: record[key]=timing[key]
                out.write(json.dumps(record)+"\n")
                out.flush()
                samples[mode].append(record["wall_ms"])
                if "keyword_ms" in record:
                    keyword_parts[mode].append(record["keyword_ms"])
                if process.returncode not in (0, 1):
                    raise SystemExit(f"query failed: {record}")
                if mode == "semantic" and (process.returncode != 0 or not record["meaning_rows"]):
                    raise SystemExit("semantic query did not return a meaning result; failed sample retained")
budget = json.loads(Path("docs/perf-budgets.json").read_text())
summary = {}
for mode, values in samples.items():
    summary[mode] = dict(samples=len(values), p50_ms=statistics.median(values), p95_ms=sorted(values)[math.ceil(len(values)*.95)-1])
    if keyword_parts[mode]:
        summary[mode]["keyword_part_p50_ms"] = statistics.median(keyword_parts[mode])
        summary[mode]["keyword_part_p95_ms"] = sorted(keyword_parts[mode])[math.ceil(len(keyword_parts[mode])*.95)-1]
    if mode != "keyword":
        key = "semantic_owner_p50_ms" if mode == "semantic" else "hybrid_owner_p50_ms"
        summary[mode].update(budget_ms=budget[key], passed=summary[mode]["p50_ms"] <= budget[key])
a.output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2)+"\n")
print(json.dumps(summary, indent=2))
raise SystemExit(0 if all(summary[m]["passed"] for m in ["semantic","hybrid"]) else 1)
