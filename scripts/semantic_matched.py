#!/usr/bin/env python3
"""Interleaved 0.4.9/Candle bulk trials; no rendered text or vectors are saved."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from semantic_conditions import wait_for_load

p = argparse.ArgumentParser(description=__doc__)
for key in ['binary', 'model', 'reference', 'reference_model', 'python', 'scratch', 'output']:
    p.add_argument('--' + key.replace('_', '-'), type=Path, required=True)
p.add_argument('--fixtures', type=Path, nargs='+', required=True)
p.add_argument('--rounds', type=int, default=3)
p.add_argument('--batches', type=int, default=4)
p.add_argument('--child', choices=['cpu', 'metal'])
p.add_argument('--control', action='store_true', help='Also run the same Candle binary with experimental kernels disabled')
p.add_argument('--max-load', type=float, help='Predeclared quiet-start limit; all waits and all completed samples are retained')
a = p.parse_args()
assert os.getpriority(os.PRIO_PROCESS, 0) >= 10
if a.child:
    sys.path.insert(0, str(a.reference.absolute() / 'py'))
    import embedder
    start = time.perf_counter()
    model = embedder.Embedder(download=False, lane=embedder.LANE_CPU if a.child == 'cpu' else embedder.LANE_METAL)
    load_s = time.perf_counter()-start
    texts = json.loads(a.fixtures[0].read_text())
    samples = []
    for _ in range(a.batches):
        start = time.perf_counter()
        vectors = model.embed_texts(texts, token_budget=1024)
        assert vectors.shape == (len(texts), 384)
        samples.append(time.perf_counter()-start)
    print(json.dumps(dict(load_s=load_s, batch_seconds=samples, rows=len(texts)*a.batches,
                          inference_s=sum(samples), rows_s=len(texts)*a.batches/sum(samples))))
    raise SystemExit(0)

env = dict(os.environ, HOME=str(a.scratch.absolute() / 'home'),
           AGREP_HOME=str(a.scratch.absolute() / 'home'), AGREP_DATA_DIR=str(a.scratch.absolute() / 'data'),
           AGREP_NO_DAEMON='1', AGREP_SEM_THREADS='6', RAYON_NUM_THREADS='6',
           VECLIB_MAXIMUM_THREADS='1', TOKENIZERS_PARALLELISM='false', PYTHONDONTWRITEBYTECODE='1')
env.pop('AGREP_SEM_PROFILE', None)
env.pop('AGREP_QUERY_TIMING', None)
for key in ['HOME', 'AGREP_DATA_DIR']:
    Path(env[key]).mkdir(parents=True, exist_ok=True)
with a.output.open('a') as output:
    for round_no in range(a.rounds):
        for device in ['metal', 'cpu']:
            for fixture in a.fixtures:
                engines = ['reference', 'candle-control', 'candle'] if a.control else ['reference', 'candle']
                for engine in engines if round_no % 2 == 0 else engines[::-1]:
                    waited = wait_for_load(a.output, a.max_load) if a.max_load is not None else 0
                    child_env = dict(env, AGREP_MODEL_DIR=str((a.reference_model if engine == 'reference' else a.model).absolute()))
                    if engine != 'reference':
                        if engine == 'candle-control':
                            child_env.update(AGREP_SEM_FUSED_ATTN='0', AGREP_SEM_FUSED_RESIDUAL='0', AGREP_SEM_VECTOR_ERF='0', AGREP_SEM_ARENA='0', AGREP_SEM_PARALLEL_BATCHES='0')
                        command = [str(a.binary.absolute()), str(a.model.absolute()), device, 'batch', '1024', str(a.batches), str(fixture.absolute())]
                    else:
                        command = [str(a.python.absolute()), str(Path(__file__).absolute()), '--child', device,
                                   '--binary', str(a.binary.absolute()), '--model', str(a.model.absolute()),
                                   '--reference', str(a.reference.absolute()), '--reference-model', str(a.reference_model.absolute()),
                                   '--python', str(a.python.absolute()), '--scratch', str(a.scratch.absolute()),
                                   '--output', str(a.output.absolute()), '--batches', str(a.batches), '--fixtures', str(fixture.absolute())]
                    row = dict(round=round_no, device=device, engine=engine, fixture=fixture.name,
                               fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
                               binary_sha256=hashlib.sha256(a.binary.read_bytes()).hexdigest() if engine != 'reference' else None,
                               threads=6, blas_threads=1, nice=os.getpriority(os.PRIO_PROCESS,0), uptime=subprocess.check_output(['uptime'], text=True).strip(), load_before=os.getloadavg())
                    row['configuration']={k:child_env.get(k,'0') for k in ['AGREP_SEM_FUSED_ATTN','AGREP_SEM_FUSED_RESIDUAL','AGREP_SEM_VECTOR_ERF','AGREP_SEM_ARENA','AGREP_SEM_PARALLEL_BATCHES']}
                    row.update(start_load_limit=a.max_load, waited_s=waited)
                    start = time.perf_counter()
                    child = subprocess.run(command, env=child_env, capture_output=True, text=True)
                    row.update(exit=child.returncode, wall_s=time.perf_counter()-start, load_after=os.getloadavg())
                    if child.returncode == 0:
                        row.update(json.loads(child.stdout))
                    output.write(json.dumps(row)+'\n')
                    output.flush()
                    print(json.dumps(row), flush=True)
                    if child.returncode:
                        (a.scratch / 'failure.log').write_text(child.stderr)
                        raise SystemExit(child.returncode)
