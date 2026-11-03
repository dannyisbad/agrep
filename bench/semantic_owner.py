#!/usr/bin/env python3
"""Owner-scale semantic check: synthetic vectors, real model, fresh CLI processes.

Every query must succeed with meaning results. Latency is printed against the SPEC
targets in docs/perf-budgets.json and does not fail the check; speed is gated by the
matched comparison with agrep 0.4.9 (crates/agrep/examples/matched.rs)."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def check(report, targets):
    """Correctness failures, and one measured-vs-target line per statistic."""
    errors, lines = [], []
    if report.get('references', 0) < 200_000:
        errors.append('owner workload requires at least 200000 vector references')
    for mode, prefix in [('semantic', 'semantic_owner'), ('hybrid', 'hybrid_owner')]:
        rows = [r for r in report.get('samples', []) if r.get('mode') == mode]
        if len(rows) < 20:
            errors.append(f'{mode}: at least 20 fresh-process samples required')
        if not rows or any(r.get('exit') != 0 or r.get('meaning_rows', 0) < 1 for r in rows):
            errors.append(f'{mode}: each query must succeed with real meaning results')
        values = [r.get('wall_ms') for r in rows]
        if not values or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values):
            errors.append(f'{mode}: invalid timing samples')
            continue
        for stat, value in [('p50', statistics.median(values)), ('p95', percentile(values, .95))]:
            key = f'{prefix}_{stat}_ms'
            limit = targets.get(key)
            if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
                errors.append(f'{key}: missing positive target')
            else:
                above = ', above target' if value > limit else ''
                lines.append(f'{mode} {stat}: {value:.3f} ms (target {limit:.3f} ms{above})')
    return errors, lines


def load():
    return list(os.getloadavg()) if hasattr(os, 'getloadavg') else None


def run(a):
    binary, builder, evaluator = (str(p.resolve()) for p in (a.binary, a.builder, a.evaluator))
    targets = json.loads((ROOT / 'docs/perf-budgets.json').read_text(encoding='utf-8'))
    queries = json.loads((ROOT / 'crates/agrep-core/tests/fixtures/semantic-eval.json').read_text(encoding='utf-8'))['texts'][:10]
    report = {'references': 0, 'kind': 'native synthetic sources and deterministic oracle-vector perturbations; real query model',
              'samples': [], 'threads': 6, 'blas_threads': 1,
              'binary_sha256': hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
              'builder_sha256': hashlib.sha256(Path(builder).read_bytes()).hexdigest(),
              'evaluator_sha256': hashlib.sha256(Path(evaluator).read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix='agrep-semantic-owner-') as temporary:
        root = Path(temporary)
        home, data = (root / part for part in ['home', 'data'])
        model = a.model.resolve() if a.model is not None else root / 'model'
        home.mkdir()
        # No inherited source/config/model roots. Preserve only executable and OS necessities.
        env = {k: v for k, v in os.environ.items() if not k.startswith('AGREP_')}
        env.update(HOME=str(home), USERPROFILE=str(home), AGREP_HOME=str(home),
                   AGREP_DATA_DIR=str(data), AGREP_MODEL_DIR=str(model), AGREP_NO_DAEMON='1',
                   AGREP_PROFILE='compact', RAYON_NUM_THREADS='6', AGREP_SEM_THREADS='6',
                   VECLIB_MAXIMUM_THREADS='1', TOKENIZERS_PARALLELISM='false')
        subprocess.run([evaluator, str(model), 'provision'], env=env, check=True, capture_output=True)
        # Runs the unchanged model/tokenizer/top-5/recall/long-tail gate before timing.
        quality = subprocess.run([evaluator, str(model), 'cpu'], env=env, check=True, capture_output=True, text=True, encoding='utf-8')
        report['model_quality'] = json.loads(quality.stdout)
        built = subprocess.run([builder, str(data), '200000'], env=env, check=True, capture_output=True, text=True, encoding='utf-8')
        fixture = json.loads(built.stdout)
        report['references'] = fixture['coverage']['indexed']
        report['coverage'] = fixture['coverage']
        report['fixture_rows'] = fixture['rows']
        env['AGREP_QUERY_TIMING'] = '1'
        for sample in range(a.runs):
            query_id = sample % len(queries)
            for mode in ['semantic', 'hybrid'] if sample % 2 == 0 else ['hybrid', 'semantic']:
                command = [binary, 'search', '-n', '8']
                if mode == 'semantic':
                    command.append('-s')
                command += ['--', queries[query_id]]
                row = {'sample': sample, 'query_id': query_id, 'mode': mode, 'load_before': load()}
                if os.name == 'posix':
                    row['uptime'] = subprocess.check_output(['uptime'], text=True).strip()
                start = time.perf_counter()
                result = subprocess.run(command, env=env, capture_output=True, text=True, encoding='utf-8')
                row.update(wall_ms=(time.perf_counter()-start)*1000, exit=result.returncode,
                           meaning_rows=result.stdout.count('meaning match'), load_after=load())
                row['phases'] = []
                for line in result.stderr.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        row['phases'].append(event)
                report['samples'].append(row)
                # Persist each complete sample, including failures, before the next process.
                a.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        report['failures'], report['targets'] = check(report, targets)
        report['passed'] = not report['failures']
        a.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    for line in report['targets'] + report['failures']:
        print(line)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['binary', 'builder', 'evaluator', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--model', type=Path, help='Explicit existing model directory for local reproduction; CI provisions in its temporary root')
    parser.add_argument('--runs', type=int, default=20)
    raise SystemExit(run(parser.parse_args()))
