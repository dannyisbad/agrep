#!/usr/bin/env python3
"""Place the semantic score bands against a committed text baseline.

Measures, on the pinned model, the three cosine levels the bands must
separate: unrelated chat rows, rows sharing one content noun, and true
paraphrases. Texts embed alone (the query path); the drift line re-embeds
them through the padded row path. CPU lane, no network: refuses when the
model is not cached.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "py"
if str(PY) not in sys.path:
    sys.path.insert(0, str(PY))

import embedder  # noqa: E402
import surface_policy as surface  # noqa: E402

FIXTURE = ROOT / "bench" / "fixtures" / "semantic_calibration.json"


def _band(scores: list[float]) -> dict:
    return {"n": len(scores), "min": min(scores),
            "mean": statistics.fmean(scores), "max": max(scores),
            "scores": scores}


_LEVELS = (("same_topic", "same-topic pairs"),
           ("shared_noun", "shared-noun pairs"),
           ("paraphrase", "paraphrase pairs"))


def measure(model, fixture: dict) -> dict:
    unrelated = list(fixture["unrelated"])
    levels = {name: [tuple(pair) for pair in fixture[name]] for name, _ in _LEVELS}
    texts = list(dict.fromkeys(
        [*unrelated, *itertools.chain.from_iterable(
            pair for pairs in levels.values() for pair in pairs)]))
    alone = {text: model.embed_query(text) for text in texts}
    batched = dict(zip(texts, model.embed_texts(texts)))

    def cosine(a: str, b: str) -> float:
        return float(alone[a] @ alone[b])

    unrelated_pairs = list(itertools.combinations(unrelated, 2))
    pairs = [*unrelated_pairs, *itertools.chain.from_iterable(levels.values())]
    bands = surface.DEFAULT_SEMANTIC_SCORE_BANDS
    return {
        "model": embedder.PROFILE["id"],
        "lane": model.lane,
        "unrelated": _band([cosine(a, b) for a, b in unrelated_pairs]),
        **{name: _band([cosine(a, b) for a, b in level_pairs])
           for name, level_pairs in levels.items()},
        "bands": {"floor": bands.floor, "strong": bands.strong},
        "batch_drift": {
            "self_cosine_min": min(float(alone[t] @ batched[t]) for t in texts),
            "pair_shift_max": max(abs(cosine(a, b) - float(alone[a] @ batched[b]))
                                  for a, b in pairs),
        },
    }


def render(report: dict) -> str:
    lines = [f"model {report['model']} lane {report['lane']}"]
    for name, label in (("unrelated", "unrelated pairs"), *_LEVELS):
        band = report[name]
        lines.append(
            f"{label:<18} n={band['n']:<4} min {band['min']:.3f}  "
            f"mean {band['mean']:.3f}  max {band['max']:.3f}")
    for name, level in report["bands"].items():
        parts = [f"{level - report['unrelated']['max']:+.3f} vs unrelated max"]
        for key, label in _LEVELS:
            band = report[key]
            over = sum(score >= level for score in band["scores"])
            parts.append(f"{label.split()[0]} {over}/{band['n']} at or above")
        lines.append(f"{name:<7} {level:.2f}: " + "; ".join(parts))
    drift = report["batch_drift"]
    lines.append(
        f"batch drift: self-cosine min {drift['self_cosine_min']:.4f}; "
        f"pair score shift max {drift['pair_shift_max']:.4f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--json", action="store_true",
                        help="emit the measurement as one JSON object")
    args = parser.parse_args()
    if not embedder.model_cached():
        print(f"refusing: the pinned model is not cached under "
              f"{embedder.model_dir()}; this bench never downloads",
              file=sys.stderr)
        return 2
    try:
        model = embedder.Embedder(download=False, lane=embedder.LANE_CPU)
    except embedder.EmbedderUnavailable as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    report = measure(model, fixture)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
