import pytest

from app.planner import PlanningError, compile_plan
from semora_contracts import QueryFilter, QueryPlan


ONTOLOGY = {
    "dataSourceId": "retail-credit-pg",
    "scopeField": "branch.id",
    "timeField": "loan.as_of_date",
    "objects": [
        {"name": "branch", "table": "branches", "primaryKey": "id", "properties": [{"name": "name", "column": "name"}]},
        {"name": "loan", "table": "loans", "primaryKey": "id", "properties": [{"name": "status", "column": "status"}, {"name": "as_of_date", "column": "as_of_date"}, {"name": "outstanding_balance", "column": "outstanding_balance"}]},
        {"name": "payment", "table": "payments", "primaryKey": "id", "properties": [{"name": "days_overdue", "column": "days_overdue"}, {"name": "overdue_amount", "column": "overdue_amount"}]},
    ],
    "relations": [
        {"name": "loan_belongs_to_branch", "from": "loan", "to": "branch", "condition": "loan.branch_id = branch.id"},
        {"name": "loan_has_payment", "from": "loan", "to": "payment", "condition": "loan.id = payment.loan_id", "optional": True},
    ],
    "metrics": [
        {"name": "loan_balance", "expression": "SUM(loan.outstanding_balance)", "format": "currency"},
        {"name": "overdue_90_rate", "expression": "SUM(payment.overdue_amount WHERE payment.days_overdue >= 90) / SUM(loan.outstanding_balance)", "format": "percentage"},
    ],
}


def test_compiler_preserves_type_discriminator_literals_in_relationships():
    import copy
    ontology = copy.deepcopy(ONTOLOGY)
    ontology['relations'][0]['condition'] += " AND loan.status = 'jbo.acct.ACCT_LOAN'"
    plan = QueryPlan(domain='bank_analysis', data_source_id='retail-credit-pg', intent='贷款余额', metrics=['loan_balance'], dimensions=['branch.name'], relationship_path=['loan_belongs_to_branch'], grain='loan')
    artifact = compile_plan(plan, ontology)
    assert "'jbo.acct.ACCT_LOAN'" in artifact.physical_sql


def test_named_year_cannot_be_silently_ignored():
    plan=QueryPlan(domain='bank',data_source_id='retail-credit-pg',intent='年度贷款余额',metrics=['loan_balance'],grain='loan',time_range='2026年')
    with pytest.raises(PlanningError,match='禁止忽略时间范围'):
        compile_plan(plan,ONTOLOGY)


def test_compiler_uses_ontology_mappings_and_bound_parameters():
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

    artifact = compile_plan(plan, ONTOLOGY)

    assert "FROM branches branch" in artifact.physical_sql
    assert "JOIN loans loan" in artifact.physical_sql
    assert "FROM payments payment" in artifact.physical_sql
    assert "LEFT JOIN __semora_" in artifact.physical_sql
    assert "%(filter_0)s" in artifact.physical_sql
    assert {k: v for k, v in artifact.parameters.items() if not k.startswith('s')} == {
        "filter_0": "active",
        "branch_id_0": 1,
        "branch_id_1": 2,
        "branch_id_2": 3,
    }
    assert 90 in artifact.parameters.values()
    assert "active" not in artifact.physical_sql


def test_compiler_carries_ontology_sensitive_columns_to_executor_contract():
    ontology = {
        **ONTOLOGY,
        "objects": ONTOLOGY["objects"]
        + [
            {
                "name": "customer",
                "table": "customers",
                "primaryKey": "id",
                "properties": [{"name": "mobile", "column": "mobile", "sensitive": True}],
            }
        ],
        "relations": ONTOLOGY["relations"]
        + [
            {
                "name": "customer_has_loan",
                "from": "customer",
                "to": "loan",
                "condition": "customer.id = loan.customer_id",
            }
        ],
    }
    plan = QueryPlan(
        domain="retail_credit",
        data_source_id="retail-credit-pg",
        intent="敏感明细",
        metrics=[],
        fields=["customer.mobile"],
        filters=[QueryFilter(field="loan.status", operator="eq", value="active")],
        relationship_path=["customer_has_loan"],
    )

    artifact = compile_plan(plan, ontology)

    assert artifact.sensitive_columns == ["customer_mobile"]


def test_compiler_rejects_unpublished_metric():
    plan = QueryPlan(domain="retail_credit", data_source_id="retail-credit-pg", intent="bad", metrics=["unknown"])

    with pytest.raises(PlanningError):
        compile_plan(plan, ONTOLOGY)


def test_compiler_uses_generic_scope_and_time_metadata_without_bank_object_names():
    ontology = {
        "dataSourceId": "deposit-pg",
        "scopeField": "office.id",
        "timeField": "account.snapshot_date",
        "objects": [
            {"name": "office", "table": "offices", "primaryKey": "office_id", "properties": [{"name": "title", "column": "title", "type": "string"}]},
            {"name": "account", "table": "accounts", "primaryKey": "account_id", "properties": [{"name": "balance", "column": "balance", "type": "decimal"}, {"name": "snapshot_date", "column": "snapshot_date", "type": "date"}]},
        ],
        "relations": [{"name": "account_at_office", "from": "account", "to": "office", "condition": "account.office_id = office.id"}],
        "metrics": [{"name": "account_balance", "expression": "SUM(account.balance)", "grain": "office"}],
    }
    plan = QueryPlan(
        domain="deposit",
        data_source_id="deposit-pg",
        intent="按网点统计余额",
        metrics=["account_balance"],
        dimensions=["office.title"],
        relationship_path=["account_at_office"],
        data_scope={"branch_ids": [7]},
        time_range="最近6个月",
        grain="office",
    )
    artifact = compile_plan(plan, ontology)
    assert "FROM offices office" in artifact.physical_sql
    assert "office.office_id IN (%(branch_id_0)s)" in artifact.physical_sql
    assert "account.snapshot_date >= CURRENT_DATE" in artifact.physical_sql
