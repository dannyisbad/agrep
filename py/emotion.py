"""Affect GATE for tilt: per-message graded emotion scores -> data/emotions.jsonl.

This is the cheap, always-on first pass that decides how each message "reads"
emotionally and which messages are ambiguous enough to escalate. It uses a
GoEmotions text-classification model (multi-label, 28 emotions + neutral).

  PRIMARY  : cirimus/modernbert-base-go-emotions
  FALLBACK : SamLowe/roberta-base-go_emotions

Both expose the standard 28 GoEmotions labels, so the rage/hype groupings below
are identical regardless of which one loads.

For each message we emit one line of data/emotions.jsonl:
  {
    "id": "<agent:session:turn>",
    "rage_raw":  anger+annoyance+disapproval+disgust+disappointment,
    "hype_raw":  excitement+admiration+joy+approval+amusement,
    "top":       ["label", ...]   # top-3 emotion labels by score
    "routed_to_judge": bool        # profanity present AND ambiguous
  }

Scores are the model's per-label sigmoid probabilities (graded, NOT argmax),
summed within each group. They are intentionally NOT clamped to [0,1]; a hot
message can score high on several rage labels at once, and downstream tilt math
expects the raw sum.

ROUTING: `routed_to_judge` marks messages the gate can't confidently read on
its own, to be handed to the LLM judge. A message routes iff BOTH hold:
  (a) profanity is present  -- swearing without clear affect is the classic
      ambiguous case ("this is fucking brilliant" vs "this fucking sucks"); and
  (b) the affect read is ambiguous -- rage and hype are close together, or both
      are weak, so the gate can't tell rage from hype.
Profanity can be supplied per-id by an upstream stage (see --profanity-ids) or
recomputed here from a small built-in lexicon.

================================ IMPORTANT ================================
The LLM judge is the NEXT, SEPARATE stage. The plan is to feed the messages
flagged with routed_to_judge=True to an LLM (Qwen3.5-4B) for a final graded
affect verdict. That judge is NOT implemented in this file -- emotion.py only
GATES and routes. Wiring up Qwen3.5-4B is later work.
==========================================================================

Usage:
  python emotion.py
  python emotion.py --smoke 16
  python emotion.py --profanity-ids data/profanity.ids   # newline-delimited ids
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import common

PRIMARY = "cirimus/modernbert-base-go-emotions"
FALLBACKS = ["SamLowe/roberta-base-go_emotions"]
CANDIDATES = [PRIMARY] + FALLBACKS

# GoEmotions groupings the tilt scoring layer expects. Label spellings match the
# 28-label GoEmotions taxonomy used by both candidate models.
RAGE_LABELS = ("anger", "annoyance", "disapproval", "disgust", "disappointment")
HYPE_LABELS = ("excitement", "admiration", "joy", "approval", "amusement")

# Ambiguity thresholds for routing. Tunable; v0.5 heuristic.
#   - if the dominant group barely leads the other, the read is mixed
#   - if both groups are weak, there's not enough signal to trust the gate
AMBIG_MARGIN = 0.15   # |rage_raw - hype_raw| <= this  => too close to call
AMBIG_FLOOR = 0.30    # max(rage_raw, hype_raw) < this  => too weak to call

# Minimal profanity lexicon used only when no upstream profanity set is given.
# This is deliberately small (v0.5); the real signal is expected to come in via
# --profanity-ids from tier-0.
#
# Two tiers, both case-insensitive:
#   _PROFANITY_STEMS  match the stem at a left word boundary, allowing inflection
#       suffixes on the right (\w*), so "fuck" hits "fucking"/"fucked"/"fucker".
#       The left \b keeps "ass" out of "class"/"pass" since those have no
#       boundary before the substring. Only stems that won't over-match in
#       normal dev chat belong here.
#   _PROFANITY_EXACT  whole-word matches only (\b...\b), for short/ambiguous
#       tokens like "hell"/"damn" that would over-fire as prefixes
#       ("hello", " damning praise" is rare enough to ignore at v0.5).
_PROFANITY_STEMS = (
    "fuck", "shit", "bitch", "bastard", "dick", "bullshit", "goddamn", "asshole",
)
_PROFANITY_EXACT = (
    "ass", "damn", "crap", "hell", "piss", "wtf",
)
_PROFANITY_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) + r"\w*" for w in _PROFANITY_STEMS)
    + r"|(?:" + "|".join(re.escape(w) for w in _PROFANITY_EXACT) + r")\b"
    + r")",
    re.IGNORECASE,
)


def has_profanity(text: str) -> bool:
    return bool(_PROFANITY_RE.search(text))


def is_ambiguous(rage_raw: float, hype_raw: float) -> bool:
    """True when the gate can't confidently call rage vs hype."""
    if max(rage_raw, hype_raw) < AMBIG_FLOOR:
        return True
    if abs(rage_raw - hype_raw) <= AMBIG_MARGIN:
        return True
    return False


def build_loader(device: str, max_len: int = 256):
    """Return loader(model_id) -> transformers text-classification pipeline.

    return_all_scores/top_k=None gives the full per-label distribution (graded
    multi-label), not just the argmax. function_to_apply='sigmoid' is the
    correct activation for GoEmotions multi-label heads (independent per-label
    probabilities, not a softmax over labels).

    `max_len` caps the tokenizer so a giant pasted message can't blow up the
    attention matrix (ModernBERT's native max is 8192; one long message in a
    batch OOMs a 10GB GPU). Emotion lives in the gist, so 256 tokens is plenty.
    """
    from transformers import pipeline  # local import: heavy

    def loader(model_id: str):
        import torch  # local import: torch may be installed after this module

        device_index = 0 if device == "cuda" else -1
        dtype = torch.float16 if device == "cuda" else None
        clf = pipeline(
            task="text-classification",
            model=model_id,
            tokenizer=model_id,
            device=device_index,
            top_k=None,                       # return scores for ALL labels
            function_to_apply="sigmoid",      # multi-label GoEmotions head
            truncation=True,
            torch_dtype=dtype,
        )
        # cap truncation length: with truncation=True the tokenizer truncates to
        # model_max_length, so this bounds batch x seq^2 attention memory.
        clf.tokenizer.model_max_length = max_len
        return clf

    return loader


def score_to_dict(scored) -> dict[str, float]:
    """Normalize one pipeline output (list of {label,score}) to {label: score}.

    With top_k=None the pipeline returns, per input, a list of dicts. We lower
    the label so RAGE_LABELS/HYPE_LABELS match regardless of model casing.
    """
    return {item["label"].lower(): float(item["score"]) for item in scored}


def group_sum(scores: dict[str, float], labels: tuple[str, ...]) -> float:
    return float(sum(scores.get(lbl, 0.0) for lbl in labels))


def top_k_labels(scores: dict[str, float], k: int = 3) -> list[str]:
    return [lbl for lbl, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]]


def load_profanity_ids(path: Path | None) -> set[str] | None:
    """Load an upstream set of message ids known to contain profanity. None
    means 'not supplied' -> recompute profanity from text here."""
    if path is None:
        return None
    if not path.exists():
        common.log(f"warn: --profanity-ids {path} not found; recomputing from text.")
        return None
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Affect gate -> emotions.jsonl")
    ap.add_argument("--smoke", type=int, metavar="N", default=None,
                    help="Process only the first N messages.")
    ap.add_argument("--batch-size", type=int, default=None,
                    help="Classification batch size. Default 64 (cuda) / 16 (cpu).")
    ap.add_argument("--profanity-ids", type=Path, default=None,
                    help="Optional newline-delimited file of message ids that "
                         "contain profanity (from an upstream stage). If absent, "
                         "profanity is recomputed from a built-in lexicon.")
    ap.add_argument("--full", action="store_true",
                    help="Re-score every message. Default is incremental: append only "
                         "messages not already in emotions.jsonl.")
    args = ap.parse_args()

    device = common.pick_device()
    if device == "cuda":
        try:
            import torch

            # OOM cleanly in-process rather than spilling to Windows shared memory.
            torch.cuda.set_per_process_memory_fraction(0.85)
        except Exception:  # noqa: BLE001
            pass
    batch_size = args.batch_size if args.batch_size else (32 if device == "cuda" else 16)
    profanity_ids = load_profanity_ids(args.profanity_ids)

    msgs = list(common.iter_messages(limit=args.smoke))
    if not msgs:
        common.log("error: no messages to score (messages.jsonl empty or missing).")
        return 1

    # --- incremental: only score messages not already in emotions.jsonl ---
    incremental = not args.full and args.smoke is None and common.EMOTIONS_PATH.exists()
    if incremental:
        done = common.jsonl_ids(common.EMOTIONS_PATH)
        before = len(msgs)
        msgs = [m for m in msgs if m.id not in done]
        common.log(f"incremental: {len(done)} already scored, {len(msgs)} new (of {before})")
        if not msgs:
            common.log("emotions already up to date (0 new).")
            return 0

    common.log(f"device={device} batch_size={batch_size} messages={len(msgs)}"
               + (f" (smoke, first {args.smoke})" if args.smoke else ""))

    resolved = common.resolve_model(CANDIDATES, build_loader(device), label="emotion")
    clf = resolved.obj

    t0 = time.perf_counter()
    routed_count = 0
    written = 0

    common.EMOTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # incremental appends to the live file; full writes a temp then atomically swaps.
    tmp = common.EMOTIONS_PATH if incremental else common.EMOTIONS_PATH.with_suffix(
        common.EMOTIONS_PATH.suffix + ".tmp")

    with tmp.open("a" if incremental else "w", encoding="utf-8", newline="\n") as out:
        for batch in common.iter_batches(msgs, batch_size):
            texts = [m.text for m in batch]
            # The pipeline truncates long inputs (truncation=True). With top_k=None
            # it returns one list-of-{label,score} per input.
            results = clf(texts, batch_size=len(texts))
            # A single-input call can return a flat list rather than a list of
            # lists; normalize so we always iterate per-message.
            if results and isinstance(results[0], dict):
                results = [results]

            for m, scored in zip(batch, results):
                scores = score_to_dict(scored)
                rage_raw = group_sum(scores, RAGE_LABELS)
                hype_raw = group_sum(scores, HYPE_LABELS)
                top = top_k_labels(scores, 3)

                if profanity_ids is not None:
                    prof = m.id in profanity_ids
                else:
                    prof = has_profanity(m.text)

                routed = bool(prof and is_ambiguous(rage_raw, hype_raw))
                if routed:
                    routed_count += 1

                out.write(json.dumps({
                    "id": m.id,
                    "rage_raw": round(rage_raw, 6),
                    "hype_raw": round(hype_raw, 6),
                    "top": top,
                    "routed_to_judge": routed,
                }, ensure_ascii=False))
                out.write("\n")
                written += 1

    if not incremental:
        tmp.replace(common.EMOTIONS_PATH)
    elapsed = time.perf_counter() - t0

    vram = common.approx_vram_mb()
    vram_str = f"{vram:.0f} MiB" if vram is not None else "n/a (cpu)"
    common.log(
        "emotion done | "
        f"model={resolved.id} | device={device} | count={written} | "
        f"routed_to_judge={routed_count} | elapsed={elapsed:.2f}s | approx_vram={vram_str}"
    )
    common.log(f"wrote {common.EMOTIONS_PATH}")
    common.log("note: messages with routed_to_judge=true go to the LLM judge "
               "(Qwen3.5-4B) in a later, separate stage -- not run here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
