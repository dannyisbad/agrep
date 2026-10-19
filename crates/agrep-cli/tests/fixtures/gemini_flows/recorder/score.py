"""score.py BIN OUT [SHOW]: index OUT/hash8888synthetic (see fuzz.ts) with the agrep-rs binary BIN in a
sandboxed home and data dir, then compare every session with OUT/oracle.json; mismatches go to
OUT/mismatches.json. Exit 0 when all match."""
import json
import os
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    binary, out = sys.argv[1], sys.argv[2]
    show = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    oracle = json.load(open(os.path.join(out, "oracle.json"), encoding="utf-8"))
    sandbox = tempfile.mkdtemp(prefix="gemini-fuzz-")
    try:
        home = os.path.join(sandbox, "h")
        data = os.path.join(sandbox, "d")
        os.makedirs(os.path.join(home, ".gemini", "tmp"))
        os.makedirs(os.path.join(sandbox, "t"))
        os.makedirs(data)
        shutil.copytree(os.path.join(out, "hash8888synthetic"),
                        os.path.join(home, ".gemini", "tmp", "hash8888synthetic"))
        env = {"HOME": home, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": os.path.join(sandbox, "t"),
               "AGREP_HOME": home, "AGREP_DATA_DIR": data}
        run = subprocess.run([binary, "index", "--agent", "gemini", "--full"], env=env, capture_output=True,
                             text=True, timeout=600)
        if run.returncode:
            print(run.stdout[-2000:], run.stderr[-2000:])
            return 2
        replies = {}
        for line in open(os.path.join(data, "replies.jsonl"), encoding="utf-8"):
            row = json.loads(line)
            replies[row["id"]] = row["reply"]
        rows: dict[str, list] = {}
        for line in open(os.path.join(data, "messages.jsonl"), encoding="utf-8"):
            row = json.loads(line)
            rows.setdefault(row["session"], []).append(row)
        events: dict[str, list] = {}
        store = os.path.join(data, "events", ".store.sqlite3")
        if os.path.exists(store):
            import sqlite3
            con = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
            for name, payload in con.execute("SELECT name, payload FROM event_sessions"):
                session = name[len("gemini-"):].split("--")[0]
                for line in bytes(payload).decode("utf-8").splitlines():
                    if line.strip():
                        events.setdefault(session, []).append(json.loads(line)["call_id"])
            con.close()
        bad = []
        for sid, case in oracle.items():
            got = [[r["who"], r["text"], replies.get(r["id"], "")]
                   for r in sorted(rows.get(sid, []), key=lambda r: r["turn"])]
            want = [[w, t, " ".join(r.split())] for w, t, r in case["rows"]]
            got = [[w, t, " ".join(r.split())] for w, t, r in got]
            got_events = sorted(events.get(sid, []))
            if got != want or got_events != case["events"]:
                bad.append((case["flow"], case["ops"], want, got, case["events"], got_events))
        print(f"{len(oracle) - len(bad)}/{len(oracle)} sessions match the oracle; {len(bad)} differ")
        for flow, ops, want, got, we, ge in bad[:show]:
            print(f"--- {flow}: {' '.join(ops)}")
            if want != got:
                print(f"  want rows   {want}")
                print(f"  got  rows   {got}")
            if we != ge:
                print(f"  want events {we}\n  got  events {ge}")
        json.dump([{"flow": f, "ops": o, "want": w, "got": g, "want_events": we, "got_events": ge}
                   for f, o, w, g, we, ge in bad], open(os.path.join(out, "mismatches.json"), "w"), indent=1)
        return 1 if bad else 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
