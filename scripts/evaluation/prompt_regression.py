"""Opt-in real-model regression; SQL executes only against synthetic local SQLite."""
import asyncio,sys,os,json,time,sqlite3,re,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[2];os.chdir(root)
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--case',choices=['existence','regroup'],required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
sys.path[:0]=[str(root/p) for p in ['packages/query-core/python','services/api','services/agent','packages/contracts/python','packages/telemetry/python','packages/s3-security/python']]+[str(root)]
os.environ.pop('METADATA_DATABASE_URL',None)
os.environ['AGENT_QUERY_INTENT']='true';os.environ['AGENT_BOUNDED_CONTEXT']='true'
import pytest,sqlglot
from semora_agent import AnalystAgent,ReviewerAgent
from semora_contracts import ExecutionResult,OntologySnapshotRef,SQLArtifact
from services.api.tests.test_source_execution import harness
from services.api.tests.test_result_regroup import fixture
class Provider:
 configured=required=True
 def __init__(self):self.traces=[];self.requests=[]
 async def complete_messages(self,messages,tools,**kwargs):
  p=await asyncio.create_subprocess_exec('docker','exec','-i','semora-api-1','python','/tmp/model-bridge.py',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
  out,err=await p.communicate(json.dumps({'messages':messages,'tools':tools,'tool_choice':kwargs.get('tool_choice')}).encode())
  if p.returncode:raise RuntimeError('model bridge failure')
  value=json.loads(out);self.traces+=value['traces'];self.requests.append({'messages':messages,'response':value.get('result')})
  print(json.dumps({'model_calls':len(self.traces),'request_duration_ms':self.traces[-1].get('duration_ms') if self.traces else None,'error_type':value.get('error_type')}),flush=True)
  if value.get('error_type'):raise RuntimeError('model request '+value['error_type'])
  return value['result']
async def main():
 for source,target in [('scripts/evaluation/prompt_model_bridge.py','/tmp/model-bridge.py'),('services/agent/semora_agent/model_diagnostics.py','/tmp/new_model_diagnostics.py')]:
  subprocess.run(['docker','cp',source,'semora-api-1:'+target],check=True,stdout=subprocess.DEVNULL)
 patch=pytest.MonkeyPatch();w,session,_=harness(patch);ont,_=fixture();p=Provider()
 w.analyst=AnalystAgent(p);w.reviewer=ReviewerAgent(p)
 async def snapshot(domain):return OntologySnapshotRef(domain='commerce',version='1',content_hash='test',artifact_version='test'),ont
 w.snapshot_loader=snapshot
 db={'customers-db':sqlite3.connect(':memory:'),'accounts-db':sqlite3.connect(':memory:')}
 db['customers-db'].executescript('CREATE TABLE entity(id INTEGER); INSERT INTO entity VALUES(1),(2),(3); CREATE TABLE record(id INTEGER,entity_id INTEGER,ended_at INTEGER); INSERT INTO record VALUES(11,1,20),(12,1,2),(21,2,18),(31,3,1);')
 facts='(1,11,4),(2,11,4),(3,12,8),(4,21,10),(5,31,999)' if args.case=='regroup' else '(1,11,4),(2,11,5),(5,31,999)'
 db['accounts-db'].executescript('CREATE TABLE fact(id INTEGER,record_id INTEGER,amount NUMERIC); INSERT INTO fact VALUES'+facts+';')
 from app.datasources import datasource_service
 for source,database in db.items():
  tables=[]
  for (name,) in database.execute("SELECT name FROM sqlite_master WHERE type='table'"):
   tables.append({'name':name,'columns':[{'name':c[1],'type':c[2],'nullable':not bool(c[3]),'primary_key':bool(c[5])} for c in database.execute('PRAGMA table_info("'+name+'")')]})
  record=datasource_service.repository.get(source);record.scan_result={'tables':tables};datasource_service.repository.save(record)
 async def compile_wren(plan,ontology):
  code="import json,sys;from semora_contracts import QueryPlan;from app.wren_adapter import compile_with_wren;p=json.load(sys.stdin);print(compile_with_wren(QueryPlan.model_validate(p['plan']),p['ontology']).model_dump_json())"
  process=await asyncio.create_subprocess_exec('docker','exec','-i','semora-semantic-planner-1','python','-c',code,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
  out,err=await process.communicate(json.dumps({'plan':plan.model_dump(mode='json'),'ontology':ontology}).encode())
  if process.returncode:raise RuntimeError('Wren compile failed: '+err.decode()[-1000:])
  return SQLArtifact.model_validate_json(out)
 w._compile=compile_wren
 calls=[]
 async def execute(sql,parameters,sensitive,settings):
  calls.append({'source':settings['data_source_id'],'sql':sql})
  sql=re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s',r':\1',sql)
  tree=sqlglot.parse_one(sql,read='postgres')
  for table in tree.find_all(sqlglot.exp.Table):table.set('db',None);table.set('catalog',None)
  cur=db[settings['data_source_id']].execute(tree.sql(dialect='sqlite'),parameters)
  rows=[list(r) for r in cur.fetchall()]
  return ExecutionResult(columns=[c[0] for c in cur.description],rows=rows,row_count=len(rows),duration_ms=1,summary={'masked_columns':[]})
 w.execute=execute
 request='Select the last 2 different entity objects using each entity latest record.ended_at descending (higher values mean later). Then sum fact.amount across ALL records belonging to those entities, group only by entity.id, sort by total descending and return only the highest total entity. Output entity.id and total. No extra time or status filters. entity_records and record_facts are the published relationship roles. Top entities and final top result are separate selections.'
 request+=' For selection ONLY require record.ended_at > 10. After selecting the entities, include ALL associated records, including those with ended_at <= 10. Keep the selection condition distinct from the final result scope.'
 if args.case=='existence':request='选出record.ended_at最大的两个不同entity，再从这两个对象中找出没有任何关联fact的对象，只输出entity.id并按entity.id升序。不限制状态和时间窗口，ended_at是明确顺序值，越大越晚。选定后检查这些对象的全部record，只要任一record关联fact就算该entity存在fact。使用已发布的entity_records与record_facts关系。'
 expected=[[2]] if args.case=='existence' else [[1,16]]
 start=time.monotonic();run=await w.start(session.id,request,domain='commerce')
 report={'status':run.status.value,'error':run.error,'duration_ms':round((time.monotonic()-start)*1000),'model_requests':len(p.traces),'database_queries':len(calls),'compiler':'deployed real Wren Core','rows':run.result.rows if run.result else None,'result_correct':bool(run.result and run.result.rows==expected),'sql':calls,'review_case':run.review_case.model_dump(mode='json') if run.review_case else None,'candidate':run.agent_memory.get('last_candidate'),'review':run.review.model_dump(mode='json') if run.review else None,'result_review':run.result_review.model_dump(mode='json') if run.result_review else None,'intent':run.plan.query_intent.model_dump(mode='json') if run.plan and run.plan.query_intent else None,'audit_valid':w.store.audit.verify(run.id)==(True,None),'business_database_accessed':False,'production_metadata_written':False,'request_metrics':p.traces,'events':[e.model_dump(mode='json') for e in run.events],'transcript':p.requests}
 Path(args.output).parent.mkdir(parents=True,exist_ok=True)
 Path(args.output).write_text(json.dumps(report,ensure_ascii=False,default=str,indent=2),encoding='utf8')
 print(json.dumps({k:v for k,v in report.items() if k not in ['sql','request_metrics','intent','review','result_review','review_case','candidate','events','transcript']},default=str),flush=True)
 patch.undo()
 if not report['result_correct']:sys.exit(1)
asyncio.run(main())
