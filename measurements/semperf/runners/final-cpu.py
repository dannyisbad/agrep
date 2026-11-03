import os,subprocess,sys
py="/Users/runner/.local/share/uv/tools/agrep/bin/python"
engines=[("reference","reference-","scratch/reference-model","scratch/bin/semantic-before"),("before","candle-","scratch/model","scratch/bin/semantic-before"),("after","candle-","scratch/model","scratch/bin/semantic-after")]
for device in ["metal","cpu"]:
 for fixture in ["semantic-batch","semantic-mixed"]:
  for round_no in range(3):
   for label,prefix,model,binary in engines[round_no:]+engines[:round_no]:
    env=dict(os.environ,AGREP_BENCH_ROUND=str(round_no))
    command=[py,"scripts/semantic_compare.py","--engine",prefix+device,"--binary",binary,"--fixture","fixtures/"+fixture+".json","--model",model,"--reference","/Users/runner/.agrep-p8/integ","--output","measurements/semperf/matched-"+label+".jsonl","--batches","8","--samples","1","--blas-threads","1","--max-load","12"]
    subprocess.run(command,env=env,check=True)
