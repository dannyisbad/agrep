"""Concept threads: cluster SESSIONS by semantic content, independent of cwd.

Fixes the cwd-bucketing problem: agents `mkdir x && cd x`, so generic buckets
(Desktop, Users/Danny) lump hundreds of unrelated threads together while one real
effort scatters across many cwds. We regroup by what each session is actually ABOUT.

Pipeline:
  1. load message embeddings (data/embeddings.f32 + .ids) + messages.jsonl meta
  2. per-session centroid = mean of its message vectors, renormalized
  3. PCA -> ~50d, then HDBSCAN over the centroids (unit vectors -> Euclidean ~ cosine)
  4. label each cluster by top c-TF-IDF terms of its messages
  5. write data/concepts.json + data/session_concepts.jsonl, print a summary

Run AFTER `tilt embed`. No LLM here (that's the optional summary pass); this is the
instant, centroid-based concept layer.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict

import numpy as np

import common


def cluster_summaries(args) -> int:
    """Cluster the per-session SUMMARY embeddings into topic threads.

    The message-centroid path (below) averages every message vector of a session, so long
    coding sessions all collapse toward one generic centroid and pile into a vague catch-all
    ("Opencode Workflow Engineering", ~109 chats of unrelated work). Each session summary is
    instead a single clean topic sentence, so clustering `summary_emb.*` groups by what the
    chat was actually about. We partition (Ward, fixed K) rather than HDBSCAN so every
    browsable chat lands in a coherent topic instead of a giant "misc" noise bin.

    Only `concepts.json` + `session_concepts.jsonl` are rewritten. `session_emb.*` /
    `session_snip.jsonl` (message centroids, consumed by chat-level search) are left intact.
    """
    import warnings
    warnings.filterwarnings("ignore")
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer

    emb_meta = common.DATA_DIR / "summary_emb.meta"
    if not emb_meta.exists():
        print("  no summary_emb.* — run the summary embed step first; falling back to centroids")
        return cluster_centroids(args)
    dim = int(emb_meta.read_text().strip())
    ids = (common.DATA_DIR / "summary_emb.ids").read_text(encoding="utf-8").splitlines()
    mat = np.fromfile(common.DATA_DIR / "summary_emb.f32", dtype="<f4").reshape(-1, dim)

    summ: dict[str, dict] = {}
    for line in (common.DATA_DIR / "summaries.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            summ[o["session"]] = o
    sessions = [s for s in ids if s in summ]
    rows = [i for i, s in enumerate(ids) if s in summ]
    mat = mat[rows]
    if len(sessions) <= args.k:
        print(f"  only {len(sessions)} summarized sessions; need > k={args.k}")
        return 1

    norm = mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9, None)
    K = min(args.k, len(sessions) - 1)
    common.log(f"summary clustering: {len(sessions)} sessions -> {args.method} K={K}")
    if args.method == "kmeans":
        labels = KMeans(n_clusters=K, n_init=10, random_state=0).fit_predict(norm)
    else:
        labels = AgglomerativeClustering(n_clusters=K, linkage="ward").fit_predict(norm)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        clusters[int(lab)].append(i)
    cluster_ids = sorted(clusters)

    # label each cluster by top c-TF-IDF terms of its member SUMMARIES (clean topic text)
    docs = [" ".join(summ[sessions[si]].get("summary", "") for si in clusters[c]) for c in cluster_ids]
    labels_terms: dict[int, list[str]] = {}
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1,
                          token_pattern=r"[A-Za-z][A-Za-z0-9_+\-]{2,}")
    X = vec.fit_transform(docs)
    terms = np.array(vec.get_feature_names_out())
    for row, c in enumerate(cluster_ids):
        top = X[row].toarray().ravel().argsort()[::-1][:6]
        labels_terms[c] = [terms[j] for j in top if X[row, j] > 0]

    def n_msgs(si):
        return int(summ[sessions[si]].get("n_msgs", 0))

    records = []
    for c in cluster_ids:
        members = clusters[c]
        agents = Counter(summ[sessions[si]].get("agent", "") for si in members)
        cwds = Counter(summ[sessions[si]].get("cwd_project", "") for si in members)
        records.append({
            "concept_id": c,
            "label": ", ".join(labels_terms.get(c, [])[:5]) or f"concept-{c}",
            "terms": labels_terms.get(c, []),
            "n_sessions": len(members),
            "n_messages": sum(n_msgs(si) for si in members),
            "agents": dict(agents),
            "cwd_buckets": dict(cwds.most_common(6)),
        })
    records.sort(key=lambda r: r["n_sessions"], reverse=True)

    (common.DATA_DIR / "concepts.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    with (common.DATA_DIR / "session_concepts.jsonl").open("w", encoding="utf-8") as f:
        for i, s in enumerate(sessions):
            c = int(labels[i])
            f.write(json.dumps({
                "session": s, "concept_id": c,
                "label": ", ".join(labels_terms.get(c, [])[:5]) or f"concept-{c}",
                "agent": summ[s].get("agent", ""), "cwd_project": summ[s].get("cwd_project", ""),
                "n_msgs": int(summ[s].get("n_msgs", 0)),
            }) + "\n")

    sizes = sorted((len(clusters[c]) for c in cluster_ids), reverse=True)
    print(f"\n  tilt concepts (summary) · {len(sessions)} chats -> {len(cluster_ids)} topics, "
          f"sizes {sizes}")
    print(f"  {'#sess':>5} {'#msgs':>7}  topic (top terms)")
    for r in records[:args.top]:
        print(f"  {r['n_sessions']:>5} {r['n_messages']:>7}  {r['label'][:54]}")
    print("\n  next: python label_concepts.py   (Gemma4 names) then restart the server\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="cluster sessions into concept threads")
    ap.add_argument("--source", choices=["summary", "centroid"], default="summary",
                    help="summary = cluster per-session summary embeddings (clean topics, default); "
                         "centroid = legacy message-centroid clustering")
    ap.add_argument("--method", choices=["ward", "kmeans"], default="ward",
                    help="partition method for --source summary")
    ap.add_argument("--k", type=int, default=24, help="number of topics for --source summary")
    ap.add_argument("--min-cluster-size", type=int, default=8)
    ap.add_argument("--pca-dim", type=int, default=50)
    ap.add_argument("--selection", choices=["eom", "leaf"], default="eom",
                    help="HDBSCAN cluster selection (centroid source only).")
    ap.add_argument("--sample-per-session", type=int, default=30,
                    help="max messages per session fed to the label TF-IDF")
    ap.add_argument("--top", type=int, default=20, help="how many concepts to print")
    args = ap.parse_args()

    if args.source == "summary":
        return cluster_summaries(args)
    return cluster_centroids(args)


def cluster_centroids(args) -> int:

    t0 = time.perf_counter()
    ids, mat = common.read_embeddings()  # (N,), (N, dim) float32, rows L2-normalized
    row_of = {mid: i for i, mid in enumerate(ids)}

    # group rows by session + collect meta
    sess_rows: dict[str, list[int]] = defaultdict(list)
    sess_agent: dict[str, str] = {}
    sess_cwd: dict[str, str] = {}
    sess_text: dict[str, list[str]] = defaultdict(list)
    with common.MESSAGES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = o.get("id")
            r = row_of.get(mid)
            if r is None:
                continue
            sess = o.get("session") or mid
            sess_rows[sess].append(r)
            sess_agent.setdefault(sess, o.get("agent", ""))
            sess_cwd.setdefault(sess, o.get("project", ""))
            if len(sess_text[sess]) < args.sample_per_session:
                t = o.get("text", "")
                if t:
                    sess_text[sess].append(t)

    sessions = list(sess_rows.keys())
    if len(sessions) < args.min_cluster_size:
        print(f"  only {len(sessions)} sessions; need >= {args.min_cluster_size} to cluster")
        return 1

    # session centroids (mean of member message vectors, renormalized)
    cents = np.zeros((len(sessions), mat.shape[1]), dtype=np.float32)
    for i, s in enumerate(sessions):
        v = mat[sess_rows[s]].mean(axis=0)
        n = np.linalg.norm(v)
        cents[i] = v / n if n > 1e-12 else v

    common.log(f"sessions={len(sessions)} dim={mat.shape[1]} -> PCA {args.pca_dim} -> HDBSCAN")

    from sklearn.cluster import HDBSCAN
    from sklearn.decomposition import PCA

    pca_dim = min(args.pca_dim, cents.shape[1], max(2, cents.shape[0] - 1))
    red = PCA(n_components=pca_dim, random_state=0).fit_transform(cents)
    labels = HDBSCAN(min_cluster_size=args.min_cluster_size, metric="euclidean",
                     cluster_selection_method=args.selection).fit_predict(red)

    # group sessions by cluster label (-1 == noise/unclustered)
    clusters: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        clusters[int(lab)].append(i)

    # ---- label clusters via c-TF-IDF over per-cluster concatenated text ----
    from sklearn.feature_extraction.text import TfidfVectorizer

    cluster_ids = [c for c in clusters if c != -1]
    docs = []
    for c in cluster_ids:
        chunk = []
        for si in clusters[c]:
            chunk.extend(sess_text[sessions[si]])
        docs.append(" ".join(chunk)[:200_000])
    labels_terms: dict[int, list[str]] = {}
    if docs:
        vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2),
                              max_features=20000, min_df=2, token_pattern=r"[A-Za-z][A-Za-z0-9_+\-]{2,}")
        try:
            X = vec.fit_transform(docs)
            terms = np.array(vec.get_feature_names_out())
            for row, c in enumerate(cluster_ids):
                top = X[row].toarray().ravel().argsort()[::-1][:6]
                labels_terms[c] = [terms[j] for j in top if X[row, j] > 0]
        except ValueError:
            for c in cluster_ids:
                labels_terms[c] = []

    # ---- assemble concept records ----
    def msg_count(si_list):
        return sum(len(sess_rows[sessions[si]]) for si in si_list)

    records = []
    for c in cluster_ids:
        members = clusters[c]
        agents = Counter(sess_agent[sessions[si]] for si in members)
        cwds = Counter(sess_cwd[sessions[si]] for si in members)
        records.append({
            "concept_id": c,
            "label": ", ".join(labels_terms.get(c, [])[:5]) or f"concept-{c}",
            "terms": labels_terms.get(c, []),
            "n_sessions": len(members),
            "n_messages": msg_count(members),
            "agents": dict(agents),
            "cwd_buckets": dict(cwds.most_common(6)),
        })
    records.sort(key=lambda r: r["n_messages"], reverse=True)

    # write outputs
    (common.DATA_DIR / "concepts.json").write_text(
        json.dumps(records, indent=1), encoding="utf-8")
    with (common.DATA_DIR / "session_concepts.jsonl").open("w", encoding="utf-8") as f:
        for i, s in enumerate(sessions):
            c = int(labels[i])
            f.write(json.dumps({
                "session": s, "concept_id": c,
                "label": labels_terms.get(c, []) and ", ".join(labels_terms[c][:5]) or ("misc" if c == -1 else f"concept-{c}"),
                "agent": sess_agent[s], "cwd_project": sess_cwd[s],
                "n_msgs": len(sess_rows[s]),
            }) + "\n")

    # session-level vectors + snippets, for chat-level search ("what was this chat about")
    cents.astype("<f4").tofile(common.DATA_DIR / "session_emb.f32")
    (common.DATA_DIR / "session_emb.meta").write_text(str(cents.shape[1]), encoding="utf-8")
    with (common.DATA_DIR / "session_emb.ids").open("w", encoding="utf-8", newline="\n") as f:
        for s in sessions:
            f.write(s + "\n")
    with (common.DATA_DIR / "session_snip.jsonl").open("w", encoding="utf-8") as f:
        for i, s in enumerate(sessions):
            c = int(labels[i])
            samples = sess_text[s]
            rep = max(samples, key=len) if samples else ""
            concept = (", ".join(labels_terms[c][:5]) if c in labels_terms
                       else ("misc" if c == -1 else f"concept-{c}"))
            f.write(json.dumps({
                "session": s, "agent": sess_agent[s], "cwd_project": sess_cwd[s],
                "concept": concept, "n_msgs": len(sess_rows[s]),
                "snippet": " ".join(rep.split())[:400],
            }) + "\n")

    n_noise = len(clusters.get(-1, []))
    elapsed = time.perf_counter() - t0
    print(f"\n  tilt concepts · {len(sessions)} sessions -> {len(cluster_ids)} concept threads "
          f"({n_noise} unclustered) in {elapsed:.1f}s")
    print(f"  {'#msgs':>6} {'#sess':>5}  concept (top terms)                        spans cwd-buckets")
    for r in records[:args.top]:
        buckets = ", ".join(list(r["cwd_buckets"].keys())[:4])
        print(f"  {r['n_messages']:>6} {r['n_sessions']:>5}  {r['label'][:42]:<42}  {buckets[:40]}")
    print()
    # show the cwd-fix concretely: how a generic bucket fragments across concepts
    for generic in ("Users/Danny", "Desktop"):
        spread = [r for r in records if generic in r["cwd_buckets"]]
        if spread:
            print(f"  '{generic}' cwd-bucket is spread across {len(spread)} distinct concepts "
                  f"(e.g. {', '.join(r['label'][:24] for r in spread[:4])})")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
