#!/usr/bin/env python3
"""Development-only 0.4.9/0.5 surface comparison, always using disposable homes."""
import argparse,json,os,pathlib,shutil,subprocess,tempfile
p=argparse.ArgumentParser();p.add_argument('--binary',default='target/debug/agrep');p.add_argument('--reference',required=True);p.add_argument('--python',required=True);p.add_argument('--output',required=True);a=p.parse_args()
binary=str(pathlib.Path(a.binary).resolve());reference=pathlib.Path(a.reference)
rows=[]
for fixture in ['pi','codex','claude','gemini']:
 with tempfile.TemporaryDirectory(prefix='agrep-surface-compare-') as tmp:
  root=pathlib.Path(tmp);home=root/'home';shutil.copytree(pathlib.Path('crates/agrep/tests/fixtures')/fixture/'home',home)
  ref=[a.python,str(reference/'cli.py')];new=[binary]
  env={'PATH':os.environ.get('PATH',''),'HOME':str(home),'AGREP_HOME':str(home),'AGREP_CALLER_PUBLICATION_DIR':str(root/'empty-callers'),'AGREP_PROFILE':'classic','AGREP_NO_DAEMON':'1','TZ':'UTC','AGREP_RS_BIN':str(pathlib.Path(a.python).parent.parent/'lib/python3.14/site-packages/agrep/_bin/agrep-rs')}
  def run(cmd,args,lane,profile='classic'):
   e={**env,'AGREP_DATA_DIR':str(root/lane),'AGREP_PROFILE':profile};r=subprocess.run(['nice','-n','10',*cmd,*args],env=e,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   return {'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
  for cmd,lane in [(ref,'old'),(new,'new')]:
   r=run(cmd,['index'],lane)
   if r['exit']:raise RuntimeError(f'{fixture} {lane}: {r}')
  data=[]
  for line in (pathlib.Path('crates/agrep/tests/fixtures')/fixture/'golden.txt').read_text().splitlines():
   try:v=json.loads(line)
   except ValueError:continue
   if v.get('text') and v.get('who')=='user':data.append(v)
  row=data[0];query=row['text'].split()[0];session=row['session'];turn=str(row['turn'])
  commands=[(['search',query,'--flat'],'classic'),(['search',query,'--json'],'classic'),(['search',query,'-c'],'classic'),(['search',query,'--count-by-tier'],'classic'),(['search',query,'--color','always','--classic'],'classic'),(['search',query,'--lexical'],'compact'),(['around',session,turn],'classic'),(['around',session,turn,'--json'],'classic'),(['around',session,turn,'--full'],'classic'),(['recall',query,'--lexical'],'classic'),(['recall',query,'--lexical','--json'],'classic'),(['recall',query,'--lexical','--probe'],'classic')]
  for args,profile in commands:
   old=run(ref,args,'old',profile);current=run(new,args,'new',profile)
   rows.append({'fixture':fixture,'args':args,'profile':profile,'reference':old,'current':current,'stdout_equal':old['stdout']==current['stdout'],'exit_equal':old['exit']==current['exit']})
pathlib.Path(a.output).write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'cases':len(rows),'stdout_equal':sum(r['stdout_equal'] for r in rows),'exit_equal':sum(r['exit_equal'] for r in rows)}))
