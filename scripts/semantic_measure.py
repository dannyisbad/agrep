#!/usr/bin/env python3
"""Fixed semantic measurements; preserves every run and never retries failures."""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time

ap = argparse.ArgumentParser()
ap.add_argument('--threads', type=int, default=min(6, os.cpu_count() or 1))
ap.add_argument('--runs', type=int, default=3)
ap.add_argument('--batches', type=int, default=16)
ap.add_argument('--window', type=int, default=1024)
ap.add_argument('--phase', choices=['embed', 'query'], required=True)
ap.add_argument('--model', type=Path, required=True)
ap.add_argument('--data', type=Path)
ap.add_argument('--home', type=Path, required=True)
ap.add_argument('--binary', type=Path, default=Path('target/release/agrep'))
ap.add_argument('--eval-binary', type=Path, default=Path('target/release/examples/semantic_eval'))
args = ap.parse_args()
budgets = json.loads(Path('docs/perf-budgets.json').read_text())
if args.runs < 1:
    ap.error('--runs must be positive')
env = dict(os.environ, AGREP_MODEL_DIR=str(args.model.resolve()), AGREP_HOME=str(args.home.resolve()), HOME=str(args.home.resolve()), AGREP_PROFILE='compact', RAYON_NUM_THREADS=str(args.threads), VECLIB_MAXIMUM_THREADS='1', TOKENIZERS_PARALLELISM='false')
args.home.mkdir(parents=True, exist_ok=True)
if args.data:
    env['AGREP_DATA_DIR'] = str(args.data.resolve())
else:
    env['AGREP_DATA_DIR'] = str((args.home / 'data').resolve())

def run(argv):
    uptime = subprocess.check_output(['uptime'], text=True).strip()
    load_before = os.getloadavg()
    start = time.perf_counter()
    process = subprocess.run([str(x) for x in argv], env=env, capture_output=True, text=True)
    record = {'argv': [str(x) for x in argv], 'wall_ms': (time.perf_counter()-start)*1000, 'exit': process.returncode, 'stdout': process.stdout, 'stderr': process.stderr, 'uptime': uptime, 'load_before': load_before, 'load_after': os.getloadavg()}
    if process.returncode:
        print(json.dumps({'failed': record, 'completed': out.get('runs', [])}), flush=True)
        raise SystemExit(2)
    return record

out = {'phase': args.phase, 'window': args.window, 'batches': args.batches, 'samples': args.runs, 'threads': args.threads, 'nice': os.getpriority(os.PRIO_PROCESS, 0) if hasattr(os, 'getpriority') else None, 'load_average': os.getloadavg() if hasattr(os, 'getloadavg') else None, 'runs': [], 'gates': {}}
if args.phase == 'embed':
    for device in ['cpu', 'metal']:
        rows = []
        for i in range(args.runs):
            r = run([args.eval_binary, args.model, device, 'batch', str(args.window), str(args.batches)])
            out['runs'].append(r)
            rows.append(json.loads(r['stdout'])['rows_s'])
        median = statistics.median(rows)
        budget = budgets['semantic_'+device+'_rows_s']
        out['gates'][device+'_rows_s'] = {'observed': median, 'required': budget, 'pass': math.isfinite(median) and median >= budget}
else:
    if not args.data:
        ap.error('--data is required for query')
    fixture = json.loads(Path('crates/agrep-core/tests/fixtures/semantic-eval.json').read_text())
    samples = []
    for q in fixture['texts'][:10]:
        r = run([args.binary, 'search', '-n', '8', '--', q])
        out['runs'].append(r)
        samples.append(r['wall_ms'])
    p50 = statistics.median(samples)
    out['samples'] = len(samples)
    maximum = budgets['hybrid_owner_p50_ms']
    out['gates']['hybrid_ms'] = {'observed': p50, 'maximum': maximum, 'pass': math.isfinite(p50) and p50 <= maximum}
    out['p95_ms'] = sorted(samples)[math.ceil(0.95*len(samples))-1]
print(json.dumps(out, indent=2), flush=True)
raise SystemExit(0 if all(g['pass'] for g in out['gates'].values()) else 1)
