#!/usr/bin/env python3
"""Matched-fixture 0.4.9/Candle measurements; every sample is appended and flushed.

Run with nice 10 and the reference Python environment. All paths are explicit;
the reference checkout is imported read-only, never its installed CLI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from semantic_conditions import wait_for_load


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["reference-cpu", "reference-metal", "candle-cpu", "candle-metal"], required=True)
    ap.add_argument("--fixture", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--binary", type=Path)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--blas-threads", type=int, default=1)
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--batches", type=int, default=8)
    ap.add_argument("--token-budget", type=int, default=1024, help="0.4.9 scheduling budget; its product default is 1024")
    ap.add_argument("--batch-tokens", type=int, help="Candle scheduling-only sweep")
    ap.add_argument("--max-load", type=float, default=os.cpu_count(), help="Wait before CPU samples when the one-minute load exceeds this value")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    assert 1 <= a.threads <= 6 and a.samples > 0 and a.batches > 0
    assert os.environ.get("AGREP_HOME") and os.environ.get("AGREP_DATA_DIR")
    assert os.getpriority(os.PRIO_PROCESS, 0) >= 10
    blas_threads = a.blas_threads or a.threads
    assert 1 <= blas_threads <= 6
    os.environ.update(AGREP_MODEL_DIR=str(a.model.resolve()), AGREP_SEM_THREADS=str(a.threads), RAYON_NUM_THREADS=str(a.threads), VECLIB_MAXIMUM_THREADS=str(blas_threads), TOKENIZERS_PARALLELISM="false", PYTHONDONTWRITEBYTECODE="1")
    texts = json.loads(a.fixture.read_text())
    if a.engine.startswith("reference") and not a.child:
        for sample in range(a.samples):
            env = dict(os.environ, AGREP_BENCH_SAMPLE=str(sample))
            child = subprocess.run([sys.executable, *sys.argv, "--child", "--samples", "1"], env=env)
            if child.returncode:
                raise SystemExit(child.returncode)
        return
    base = dict(engine=a.engine, fixture=a.fixture.name, sha256=hashlib.sha256(a.fixture.read_bytes()).hexdigest(), rows_per_batch=len(texts), threads=a.threads, blas_threads=blas_threads, batches=a.batches, nice=os.getpriority(os.PRIO_PROCESS, 0), token_budget=a.token_budget if a.engine.startswith("reference") else None, campaign_round=os.environ.get("AGREP_BENCH_ROUND"))
    model = None
    if a.engine.startswith("reference"):
        sys.path.insert(0, str(a.reference.resolve() / "py"))
        import embedder
        tick = time.perf_counter()
        model = embedder.Embedder(download=False, lane=embedder.LANE_METAL if a.engine.endswith("metal") else embedder.LANE_CPU)
        base["load_s"] = time.perf_counter() - tick
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("a") as out:
        for sample in range(a.samples):
            waited = wait_for_load(a.output, a.max_load) if a.engine.endswith('cpu') else 0
            record = dict(base, sample=int(os.environ.get("AGREP_BENCH_SAMPLE", sample)), uptime=subprocess.check_output(["uptime"], text=True).strip(), load_before=os.getloadavg())
            record.update(start_load_limit=a.max_load if a.engine.endswith('cpu') else None, waited_s=waited)
            tick = time.perf_counter()
            if model is not None:
                seconds = []
                for _ in range(a.batches):
                    start = time.perf_counter()
                    vectors = model.embed_texts(texts, token_budget=a.token_budget)
                    assert vectors.shape == (len(texts), 384)
                    seconds.append(time.perf_counter() - start)
                record.update(batch_seconds=seconds, inference_s=sum(seconds), rows_s=len(texts)*a.batches/sum(seconds), exit=0)
            else:
                command = [str(a.binary.resolve()), str(a.model.resolve()), a.engine.split("-")[1], "batch", "1024", str(a.batches), str(a.fixture.resolve())]
                if a.batch_tokens:
                    command.append(str(a.batch_tokens))
                record["batch_tokens"] = a.batch_tokens
                p = subprocess.run(command, capture_output=True, text=True)
                record.update(exit=p.returncode, stderr=p.stderr, stdout=p.stdout)
                if p.returncode == 0:
                    record.update(json.loads(p.stdout))
            record.update(wall_s=time.perf_counter()-tick, load_after=os.getloadavg())
            out.write(json.dumps(record)+"\n")
            out.flush()
            print(json.dumps(record), flush=True)
            if record["exit"]:
                raise SystemExit(record["exit"])


if __name__ == "__main__":
    main()
