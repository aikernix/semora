"""Enforce the new dependency and protocol boundaries with executable examples."""
import ast,os,subprocess,sys
from pathlib import Path


def test_query_core_compiles_without_application_agent_or_network_clients():
    script='''
import sys,importlib.abc
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in {'app','semora_agent','httpx','psycopg','langgraph','langchain_core'}:
            raise AssertionError('QueryCore imported forbidden dependency: '+fullname)
sys.meta_path.insert(0,Guard())
from semora_query_core.intent import compile_intent
from semora_query_core.sql import compile_plan
from semora_query_core.stages import validate_query_shape
from semora_contracts import QueryPlan
ont={'domain':'generic','dataSourceId':'one','objects':[{'name':'item','table':'items','primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'}]}],'relations':[],'metrics':[]}
p=QueryPlan.model_validate(compile_intent({'summary':'list','subject':'item','fields':['item.id']},ont,{}))
validate_query_shape(p,ont)
a=compile_plan(p,ont)
assert 'SELECT' in a.physical_sql and a.data_source_id=='one'
print('isolated core compiled')
'''
    root=Path(__file__).resolve().parents[3]
    env={**os.environ,'PYTHONPATH':os.pathsep.join(str(root/p) for p in ['packages/query-core/python','packages/contracts/python'])}
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert 'isolated core compiled' in result.stdout


def test_role_implementations_cannot_import_compatibility_facade():
    import app.agent_tools,app.intent_analysis,app.review_analysis,app.legacy_plan_analysis,app.agentic
    for module in [app.agent_tools,app.intent_analysis,app.review_analysis,app.legacy_plan_analysis]:
        tree=ast.parse(Path(module.__file__).read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                assert not (node.module or '').endswith('agentic'),module.__name__
    tools=Path(app.agent_tools.__file__).read_text(encoding='utf-8-sig')
    assert 'await run_agent(' not in tools and 'await ports.runner(' not in tools
    facade=ast.parse(Path(app.agentic.__file__).read_text(encoding='utf-8-sig'))
    assert not any(isinstance(n,(ast.For,ast.While)) for n in ast.walk(facade))


def test_new_suites_use_production_default(monkeypatch):
    from app.intent_planner import enabled
    monkeypatch.delenv('AGENT_QUERY_INTENT',raising=False)
    assert enabled()
