import asyncio,json,os,sys,argparse,hashlib
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(root/p) for p in ['services/api','services/agent','packages/contracts/python','packages/query-core/python','packages/telemetry/python','packages/s3-security/python']]+[str(root)]
os.environ.pop('METADATA_DATABASE_URL',None)
os.environ['AGENT_QUERY_INTENT']='true';os.environ['AGENT_BOUNDED_CONTEXT']='true';os.environ['AGENT_TASK_MEMORY']='true'
import pytest
from semora_agent import AnalystAgent,ReviewerAgent
from semora_agent.prompts import VERSION
from services.api.tests.test_agent_runtime import NativeModel
from services.api.tests.test_source_execution import harness
class Capture(NativeModel):
 def __init__(self,calls):super().__init__(calls);self.requests=[]
 async def complete_messages(self,messages,tools,**kw):
  self.requests.append({'messages':messages,'tools':tools})
  return await super().complete_messages(messages,tools,**kw)
async def main():
 patch=pytest.MonkeyPatch();w,session,calls=harness(patch)
 a=Capture([('validate_intent',{'intent':{'summary':'查询对象的关联数值','subject':'client','fields':['balances.amount']}})])
 r=Capture([('AgentSubmission',{'payload':{'decision':'approved','rationale':'需求与实际产物一致'}})]*3)
 w.analyst=AnalystAgent(a);w.reviewer=ReviewerAgent(r)
 run=await w.start(session.id,'查询对象的关联数值',domain='commerce')
 assert run.status.value=='completed',run.error
 requests=[{'phase':phase,**p} for phase,p in zip(['analyst','plan_review','execution_review','result_review'],a.requests+r.requests)]
 stats=[]
 for p in requests:
  system='\n'.join(m['content'] for m in p['messages'] if m['role']=='system')
  stats.append({'phase':p['phase'],'system_chars':len(system),'system_sha256':hashlib.sha256(system.encode()).hexdigest(),'request_bytes':len(json.dumps(p,ensure_ascii=False,default=str).encode())})
 parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
 path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
 path.write_text(json.dumps({'prompt_version':VERSION,'fixture':'synthetic local SQLite; no production data','stats':stats,'requests':requests,'result_correct':run.result.rows==[[10],[20],[999]]},ensure_ascii=False,default=str,indent=2)+'\n',encoding='utf8')
 print(json.dumps(stats));patch.undo()
if __name__=='__main__':asyncio.run(main())
