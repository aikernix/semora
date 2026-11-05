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
