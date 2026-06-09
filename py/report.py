"""Data layer + static report builder for tilt.

`build_data()` computes the report model with a SENSIBLE rage metric:
  - "hot" message = rage_raw above the global ~p90 threshold (HOT_T)
  - rank by SHARE of hot messages (interpretable %), gated by sample size so a
    94-message agent can't top the chart on noise
Used by both the static report (build()) and the live server (server.py).

Run after: tilt index, emotion.py, vibe.py.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

import common

HOT_T = 0.15          # ~p90 of rage_raw; above this a message "reads hot"
MIN_AGENT = 500       # gate: agents need this many msgs to rank (kills tiny-sample noise)
MIN_PROJECT = 50


def _emotions() -> dict[str, dict]:
    emo = {}
    p = common.DATA_DIR / "emotions.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                emo[o["id"]] = o
    return emo


def build_data() -> dict:
    emo = _emotions()
    per_agent = defaultdict(lambda: {"msgs": 0, "hot": 0, "rage": 0.0})
    per_proj = defaultdict(lambda: {"msgs": 0, "hot": 0, "rage": 0.0, "agent": ""})
    weeks = defaultdict(lambda: {"msgs": 0, "hot": 0})
    total = 0
    with common.MESSAGES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            e = emo.get(o["id"])
            if not e:
                continue
            r = float(e.get("rage_raw", 0.0))
            hot = 1 if r > HOT_T else 0
            for d, key in ((per_agent, o.get("agent", "?")), (per_proj, o.get("project", "?"))):
                d[key]["msgs"] += 1
                d[key]["hot"] += hot
                d[key]["rage"] += r
            per_proj[o.get("project", "?")]["agent"] = o.get("agent", "?")
            ts = int(o.get("ts", 0))
            if ts > 0:
                wk = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-W%U")
                weeks[wk]["msgs"] += 1
                weeks[wk]["hot"] += hot
            total += 1

    def rows(d, gate):
        out = []
        for name, v in d.items():
            if v["msgs"] < gate:
                continue
            out.append({"name": name, "agent": v.get("agent", ""), "msgs": v["msgs"],
                        "hot_pct": round(v["hot"] / v["msgs"] * 100, 1),
                        "mean_rage": round(v["rage"] / v["msgs"], 3)})
        return sorted(out, key=lambda r: r["hot_pct"], reverse=True)

    agents = rows(per_agent, MIN_AGENT)
    projects = rows(per_proj, MIN_PROJECT)[:16]
    timeline = [{"week": w, **weeks[w]} for w in sorted(weeks)]
    timeline = [{"week": t["week"], "msgs": t["msgs"], "hot_pct": round(t["hot"] / t["msgs"] * 100, 1)}
                for t in timeline if t["msgs"] >= 10]

    traces = []
    vdir = common.DATA_DIR / "vibe"
    idx = vdir / "index.json"
    if idx.exists():
        for entry in json.loads(idx.read_text(encoding="utf-8")):
            p = vdir / f"{entry['session']}.json"
            if p.exists():
                traces.append(json.loads(p.read_text(encoding="utf-8")))

    overall_hot = round(sum(a["hot_pct"] * a["msgs"] for a in agents) / max(1, sum(a["msgs"] for a in agents)), 1)
    return {"agents": agents, "projects": projects, "timeline": timeline, "traces": traces,
            "total": total, "overall_hot": overall_hot, "hottest": agents[0]["name"] if agents else "-"}


def build():
    data = build_data()
    tpl = (common.PY_DIR.parent / "web" / "report.template.html").read_text(encoding="utf-8")
    out = tpl.replace("/*__DATA__*/", json.dumps(data).replace("</", "<\\/"))
    dist = common.PY_DIR.parent / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "report.html").write_text(out, encoding="utf-8")
    print(f"  wrote {dist/'report.html'} | {data['total']} msgs · {len(data['agents'])} agents · "
          f"{len(data['projects'])} projects · {len(data['traces'])} traces · {data['overall_hot']}% hot overall")


if __name__ == "__main__":
    build()
