#!/usr/bin/env python3
"""Paired semantic kernel trials. Evidence contains only numeric aggregates.

All text comes from committed public fixtures. Every batch, first batch included,
is retained. Profiling synchronizes each operation and is a separate experiment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--scratch', type=Path, required=True)
    p.add_argument('--fixtures', type=Path, nargs='+', required=True)
    p.add_argument('--devices', nargs='+', default=['cpu', 'metal'])
    p.add_argument('--variants', nargs='+', default=['baseline:0', 'attention:1'],
                   help='label:attention[:residual[:command-buffer-limit[:vector-erf[:arena[:parallel-batches]]]]]')
    p.add_argument('--samples', type=int, default=2)
    p.add_argument('--batches', type=int, default=2)
    p.add_argument('--profile', action='store_true')
    p.add_argument('--batch-tokens', type=int)
    p.add_argument('--max-padding', type=int)
    a = p.parse_args()
    assert os.getpriority(os.PRIO_PROCESS, 0) >= 10
    binary = a.binary.absolute()
    env = dict(os.environ, HOME=str(a.scratch.absolute() / 'home'),
               AGREP_HOME=str(a.scratch.absolute() / 'home'),
               AGREP_DATA_DIR=str(a.scratch.absolute() / 'data'),
               AGREP_NO_DAEMON='1', AGREP_SEM_THREADS='6', RAYON_NUM_THREADS='6',
               VECLIB_MAXIMUM_THREADS='1', TOKENIZERS_PARALLELISM='false')
    for key in ['HOME', 'AGREP_DATA_DIR']:
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    if a.profile:
        env['AGREP_SEM_PROFILE'] = '1'
    else:
        env.pop('AGREP_SEM_PROFILE', None)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with a.output.open('a') as output:
        for sample in range(a.samples):
            for device in a.devices:
                for fixture in a.fixtures:
                    variants = a.variants if sample % 2 == 0 else list(reversed(a.variants))
                    for variant in variants:
                        parts = variant.split(':')
                        trial = dict(env, AGREP_SEM_FUSED_ATTN=parts[1],
                                     AGREP_SEM_FUSED_RESIDUAL=parts[2] if len(parts) > 2 else '0')
                        if len(parts) > 3:
                            trial['CANDLE_METAL_COMPUTE_PER_BUFFER'] = parts[3]
                        trial['AGREP_SEM_VECTOR_ERF'] = parts[4] if len(parts) > 4 else '0'
                        trial['AGREP_SEM_ARENA'] = parts[5] if len(parts) > 5 else '0'
                        trial['AGREP_SEM_PARALLEL_BATCHES'] = parts[6] if len(parts) > 6 else '0'
                        command = [str(binary), str(a.model.absolute()), device,
                                   'batch', '1024', str(a.batches), str(fixture.absolute())]
                        if a.batch_tokens:
                            command.append(str(a.batch_tokens))
                        if a.max_padding is not None:
                            if not a.batch_tokens:
                                command.append('16384' if device=='cpu' else '1024')
                            command.append(str(a.max_padding))
                        row = dict(label=parts[0], sample=sample, device=device,
                                   fixture=fixture.name,
                                   fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
                                   binary_sha256=digest, threads=6, blas_threads=1, nice=os.getpriority(os.PRIO_PROCESS,0),
                                   profile=a.profile, uptime=subprocess.check_output(['uptime'], text=True).strip(),
                                   configuration=parts[1:], batch_tokens=a.batch_tokens, max_padding=a.max_padding,
                                   load_before=os.getloadavg())
                        start = time.perf_counter()
                        r = subprocess.run(command, env=trial, capture_output=True, text=True)
                        row.update(wall_s=time.perf_counter()-start, exit=r.returncode,
                                   load_after=os.getloadavg())
                        if r.returncode == 0:
                            row.update(json.loads(r.stdout))
                        # No rendered rows, strings or source text enter evidence.
                        stages = []
                        for line in r.stderr.splitlines():
                            try:
                                event = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(event, dict) and any(k in event for k in ['semantic_stage','phase','layer','batch_rows','arena_batch','arena_reserve']):
                                stages.append(event)
                        if stages:
                            row['stages'] = stages
                        output.write(json.dumps(row) + '\n')
                        output.flush()
                        print(json.dumps({k: row.get(k) for k in ['label', 'device', 'fixture', 'sample', 'exit', 'rows_s', 'load_before', 'load_after']}), flush=True)
                        if r.returncode:
                            (a.scratch / 'failure.log').write_text(r.stderr)
                            sys.exit(r.returncode)


if __name__ == '__main__':
    main()
