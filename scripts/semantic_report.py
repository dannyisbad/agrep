#!/usr/bin/env python3
"""Summarize preserved runs; missing or failed evidence never becomes a pass."""
import json
from pathlib import Path
import statistics

root=Path('measurements/semperf')
budget=json.loads(Path('docs/perf-budgets.json').read_text())
report={'bulk':{},'queries':{},'failures':[]}
for label in ['reference','before','after']:
    path=root/f'matched-{label}.jsonl'
    records=[json.loads(line)for line in path.read_text().splitlines()]if path.exists()else[]
    for device in ['cpu','metal']:
        for fixture in ['semantic-batch.json','semantic-mixed.json']:
            rows=[r for r in records if r['engine'].endswith(device)and r['fixture']==fixture]
            bad=[r for r in rows if r['exit']!=0]
            report['failures']+=bad
            valid=[r for r in rows if r['exit']==0]
            key=f'{device}/{fixture}'
            entry=report['bulk'].setdefault(key,{})
            if valid:
                loads=[r[k][0]for r in valid for k in ['load_before','load_after']]
                entry[label]={'samples':len(valid),'rows_s':statistics.median(r['rows_s']for r in valid),'fixture_hashes':sorted({r['sha256'] for r in valid}),'thread_settings':sorted({(r['threads'],r['blas_threads']) for r in valid}),'range_rows_s':[min(r['rows_s']for r in valid),max(r['rows_s']for r in valid)],'load_1m_range':[min(loads),max(loads)]}
                if fixture=='semantic-mixed.json':
                    entry[label]['projection_130k_minutes']=130000/entry[label]['rows_s']/60
            if label=='after'and valid:
                threshold=budget[f'semantic_{device}_rows_s']
                if fixture == 'semantic-mixed.json': threshold=0
                reference=entry.get('reference',{}).get('rows_s')
                comparable = all(entry.get(name,{}).get('samples')==3 for name in ['reference','before','after']) and len({tuple(entry[name]['fixture_hashes']) for name in ['reference','before','after'] if name in entry})==1 and all(entry.get(name,{}).get('thread_settings')==[(6,1)] for name in ['reference','before','after'])
                entry['gate']={'minimum':max(threshold,reference or 0),'comparable':comparable,'passed':not bad and comparable and reference is not None and entry[label]['rows_s']>=max(threshold,reference)}
for label in ['reference','before','after']:
    p=root/f'query-{label}.summary.json'
    if p.exists():
        report['queries'][label]=json.loads(p.read_text())
        records=[json.loads(line) for line in (root/f'query-{label}.jsonl').read_text().splitlines()]
        for mode,entry in report['queries'][label].items():
            selected=[r for r in records if r['mode']==mode]
            loads=[r[k][0] for r in selected for k in ['load_before','load_after']]
            entry['load_1m_range']=[min(loads),max(loads)]
            entry['measurement_contexts']=sorted({r.get('measurement_context','load-gated') for r in selected})
            entry['successful_samples']=sum(r['exit'] in ((0,1) if mode=='keyword' else (0,)) for r in selected)
            if mode!='keyword':
                entry['passed']=entry['passed'] and len(selected)>=20 and entry['successful_samples']==len(selected)
            report['failures'] += [r for r in selected if r['exit'] not in (0,1)]
(root/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
lines = ["Bulk values are median rows/s across three fresh processes, each with eight",
         "batches. Parentheses show the one-minute load range at sample boundaries;",
         "the raw records retain all three load averages and every batch time.", "",
         "| Bulk fixture | 0.4.9 | Before | After |", "|---|---:|---:|---:|"]
for key, label in [("cpu/semantic-batch.json", "CPU, 200 tokens"),
                   ("cpu/semantic-mixed.json", "CPU, mixed"),
                   ("metal/semantic-batch.json", "Metal, 200 tokens"),
                   ("metal/semantic-mixed.json", "Metal, mixed")]:
    cells=[]
    for name in ["reference","before","after"]:
        entry=report["bulk"].get(key,{}).get(name)
        if entry:
            lo,hi=entry["load_1m_range"]
            cells.append(f"{entry['rows_s']:.2f} ({lo:.2f}–{hi:.2f})")
        else:cells.append("Missing")
    lines.append("| "+label+" | "+" | ".join(cells)+" |")
lines += ["", "Fresh-process query values are p50 / p95 milliseconds, with the same load",
          "notation. There are twenty samples per mode. The reference uses its",
          "default Apple-silicon MLX lane with daemon use disabled.", "",
          "| Query mode | 0.4.9 (contended) | Before | After |", "|---|---:|---:|---:|"]
for mode in ["semantic","hybrid","keyword"]:
    cells=[]
    for name in ["reference","before","after"]:
        entry=report["queries"].get(name,{}).get(mode)
        if entry:
            lo,hi=entry["load_1m_range"]
            cells.append(f"{entry['p50_ms']:.3f} / {entry['p95_ms']:.3f} ({lo:.2f}–{hi:.2f})")
        else:cells.append("Missing")
    lines.append("| "+mode+" | "+" | ".join(cells)+" |")
if "contended" in report["queries"].get("reference",{}).get("semantic",{}).get("measurement_contexts",[]):
    lines += ["", "The reference query run was explicitly contended after the host supplied no quiet window. Its values are context only, not a matched-host speedup comparison. Before/after query and all bulk sampling conditions were unchanged."]
after=report["queries"].get("after",{}).get("hybrid",{})
if "keyword_part_p50_ms" in after:
    lines += ["", f"Inside the after-hybrid command, the keyword stage was {after['keyword_part_p50_ms']:.2f} ms p50 / {after['keyword_part_p95_ms']:.2f} ms p95. This is measured directly; it is not a subtraction of medians."]
if "after" in report["bulk"].get("cpu/semantic-mixed.json",{}):
    cpu=report["bulk"]["cpu/semantic-mixed.json"]["after"]["projection_130k_minutes"]
    metal=report["bulk"]["metal/semantic-mixed.json"]["after"]["projection_130k_minutes"]
    lines += ["", f"At the measured mixed-length rate, 130,000 rows project to **{metal:.1f} minutes on Metal** and **{cpu:.1f} minutes on CPU**. These are model-only estimates, including tokenization but excluding indexing/publication, with one window per row."]
(root/"results.md").write_text("\n".join(lines)+"\n")
print(json.dumps(report,indent=2))
