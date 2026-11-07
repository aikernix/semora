"""Local deployment acceptance; no business writes or raw-row output."""
import asyncio,hashlib,json,os,re,sys,time
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
os.environ['AGENT_STAGED_PROTOCOL']='true'
os.environ['AGENT_TASK_MEMORY']='true'
os.environ['ALLOW_LOCAL_PLANNER_FALLBACK']='false'
from app.model_settings import model_settings
from app.store import Store
from app.workflow import Workflow
from app.ontology import ontology_service
from app.settings import settings_service
from semora_contracts import QueryPlan,AuthorizationContext

OUT=Path('/tmp/semora-live-results');OUT.mkdir(exist_ok=True)
def digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,default=str).encode()).hexdigest()
def qi(v,dialect):
 if not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',x) for x in v.split('.')):raise ValueError('unsafe published identifier')
 q='`' if dialect=='mysql' else '"'
 return '.'.join(q+x+q for x in v.split('.'))

def project(result,fields):
 indices=[]
 for field in fields:
  matches=[i for i,c in enumerate(result.columns) if c==field or c.endswith('_'+field)]
  if len(matches)!=1:raise ValueError('ambiguous result column')
  indices.append(matches[0])
 return [[row[i] for i in indices] for row in result.rows]

async def main():
 assert os.getenv('SEMANTIC_PLANNER_URL'), 'real planner URL required'
 w=Workflow(Store());settings=settings_service.get().model_dump(mode='json')
 ref,ont=await ontology_service.latest_snapshot_bundle('cross_source_demo')
 objs={o['name']:o for o in ont['objects']};r=objs['records'];l=objs['lookup']
 props=lambda obj:{p['name']:p['column'] for p in obj['properties']}
 rp,lp=props(r),props(l)
 async def execute(sql,params,source):
  return await w._remote_execute(sql,params,[],{**settings,'max_result_rows':1000,'data_source_id':source})
 async def expected(kind,n=3):
  rt=qi(r['table'],'mysql');rid=qi(rp['record_id'],'mysql');eid=qi(rp['entity_id'],'mysql')
  if kind=='top':
   result=await execute(f'SELECT {rid} AS record_id,{eid} AS entity_id FROM {rt} WHERE {eid} IS NOT NULL ORDER BY {rid} DESC LIMIT {n}',{},r['dataSourceId'])
   return result,['record_id','entity_id']
  lt=qi(l['table'],'postgres');lid=qi(lp['entity_id'],'postgres')
  keys_result=await execute(f'SELECT {lid} AS entity_id FROM {lt} WHERE {lid} IS NOT NULL ORDER BY {lid} ASC LIMIT 2',{},l['dataSourceId'])
  keys=list(dict.fromkeys(row[0] for row in keys_result.rows if row[0] is not None))
  if not keys:raise RuntimeError('cross-source lookup fixture has no keys')
  params={f'key_{i}':v for i,v in enumerate(keys)}
  placeholders=','.join('%('+key+')s' for key in params)
  result=await execute(f'SELECT {rid} AS record_id,{eid} AS entity_id FROM {rt} WHERE {eid} IN ({placeholders}) LIMIT 1000',params,r['dataSourceId'])
  if result.truncated:raise RuntimeError('reference result truncated')
  return result,['record_id','entity_id']
 top,fields=await expected('top')
 cross,_=await expected('cross')
 pre={'at':datetime.now(timezone.utc).isoformat(),'ontology':ref.model_dump(mode='json'),'rows_top':top.row_count,'rows_cross':cross.row_count,'config':{'staged':True,'task_memory':True,'fallback':False,'model_timeout':os.getenv('LLM_TIMEOUT_SECONDS','300'),'agent_timeout':os.getenv('AGENT_TIMEOUT_SECONDS','900')}}
 plan=QueryPlan(domain='cross_source_demo',data_source_id=r['dataSourceId'],intent='验收预检',metrics=[],fields=['records.record_id','records.entity_id'],filters=[{'field':'records.entity_id','operator':'is_not_null'}],order_by=[{'field':'records.record_id','direction':'desc'}],limit=3)
 artifact=await w._compile(plan,ont)
 from app.planner import adapt_mysql_artifact
 artifact=adapt_mysql_artifact(artifact)
 checked=await execute(artifact.physical_sql,artifact.parameters,r['dataSourceId'])
 pre['real_wren_top_matches_reference']=project(checked,fields)==project(top,fields)
 import httpx
 async with httpx.AsyncClient(timeout=10) as client:
  health=(await client.get(os.environ['SEMANTIC_PLANNER_URL'].rstrip('/')+'/healthz')).json()
 pre['planner_engine']=health.get('engine');pre['planner_version']=health.get('wren_core')
 pre['local_fallback']=False
 (OUT/'preflight.json').write_text(json.dumps(pre,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'event':'preflight','top_rows':top.row_count,'cross_rows':cross.row_count,'wren_match':pre['real_wren_top_matches_reference'],'engine':pre['planner_engine']}),flush=True)
 if '--preflight' in sys.argv:return
 if not pre['real_wren_top_matches_reference']:raise RuntimeError('Wren result differs from reference')
 followup=sys.argv[sys.argv.index('--followup-only')+1] if '--followup-only' in sys.argv else None
 session=w.store.sessions[followup] if followup else w.store.create_session('[验收]最新内核-真实Wren-Executor-只读','cross_source_demo','semora-core-acceptance')
 assert session.domain=='cross_source_demo'
 events=OUT/'events.jsonl'
 auth=AuthorizationContext(subject='semora-core-acceptance',username='内核只读验收',roles=['analyst'],branch_ids=[1,2,3],sensitivity_clearance='internal',allowed_data_source_ids=[r['dataSourceId'],l['dataSourceId']])
 traces=[]
 from semora_agent.model import StructuredModel
 original_request=StructuredModel._request
 async def measured(self,body,callback=None):
  def observe(name,data):
   if name in ['agent.request.completed','agent.request.timeout','agent.request.failed']:
    traces.append({k:data.get(k) for k in ['duration_ms','request_bytes','model','finish_reason','stream_end']})
   if callback:callback(name,data)
  result=await original_request(self,body,observe)
  outlines=[]
  for call in result.get('tool_calls') or []:
   try:
    args=json.loads(call['function']['arguments']);analysis=args.get('analysis') or {}
    outlines.append({'tool':call['function']['name'],'keys':list(args),'analysis_keys':list(analysis),
      'order_by':analysis.get('order_by'),'limit':analysis.get('limit')})
   except (ValueError,TypeError,KeyError): pass
  with (OUT/'public-payload-shapes.jsonl').open('a') as f:f.write(json.dumps(outlines)+'\n')
  return result
 StructuredModel._request=measured
 original=w.emit
 def emit(run,name,status,data=None,*args,**kwargs):
  original(run,name,status,data,*args,**kwargs)
  if name in ['agent.phase.started','agent.delivery.canonical','run.failed','execution.completed','plan.preflight.completed','review.completed']:
   row={'time':time.time(),'run':run.id,'event':name,'status':status.value,'decision':((data or {}).get('review') or {}).get('decision')}
   with events.open('a') as f:f.write(json.dumps(row)+'\n')
   print(json.dumps(row),flush=True)
 w.emit=emit
 cases=[('top','只看records中entity_id不为空的记录，按record_id降序取前3条，只输出record_id和entity_id，不汇总，不限制时间。',3),
        ('top','把刚才的数量改为前2条，其余条件、排序和输出字段不变。',2),
        ('cross','先从lookup取entity_id不为空的前2条，按entity_id升序，再通过已发布records_lookup关系到records查对应的record_id和entity_id明细。只输出records这两个字段，不补lookup其他字段，不汇总，最终明细不要求排序。',2),
        ('top','重新核验：只看records中entity_id不为空的记录，按record_id降序取前3条，只输出record_id和entity_id，不汇总，不限时间。',3)]
 reports=[]
 if followup:cases=[('top','把刚才的数量改为前2条，其余条件、排序和输出字段不变。',2)]
 for index,(kind,question,n) in enumerate(cases):
  if index in (2,3):session=w.store.create_session('[验收]真实服务-'+str(index+1),'cross_source_demo',auth.subject)
  if index==1 and not reports[0]['result_correct']:
   reports.append({'case':2,'result_correct':False,'blocked':'first query did not complete correctly'});continue
  start_requests=len(traces)
  before,fields=await expected(kind,n)
  started=time.monotonic();run=await w.start(session.id,question,domain='cross_source_demo',authorization=auth)
  after,_=await expected(kind,n)
  stable=project(before,fields)==project(after,fields)
  actual=project(run.result,fields) if run.result else None;reference=project(after,fields)
  if kind=='cross':
   actual=sorted(actual or [],key=lambda row:json.dumps(row,default=str));reference=sorted(reference,key=lambda row:json.dumps(row,default=str))
  row={'case':index+1,'kind':kind,'run_id':run.id,'session_id':session.id,'status':run.status.value,'duration_ms':round((time.monotonic()-started)*1000),'model_calls':len(traces)-start_requests,'agent_model_calls':sum(e.event=='agent.model.started' for e in run.events),'stable_reference':stable,'reference_rows':after.row_count,'actual_rows':run.result.row_count if run.result else None,'result_hash':digest(actual),'reference_hash':digest(reference),'result_correct':stable and run.status.value=='completed' and actual==reference,'sql_hash':run.sql.artifact_hash if run.sql else None,'source_steps':len(run.plan.source_steps) if run.plan else None,'request_metrics':traces[start_requests:],'error':run.error}
  if kind=='top':row['limit_correct']=bool(run.plan and run.plan.limit==n);row['order_preserved']=bool(run.plan and run.plan.order_by and run.plan.order_by[0].field=='records.record_id' and run.plan.order_by[0].direction=='desc')
  reports.append(row);(OUT/'runs.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(row,ensure_ascii=True),flush=True)
  if run.status.value in ['clarifying','awaiting_approval']:
   print(json.dumps({'event':'needs_user_input','case':index+1}),flush=True);break
 print(json.dumps({'event':'finished','all_correct':len(reports)==len(cases) and all(x['result_correct'] for x in reports)}),flush=True)
asyncio.run(main())
