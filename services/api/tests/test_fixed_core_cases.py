import json
import re
import sqlite3
from pathlib import Path
import pytest
import sqlglot
from semora_contracts import QueryPlan
from app.planner import compile_plan
from services.api.tests.test_plan_candidates import ONTOLOGY
FIXTURE=json.loads((Path(__file__).parent/'fixtures/core_query_cases.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('case',FIXTURE['cases'],ids=lambda c:c['id'])
def test_fixed_question_has_expected_database_result(case):
    plan=QueryPlan(domain='generic',data_source_id='source-a',intent=case['question'],metrics=[],grain='record').model_copy(update={})
    plan=QueryPlan.model_validate({**plan.model_dump(),**case['plan']})
    artifact=compile_plan(plan,ONTOLOGY)
    sql=re.sub(r'%\((\w+)\)s',r':\1',artifact.physical_sql)
    sql=sqlglot.transpile(sql,read='postgres',write='sqlite')[0]
    with sqlite3.connect(':memory:') as db:
        db.executescript(FIXTURE['schema'])
        actual=[list(r) for r in db.execute(sql,artifact.parameters).fetchall()]
    assert actual==case['expected']
