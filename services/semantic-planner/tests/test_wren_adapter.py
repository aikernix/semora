from __future__ import annotations

import unittest
import sqlite3
import sqlglot

from semora_contracts import QueryFilter, QueryPlan

from app.wren_adapter import WrenPlanningError, build_wren_manifest, compile_with_wren


ONTOLOGY = {
    "dataSourceId": "retail-credit-pg",
    "scopeField": "branch.id",
    "timeField": "loan.as_of_date",
    "objects": [
        {"name": "branch", "table": "branches", "primaryKey": "id", "properties": [{"name": "name", "column": "name", "type": "string"}]},
        {"name": "loan", "table": "loans", "primaryKey": "id", "properties": [{"name": "status", "column": "status", "type": "string"}, {"name": "as_of_date", "column": "as_of_date", "type": "date"}, {"name": "outstanding_balance", "column": "outstanding_balance", "type": "decimal"}]},
        {"name": "payment", "table": "payments", "primaryKey": "id", "properties": [{"name": "days_overdue", "column": "days_overdue", "type": "integer"}, {"name": "overdue_amount", "column": "overdue_amount", "type": "decimal"}]},
    ],
    "relations": [
        {"name": "loan_belongs_to_branch", "from": "loan", "to": "branch", "condition": "loan.branch_id = branch.id", "cardinality": "N:1"},
        {"name": "loan_has_payment", "from": "loan", "to": "payment", "condition": "loan.id = payment.loan_id", "cardinality": "1:N", "optional": True},
    ],
    "metrics": [
        {"name": "loan_balance", "expression": "SUM(loan.outstanding_balance)", "format": "currency"},
        {"name": "overdue_90_rate", "expression": "SUM(payment.overdue_amount WHERE payment.days_overdue >= 90) / SUM(loan.outstanding_balance)", "format": "percentage"},
    ],
}


class WrenAdapterTest(unittest.TestCase):
    def test_having_and_conditional_aggregate_parameters_survive_wren_and_mysql(self):
        import re
        from planner import adapt_mysql_artifact
        ontology={'dataSourceId':'one','objects':[{'name':'record','table':'records','primaryKey':'id',
            'properties':[{'name':name,'column':name,'type':kind} for name,kind in
                          [('id','integer'),('category','string'),('amount','number')]]}],
            'relations':[],'metrics':[]}
        plan=QueryPlan(domain='generic',data_source_id='one',intent='aggregate',metrics=['total'],
            dimensions=['record.category'],calculations=[{'name':'total','function':'sum','field':'record.amount',
                'filters':[{'field':'record.id','operator':'gt','value':0}]}],
            having=[{'field':'total','operator':'between','value':[1,10]}])
        artifact=compile_with_wren(plan,ontology)
        for output in (artifact,adapt_mysql_artifact(artifact)):
            self.assertEqual(set(re.findall(r'%\((\w+)\)s',output.physical_sql)),set(output.parameters))
            self.assertNotIn(':having_',output.physical_sql)
            self.assertNotIn(':m_',output.physical_sql)
            sql=re.sub(r'%\((\w+)\)s',r':\1',output.physical_sql)
            tree=sqlglot.parse_one(sql,read=output.dialect)
            for table in tree.find_all(sqlglot.exp.Table):
                if table.db=='public':table.set('db',None)
            sql=tree.sql(dialect='sqlite')
            with sqlite3.connect(':memory:') as db:
                db.executescript("CREATE TABLE records(id INTEGER,category TEXT,amount INTEGER); INSERT INTO records VALUES(1,'a',2),(2,'a',3),(3,'b',50),(0,'a',100);")
                self.assertEqual(db.execute(sql,output.parameters).fetchall(),[('a',5)])

    def test_top_n_sort_survives_wren_and_mysql_translation(self):
        from planner import adapt_mysql_artifact
        ontology = {'dataSourceId':'one', 'objects':[{'name':'record','table':'records','primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'},{'name':'created_at','column':'created_at','type':'timestamp'}]}], 'relations':[], 'metrics':[]}
        plan = QueryPlan(domain='generic',data_source_id='one',intent='latest records',metrics=[],fields=['record.id','record.created_at'],order_by=[{'field':'record.created_at','direction':'desc'},{'field':'record.id','direction':'desc'}],limit=10)
        artifact = compile_with_wren(plan, ontology)
        for sql, dialect in [(artifact.physical_sql, 'postgres'), (adapt_mysql_artifact(artifact).physical_sql, 'mysql')]:
            tree = sqlglot.parse_one(sql, read=dialect)
            self.assertEqual(tree.args['limit'].expression.this, '10')
            ordered = [item for item in tree.args['order'].expressions if isinstance(item.this, sqlglot.exp.Column)]
            self.assertEqual(len(ordered), 2)
            self.assertTrue(ordered[0].args['desc'])


    def test_unused_model_with_table_name_prefix_does_not_block_compilation(self):
        ontology={'dataSourceId':'one','objects':[{'name':name,'table':'content.'+table,'primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'}]} for name,table in [('audio','appoint_audio'),('text','appoint_audio_text')]],'relations':[],'metrics':[]}
        artifact=compile_with_wren(QueryPlan(domain='test',data_source_id='one',intent='text ids',metrics=[],fields=['text.id']),ontology)
        self.assertIn('appoint_audio_text',artifact.physical_sql)

    def test_distinct_refsql_and_composite_binding_survive_wren(self):
        from semora_contracts import BoundKeyFilter
        ontology = {'dataSourceId':'one', 'objects':[{'name':'student','table':'appointments','primaryKey':'s_id','projection':{'distinct':True},'properties':[{'name':'s_id','column':'s_id','type':'integer'},{'name':'kind','column':'kind','type':'string'}]}], 'relations':[], 'metrics':[]}
        manifest = build_wren_manifest(ontology)
        self.assertIn('SELECT DISTINCT', manifest['models'][0]['refSql'])
        self.assertNotIn('tableReference', manifest['models'][0])
        plan = QueryPlan(domain='example',data_source_id='one',intent='keys',metrics=[],fields=['student.s_id','student.kind'],bound_key_filters=[BoundKeyFilter(fields=['student.s_id','student.kind'],rows=[[1,'A'],[2,'B']])])
        artifact = compile_with_wren(plan,ontology)
        import re
        sql = re.sub(r'%\((\w+)\)s',r':\1',artifact.physical_sql)
        tree = sqlglot.parse_one(sql,read='postgres')
        for table in tree.find_all(sqlglot.exp.Table):
            table.set('db',None); table.set('catalog',None)
        with sqlite3.connect(':memory:') as db:
            db.executescript("CREATE TABLE appointments(s_id integer,kind text); INSERT INTO appointments VALUES(1,'A'),(1,'A'),(1,'B'),(2,'A'),(2,'B');")
            self.assertEqual(sorted(db.execute(tree.sql(dialect='sqlite'),artifact.parameters).fetchall()),[(1,'A'),(2,'B')])

    def test_staged_query_preserves_parent_totals_and_ratio_after_wren(self):
        plan = QueryPlan(domain='example', data_source_id='retail-credit-pg', intent='验证多阶段汇总',
                         metrics=['loan_balance', 'overdue_90_rate'], dimensions=['branch.name'], grain='branch',
                         relationship_path=['loan_belongs_to_branch', 'loan_has_payment'],
                         stages=[{'name': 'payment_by_loan', 'source': 'payment', 'group_by': ['payment.loan_id'], 'measures': [{'name': 'overdue_total', 'function': 'sum', 'field': 'payment.overdue_amount', 'filters': [{'field': 'payment.days_overdue', 'operator': 'gte', 'value': 90}]}]}])
        artifact = compile_with_wren(plan, ONTOLOGY)
        import re
        sql = re.sub(r'%\((\w+)\)s', r':\1', artifact.physical_sql)
        tree = sqlglot.parse_one(sql, read='postgres')
        for table in tree.find_all(sqlglot.exp.Table):
            table.set('db', None)
            table.set('catalog', None)
        with sqlite3.connect(':memory:') as db:
            db.executescript('''CREATE TABLE branches(id INTEGER PRIMARY KEY,name TEXT);
              CREATE TABLE loans(id INTEGER PRIMARY KEY,branch_id INTEGER,status TEXT,as_of_date TEXT,outstanding_balance NUMERIC);
              CREATE TABLE payments(id INTEGER PRIMARY KEY,loan_id INTEGER,days_overdue INTEGER,overdue_amount NUMERIC);
              INSERT INTO branches VALUES(1,'A');
              INSERT INTO loans VALUES(1,1,'active','2026-01-01',100),(2,1,'active','2026-01-01',100);
              INSERT INTO payments VALUES(1,1,90,10),(2,1,90,20),(3,1,0,0),(4,2,90,30);''')
            rows = db.execute(tree.sql(dialect='sqlite'), artifact.parameters).fetchall()
        self.assertEqual(rows, [('A', 200, 30)])

    def test_manifest_has_physical_models_and_relation_columns(self) -> None:
        manifest = build_wren_manifest(ONTOLOGY)
        loan = next(item for item in manifest["models"] if item["name"] == "loan")
        self.assertEqual(loan["tableReference"], {"schema": "public", "table": "loans"})
        self.assertIn("branch_id", {item["name"] for item in loan["columns"]})

    def test_wren_expands_models_and_preserves_bound_parameters(self) -> None:
        plan = QueryPlan(
            domain="retail_credit",
            data_source_id="retail-credit-pg",
            intent="按分行分析贷款",
            metrics=["loan_balance", "overdue_90_rate"],
            dimensions=["branch.name"],
            filters=[QueryFilter(field="loan.status", operator="eq", value="active")],
            time_range="最近12个月",
            grain="branch",
            relationship_path=["loan_belongs_to_branch", "loan_has_payment"],
            stages=[{'name': 'payment_by_loan', 'source': 'payment', 'group_by': ['payment.loan_id'], 'measures': [{'name': 'overdue_total', 'function': 'sum', 'field': 'payment.overdue_amount', 'filters': [{'field': 'payment.days_overdue', 'operator': 'gte', 'value': 90}]}]}],
            data_scope={"branch_ids": [1, 2, 3]},
        )
        artifact = compile_with_wren(plan, ONTOLOGY)
        self.assertIn('FROM "public".branches', artifact.physical_sql)
        self.assertIn('FROM "public".loans', artifact.physical_sql)
        self.assertIn("%(filter_0)s", artifact.physical_sql)
        self.assertIn("%(branch_id_2)s", artifact.physical_sql)
        self.assertIn("CURRENT_DATE - INTERVAL '12 MONTH'", artifact.physical_sql)
        self.assertNotIn("current_date()", artifact.physical_sql)
        self.assertNotIn("FROM branches branch", artifact.physical_sql)

    def test_unsafe_physical_identifier_is_rejected(self) -> None:
        ontology = {**ONTOLOGY, "objects": [{**ONTOLOGY["objects"][0], "table": "branches;drop"}] + ONTOLOGY["objects"][1:]}
        with self.assertRaises(WrenPlanningError):
            build_wren_manifest(ontology)

    def test_null_filter_is_preserved_by_wren(self) -> None:
        plan = QueryPlan(domain='retail_credit', data_source_id='retail-credit-pg', intent='未设置状态',
                         metrics=['loan_balance'], grain='loan',
                         filters=[QueryFilter(field='loan.status', operator='is_null')])
        artifact = compile_with_wren(plan, ONTOLOGY)
        self.assertIn('IS NULL', artifact.physical_sql.upper())
        self.assertNotIn('filter_0', artifact.parameters)

    def test_published_manifest_is_used_as_immutable_input(self) -> None:
        manifest = build_wren_manifest(ONTOLOGY)
        manifest["models"][0]["tableReference"]["table"] = "published_branches"
        ontology = {**ONTOLOGY, "_wrenArtifact": manifest}
        plan = QueryPlan(
            domain="retail_credit",
            data_source_id="retail-credit-pg",
            intent="按分行分析贷款",
            metrics=["loan_balance"],
            dimensions=["branch.name"],
            filters=[QueryFilter(field="loan.status", operator="eq", value="active")],
            grain="branch",
            relationship_path=["loan_belongs_to_branch"],
            data_scope={"branch_ids": [1]},
        )
        artifact = compile_with_wren(plan, ontology)
        self.assertIn('"public".published_branches', artifact.physical_sql)


if __name__ == "__main__":
    unittest.main()


def test_wren_raw_partials_preserve_precision_before_regroup():
    ont={'domain':'generic','dataSourceId':'one','objects':[{
        'name':'fact','table':'facts','primaryKey':'id','properties':[
            {'name':'id','column':'id','type':'integer'},
            {'name':'owner_id','column':'owner_id','type':'integer'},
            {'name':'amount','column':'amount','type':'decimal'}]}],'relations':[],'metrics':[]}
    plan=QueryPlan(domain='generic',data_source_id='one',intent='partials',grain='fact',
        dimensions=['fact.owner_id'],metrics=['total'],aggregate_precision='raw',
        calculations=[{'name':'total','function':'sum','field':'fact.amount'}])
    artifact=compile_with_wren(plan,ont)
    assert 'ROUND' not in artifact.physical_sql.upper()
    tree=sqlglot.parse_one(artifact.physical_sql,read='postgres')
    for table in tree.find_all(sqlglot.exp.Table):
        table.set('db',None);table.set('catalog',None)
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE facts(id INTEGER,owner_id INTEGER,amount NUMERIC); INSERT INTO facts VALUES(1,1,0.004),(2,2,0.004);')
        assert sorted(db.execute(tree.sql(dialect='sqlite')).fetchall())==[(1,0.004),(2,0.004)]


def test_wren_membership_keys_are_unique_without_aggregate_metrics():
    ont={'domain':'generic','dataSourceId':'one','objects':[{
        'name':'fact','table':'facts','primaryKey':'id','properties':[
            {'name':'id','column':'id','type':'integer'},
            {'name':'owner_id','column':'owner_id','type':'integer'}]}],'relations':[],'metrics':[]}
    plan=QueryPlan(domain='generic',data_source_id='one',intent='membership keys',grain='fact',
        fields=['fact.owner_id'],dimensions=['fact.owner_id'],metrics=[])
    artifact=compile_with_wren(plan,ont)
    tree=sqlglot.parse_one(artifact.physical_sql,read='postgres')
    for table in tree.find_all(sqlglot.exp.Table):table.set('db',None);table.set('catalog',None)
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE facts(id INTEGER,owner_id INTEGER); INSERT INTO facts VALUES(1,11),(2,11);')
        assert db.execute(tree.sql(dialect='sqlite')).fetchall()==[(11,)]


def test_wren_stage_count_preserves_empty_and_matching_semantics():
    """Real Wren compilation followed by execution of the physical SQL in SQLite."""
    from copy import deepcopy
    ont={'dataSourceId':'one','objects':[
        {'name':'parent','table':'parent','primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'},{'name':'amount','column':'amount','type':'decimal'}]},
        {'name':'line','table':'line','primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'},{'name':'parent_id','column':'parent_id','type':'integer'},{'name':'quantity','column':'quantity','type':'integer'}]}],
        'relations':[{'name':'items','from':'parent','to':'line','condition':'parent.id = line.parent_id','cardinality':'1:N','optional':True}],'metrics':[]}
    for optional,grouped,empty,nullable in [(True,True,False,False),(False,False,False,False),(True,False,True,False),(True,True,False,True)]:
        ontology=deepcopy(ont);ontology['relations'][0]['optional']=optional
        field='line.quantity' if nullable else 'line.id'
        plan=QueryPlan(domain='generic',data_source_id='one',intent='aggregate',grain='parent',metrics=['total','detail'],
            dimensions=['parent.id'] if grouped else [],relationship_path=['items'],
            calculations=[{'name':'total','function':'sum','field':'parent.amount'},{'name':'detail','function':'count','field':field}],
            stages=[{'name':'reduce_line','source':'line','group_by':['line.parent_id'],
                     'measures':[{'name':'cnt','function':'count','field':field}],'join_type':'left' if optional else 'inner'}])
        artifact=compile_with_wren(plan,ontology)
        tree=sqlglot.parse_one(artifact.physical_sql,read='postgres')
        for table in tree.find_all(sqlglot.exp.Table):
            table.set('db',None);table.set('catalog',None)
        with sqlite3.connect(':memory:') as db:
            db.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY,amount NUMERIC); CREATE TABLE line(id INTEGER PRIMARY KEY,parent_id INTEGER,quantity NUMERIC);')
            if not empty:
                db.executescript('INSERT INTO parent VALUES(1,100),(2,200);')
                db.executemany('INSERT INTO line VALUES(?,?,?)',[(1,1,None if nullable else 3),(2,1,None if nullable else 4)])
            rows=sorted(db.execute(tree.sql(dialect='sqlite'),artifact.parameters).fetchall())
        expected=[(None,0)] if empty else [(1,100,0),(2,200,0)] if nullable else [(1,100,2),(2,200,0)] if grouped else [(100,2)]
        assert rows==expected,(optional,grouped,empty,nullable,rows,artifact.physical_sql)
