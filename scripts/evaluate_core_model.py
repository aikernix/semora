"""Opt-in local core acceptance: real configured provider, fixed SQLite fixtures.
No live business rows are sent; no production metadata is written.
"""
import asyncio,json,os,sys,time,argparse,inspect,subprocess,hashlib
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ['packages/query-core/python','services/api','services/agent','packages/contracts/python','packages/telemetry/python','packages/s3-security/python']]
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.pop('METADATA_DATABASE_URL',None)
parser=argparse.ArgumentParser();parser.add_argument('--review-only',action='store_true');parser.add_argument('--protocol',choices=['baseline','staged'],default='staged');args=parser.parse_args()
os.environ['AGENT_STAGED_PROTOCOL']='true' if args.protocol=='staged' else 'false'
os.environ['AGENT_TASK_MEMORY']='true'
os.environ['AGENT_TIMEOUT_SECONDS']='240'
import pytest
from semora_agent import AnalystAgent,ReviewerAgent
from services.api.tests.test_source_execution import harness

class Provider:
 configured=required=True
 def __init__(self): self.traces=[]
 async def complete_messages(self,messages,tools,**kwargs):
  packet={'messages':messages,'tools':tools,'tool_choice':kwargs.get('tool_choice')}
  Path('artifacts/core-acceptance').mkdir(parents=True,exist_ok=True)
  packets=Path(f'artifacts/core-acceptance/packets-{args.protocol}');packets.mkdir(exist_ok=True)
  (packets/f'{len(self.traces)+1:03d}.json').write_text(json.dumps(packet,ensure_ascii=False),encoding='utf-8')
  Path(f'artifacts/core-acceptance/last-packet-{args.protocol}.json').write_text(json.dumps(packet,ensure_ascii=False),encoding='utf-8')
  p=await asyncio.create_subprocess_exec('docker','exec','-i','semora-api-1','python','/tmp/model-bridge.py',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
  out,err=await p.communicate(json.dumps({'messages':messages,'tools':tools,'tool_choice':kwargs.get('tool_choice')}).encode())
  if p.returncode: raise RuntimeError('configured model bridge failed; provider request unsuccessful')
  value=json.loads(out);self.traces+=value['traces']
  print(json.dumps({'model_call':len(self.traces),'tools':[t['function']['name'] for t in tools],'request':value['traces'][-1] if value['traces'] else None,'error_type':value.get('error_type')}),flush=True)
  if value.get('error_type'): raise RuntimeError('model provider '+value['error_type'])
  return value['result']

async def main():
 root=Path(__file__).resolve().parents[1]
 for source,target in [(root/'scripts/evaluation/model_bridge.py','/tmp/model-bridge.py'),(root/'services/agent/semora_agent/model_diagnostics.py','/tmp/new_model_diagnostics.py')]:
  subprocess.run(['docker','cp',str(source),'semora-api-1:'+target],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 provider=Provider();patch=pytest.MonkeyPatch()
 trace_path=Path(f'artifacts/core-acceptance/events-{args.protocol}.jsonl');trace_path.parent.mkdir(parents=True,exist_ok=True);trace_path.write_text('',encoding='utf-8')
 w,session,calls=harness(patch)
 # Read real fixture metadata using SQLite, then register the same table/column
 # envelope consumed by production read_schema. Do not invent schema fields.
 from app.datasources import datasource_service
 databases=inspect.getclosurevars(w.execute).nonlocals['databases']
 scans={}
 for source_id,db in databases.items():
  tables=[]
  for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
   rows=db.execute('PRAGMA table_info("'+name.replace('"','""')+'")').fetchall()
   tables.append({'name':name,'columns':[{'name':r[1],'type':r[2],'nullable':not bool(r[3]),'primary_key':bool(r[5])} for r in rows]})
  source=datasource_service.repository.get(source_id);source.scan_result={'tables':tables};datasource_service.repository.save(source)
  scans[source_id]=tables

 w.analyst=AnalystAgent(provider);w.reviewer=ReviewerAgent(provider)
 original_emit=w.emit
 def emit(run,name,status,data=None,*extra,**kw):
  event=original_emit(run,name,status,data,*extra,**kw)
  if name in {'agent.tool.started','agent.tool.completed','agent.validation.failed','plan.preflight.completed','review.completed','agent.phase.started','run.failed'}:
   row={'at':time.time(),'event':name,'data':{k:(data or {}).get(k) for k in ['tool','lookup','analysis_goal','summary','observation','review','phase'] if k in (data or {})}}
   with trace_path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
  return event
 w.emit=emit
 request='查询状态为active的client对象的customer_id，再通过已配置关系查询这些客户的balances.amount和balances.customer_id。只要账户明细，不汇总、不排序、不补充客户其他字段；有效状态已明确为active，不限时间。'
 started=time.monotonic()
 if args.review_only:
  from app.agentic import review_plan
  from semora_contracts import QueryRun
  from services.api.tests.test_source_execution import ontology,proposal
  run=QueryRun(session_id=session.id,domain='commerce',question=request)
  reference=proposal(enrich=False)
  reference['source_steps'][0]['query']['fields']=['client.customer_id']
  assert reference['source_steps'][0]['query']['fields']==['client.customer_id']
  outcome=AnalystAgent()._build_outcome(reference,request,[],ontology(),{},'fixed-reference')
  review=await review_plan(w,run,ontology(),request,outcome)
  report={'acceptance_kind':'focused real Reviewer on known correct reference plan, not full NL-to-result','decision':review.decision,'issues':review.issues,'rationale':review.rationale,'duration_ms':round((time.monotonic()-started)*1000),'requests':provider.traces}
  Path('artifacts/core-acceptance/focused-review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps(report,ensure_ascii=True));patch.undo();return
 run=await w.start(session.id,request,domain='commerce')
 report={'fixture':'fixed independent SQLite people/accounts tables from test_source_execution.harness','schema_metadata':'actual PRAGMA table_info registered in scan_result','fixture_scan':scans,'real_provider':True,'parser_sha256':hashlib.sha256((root/'services/agent/semora_agent/model_diagnostics.py').read_bytes()).hexdigest(),'staged_protocol':args.protocol=='staged','task_memory':True,'duration_ms':round((time.monotonic()-started)*1000),'status':run.status.value,'error':run.error,'database_calls':len(calls),'executed_sql':[c[1] for c in calls],'parameter_names':[sorted(c[2]) for c in calls],'request_metrics':provider.traces,'rows':run.result.rows if run.result else None,'columns':run.result.columns if run.result else None,'events':[{'event':e.event,'summary':e.data.get('summary'),'review':e.data.get('review')} for e in run.events if e.event in ['agent.tool.completed','agent.validation.failed','plan.preflight.completed']]}
 if run.result:
  cols=run.result.columns;expected=sorted([['A',10],['A',20]])
  actual=sorted([[row[cols.index('balances_customer_id')],row[cols.index('balances_amount')]] for row in run.result.rows])
  report['expected']=expected;report['passed']=actual==expected and len(calls)==2
 else:report['passed']=False
 report['result_correct']=report.pop('passed')
 report['efficiency_target']={'max_duration_ms':120000,'max_model_calls':12,'status':'provisional engineering target, not a user SLA'}
 report['efficiency_passed']=report['result_correct'] and report['duration_ms']<=120000 and len(provider.traces)<=12
 Path('artifacts/core-acceptance').mkdir(parents=True,exist_ok=True)
 Path(f'artifacts/core-acceptance/real-model-{args.protocol}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(report,ensure_ascii=True))
 patch.undo()
 if not report['result_correct'] or not report['efficiency_passed']:sys.exit(1)
asyncio.run(main())
