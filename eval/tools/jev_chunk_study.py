"""Offline pilot: classify frozen chunk samples; never changes runtime policy."""
import argparse,concurrent.futures,datetime,json,os,time,urllib.request,urllib.error
from pathlib import Path
QUESTIONS={
 'structure':{'type':'choice','instructions':'判断文本的主要组织方式，只根据给出的文本。','criteria':{
  'records':'表格、JSON行、清单等多条独立记录','narrative':'连续叙述或解释性的自然语言段落','schema':'实体、属性、关系的结构化定义或代码','metadata':'标题、文号、签批、已阅等文档元数据为主'}},
 'density':{'type':'choice','instructions':'估计这段文本涉及多少条不同的主要业务记录。重复记录只算一次；一台设备及其车站和状态算一条设备记录。不是数行数或字数。','criteria':{
  'low':'0至3条不同的主要业务记录','medium':'4至10条不同的主要业务记录','high':'11条及以上不同的主要业务记录'}},
 'dependency':{'type':'choice','instructions':'如果在段落或记录边界切分，这段材料需要多少跨段上下文才能正确理解？','criteria':{
  'local':'各记录基本独立，保留表头或标题即可','linked':'存在跨句引用、条件或关系，宜保留相邻段落','uncertain':'样本不足，不能判断'}}}
def run_model(model,url,samples,output):
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));records=[]
 for sample in samples:
  for order in ['original','reversed']:
   q=json.loads(json.dumps(QUESTIONS))
   if order=='reversed':
    for question in q.values():question['criteria']=dict(reversed(list(question['criteria'].items())))
   payload={'model':model,'state':sample['view'],'questions':q}
   headers={'Content-Type':'application/json'}
   prefix='CLEF' if model=='clef-flash' else 'NANOJEV'
   if os.environ.get(prefix+'_SERVICE_API_KEY'):headers['Authorization']='Bearer '+os.environ[prefix+'_SERVICE_API_KEY']
   row={'sample_id':sample['id'],'model':model,'order':order};start=time.monotonic()
   try:
    req=urllib.request.Request(url+'/v1/systemone',data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers)
    with opener.open(req,timeout=90) as res:row['response']=json.load(res)
   except urllib.error.HTTPError as e:row['error']={'http_status':e.code,'body':e.read().decode()[:500]}
   except Exception as e:row['error']={'type':type(e).__name__}
   row['elapsed_ms']=round((time.monotonic()-start)*1000,2);records.append(row)
   output.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf8')
  print(datetime.datetime.now().isoformat(timespec='seconds'),model,sample['id'],'done',flush=True)
 return records
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--dataset',required=True);parser.add_argument('--output',required=True);parser.add_argument('--questions');args=parser.parse_args()
 if args.questions: QUESTIONS=json.loads(Path(args.questions).read_text(encoding='utf-8-sig'))
 samples=json.loads(Path(args.dataset).read_text(encoding='utf-8-sig'));out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
 (out/'protocol.json').write_text(json.dumps({'questions':QUESTIONS,'orders':['original','reversed'],'samples':len(samples),'max_real_view_characters':max((len(s['view']) for s in samples if s.get('kind')=='real'),default=0),'note':'Head/middle/tail samples, not a validated optimum chunk policy.'},ensure_ascii=False,indent=2),encoding='utf8')
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
  jobs=[pool.submit(run_model,m,u,samples,out/(m+'.json')) for m,u in [('clef-flash','http://clef:8188'),('nanojev','http://nanojev:8189')]]
  for job in jobs:job.result()
