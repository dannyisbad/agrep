import subprocess,time,json,sys
from pathlib import Path
root=Path.cwd(); evidence=root/"measurements/semperf"
while True:
 live=False
 for line in subprocess.check_output(["ps","-axo","pid,command"],text=True).splitlines():
  fields=line.split(None,1)
  if len(fields)<2:continue
  args=fields[1].split()
  if len(args)>2 and args[1]=="scripts/semantic_queries.py" and "measurements/semperf/query-reference.jsonl" in args:live=True
 if not live:break
 time.sleep(5)
rows=[json.loads(l) for l in (evidence/"query-reference.jsonl").read_text().splitlines()]
assert len(rows)==60,"Reference query did not complete; inspect raw failure before proceeding"
assert (evidence/"query-reference.summary.json").exists()
subprocess.run([sys.executable,"scripts/semantic_report.py"],check=True,stdout=(evidence/"report-generation.log").open("w"))
p=root/"docs/SEMANTIC-PERFORMANCE.md";s=p.read_text();start="<!-- final-results -->";end="<!-- /final-results -->"
a=s.index(start)+len(start);b=s.index(end);s=s[:a]+"\n"+(evidence/"results.md").read_text()+"\n"+s[b:];p.write_text(s)
print("REPORT READY",flush=True)
