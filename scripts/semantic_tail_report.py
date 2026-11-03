#!/usr/bin/env python3
"""Attribute every measured tail; keep historical inference distinct from counters."""
import json
import statistics
from pathlib import Path

root = Path('measurements/semperf2')
output = []
campaigns = [(path.name, [json.loads(s) for s in path.read_text().splitlines()])
             for path in sorted(root.glob('tails-*.jsonl'))]
owner = root / 'owner-ci.json'
if owner.exists():
    rows = [dict(row, variant='ci', round=row['sample'] // 10)
            for row in json.loads(owner.read_text())['samples']]
    campaigns.append((owner.name, rows))
for campaign, rows in campaigns:
    for row in rows:
        row.setdefault('variant', 'before')
    for variant in sorted({r['variant'] for r in rows}):
        for mode in ['semantic', 'hybrid', 'keyword']:
            group = [r for r in rows if r['variant'] == variant and r['mode'] == mode]
            if not group:
                continue
            median = statistics.median(r['wall_ms'] for r in group)
            for row in group:
                if row['wall_ms'] <= 2 * median:
                    continue
                phases = {e['phase']: e for e in row['phases'] if 'phase' in e}
                keyword = phases.get('keyword', {}).get('wall_ms', next((e['keyword_ms'] for e in row['phases'] if 'keyword_ms' in e), 0))
                gap = row['wall_ms'] - phases.get('query_total', {}).get('wall_ms', row['wall_ms'])
                significant = {name: {k: v for k, v in phase.items() if k in ['wall_ms', 'cpu_ms', 'major_faults', 'minor_faults', 'involuntary_switches']}
                               for name, phase in phases.items() if name in ['model_read_ahead', 'freshness', 'schedule', 'semantic_coverage', 'model_load', 'tokenize', 'query_inference', 'vector_scan', 'vector_sort', 'vector_refine', 'keyword', 'vector_block_open']}
                causes = []
                freshness = next((e['freshness'] for e in row['phases'] if 'freshness' in e), None)
                caches = [e['model_cache'] for e in row['phases'] if 'model_cache' in e]
                workers = [e['worker_started'] for e in row['phases'] if 'worker_started' in e]
                read_ahead = next((e['model_read_ahead'] for e in row['phases'] if isinstance(e.get('model_read_ahead'), dict)), None)
                if row.get('model_files_changed') or any(e.get('writes', 0) for e in caches):
                    causes.append('model files changed; inspect provisioning/write events')
                if freshness and freshness.get('changed_rows', 0):
                    causes.append('freshness processed changed source rows')
                if keyword > row['wall_ms'] * .6:
                    causes.append('keyword query execution')
                if phases.get('model_read_ahead', {}).get('wall_ms', 0) > row['wall_ms'] * .2:
                    causes.append('model read-ahead and cold-page request')
                for name in ['model_load', 'vector_scan', 'vector_block_open']:
                    if phases.get(name, {}).get('major_faults', 0) > 100:
                        causes.append(name + ' page-ins')
                if gap > row['wall_ms'] * .3:
                    causes.append('outside timed search: process startup/exit and scheduling')
                coverage = next((e['coverage_and_blocks_ms'] for e in row['phases'] if 'coverage_and_blocks_ms' in e), 0)
                if coverage - phases.get('vector_block_open', {}).get('wall_ms', 0) > 100:
                    causes.append('coverage count')
                if not causes:
                    largest = max(significant, key=lambda n: significant[n]['wall_ms'])
                    causes.append(largest + ' CPU/scheduling wall time; no measured page-in explanation')
                output.append(dict(campaign=campaign, variant=variant, mode=mode, round=row['round'], query_id=row['query_id'], wall_ms=row['wall_ms'], p50_ms=median, causes=causes, process_gap_ms=gap, phases=significant, load_before=row['load_before'], load_after=row['load_after'], model_files_changed=row.get('model_files_changed'), freshness=freshness, model_caches=caches, read_ahead=read_ahead, workers_started=workers))
(root / 'tail-attribution.json').write_text(json.dumps(output, indent=2) + '\n')
print('Attributed', len(output), 'outliers')
