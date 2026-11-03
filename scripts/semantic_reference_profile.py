#!/usr/bin/env python3
"""Profile the unmodified 0.4.9 embedders; retain numeric operation aggregates only."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

p=argparse.ArgumentParser()
p.add_argument('--reference',type=Path,required=True)
p.add_argument('--fixture',type=Path,required=True)
p.add_argument('--device',choices=['cpu','metal'],required=True)
p.add_argument('--scratch',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
assert os.environ.get('AGREP_HOME') and os.environ.get('AGREP_DATA_DIR') and os.getpriority(os.PRIO_PROCESS,0)>=10
os.environ.update(AGREP_SEM_THREADS='6',RAYON_NUM_THREADS='6',VECLIB_MAXIMUM_THREADS='1',TOKENIZERS_PARALLELISM='false',PYTHONDONTWRITEBYTECODE='1')
a.scratch.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(a.reference.absolute()/'py'))
texts=json.loads(a.fixture.read_text())
report={'device':a.device,'fixture_sha256':hashlib.sha256(a.fixture.read_bytes()).hexdigest(),'rows':len(texts),'threads':6,'instrumented':True,'uptime':subprocess.check_output(['uptime'],text=True).strip(),'load_before':os.getloadavg()}
operations=collections.defaultdict(lambda:{'calls':0,'wall_ms':0.,'cpu_ms':0.})
if a.device=='cpu':
 import onnxruntime as ort
 original=ort.InferenceSession;sessions=[]
 def session(path,sess_options=None,*args,**kwargs):
  opts=sess_options or ort.SessionOptions();opts.enable_profiling=True;opts.profile_file_prefix=str(a.scratch/'ort');opts.intra_op_num_threads=6;opts.inter_op_num_threads=1
  value=original(path,opts,*args,**kwargs);sessions.append(value);return value
 ort.InferenceSession=session
 import embedder
 model=embedder.Embedder(download=False,lane=embedder.LANE_CPU)
else:
 import mlx.core as mx
 import mlx.nn as nn
 import mlx_modernbert
 import embedder
 model=embedder.Embedder(download=False,lane=embedder.LANE_METAL)
 names={}
 for layer in model._metal.model.layers:
  for obj,name in [(layer.attn.Wqkv,'qkv'),(layer.attn.Wo,'attention_projection'),(layer.mlp.Wi,'mlp_in'),(layer.mlp.Wo,'mlp_out'),(layer.attn.rope,'rope'),(layer.mlp.act,'gelu'),(layer.attn_norm,'layer_norm'),(layer.mlp_norm,'layer_norm')]:names[id(obj)]=name
 def timed(name,fn,args,kwargs):
  arrays=[x for x in args if isinstance(x,mx.array)]
  if arrays:mx.eval(*arrays)
  start=time.perf_counter();cpu=time.process_time();result=fn(*args,**kwargs);mx.eval(result)
  entry=operations[name];entry['calls']+=1;entry['wall_ms']+=(time.perf_counter()-start)*1000;entry['cpu_ms']+=(time.process_time()-cpu)*1000
  return result
 for cls,default in [(nn.Linear,'linear'),(nn.LayerNorm,'outer_norm'),(nn.RoPE,'rope'),(nn.GELU,'gelu'),(mlx_modernbert.ModernBertLayer,'layer_total')]:
  original=cls.__call__
  def wrapper(self,*args,_original=original,_default=default,**kwargs):return timed(names.get(id(self),_default),lambda *x,**kw:_original(self,*x,**kw),args,kwargs)
  cls.__call__=wrapper
 original_sdpa=mx.fast.scaled_dot_product_attention
 mx.fast.scaled_dot_product_attention=lambda *args,**kwargs:timed('attention',original_sdpa,args,kwargs)
start=time.perf_counter();cpu=time.process_time();model.embed_texts(texts,token_budget=1024);report['inference_ms']=(time.perf_counter()-start)*1000
report['process_cpu_ms']=(time.process_time()-cpu)*1000
if a.device=='cpu':
 for session in sessions:
  events=json.loads(Path(session.end_profiling()).read_text())
  for event in events:
   if event.get('cat')=='Node' and event.get('name','').endswith('_kernel_time'):
    op=event.get('args',{}).get('op_name','unknown');entry=operations[op];entry['calls']+=1;entry['wall_ms']+=event.get('dur',0)/1000
report['operations']=dict(operations);report['load_after']=os.getloadavg()
a.output.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'device':a.device,'operations':len(operations),'inference_ms':report['inference_ms']}))
