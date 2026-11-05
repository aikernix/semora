import asyncio

import pytest

from semora_agent import AnalystAgent


ONTOLOGY = {
    "domain": "retail_credit",
    "dataSourceId": "retail-credit-pg",
    "scopeField": "branch.id",
    "defaultFilters": [{"field": "loan.status", "operator": "eq", "value": "active"}],
    "objects": [
        {
            "name": "loan",
            "label": "贷款",
            "properties": [
                {"name": "status", "label": "状态"},
                {"name": "outstanding_balance", "label": "贷款余额"},
            ],
        },
        {
            "name": "branch",
            "label": "机构",
            "aliases": ["分行", "网点"],
            "properties": [{"name": "name", "label": "机构名称"}],
        },
    ],
    "relations": [
        {"name": "loan_belongs_to_branch", "from": "loan", "to": "branch"},
    ],
    "metrics": [
        {"name": "loan_balance", "label": "贷款余额", "expression": "SUM(loan.outstanding_balance)", "grain": "branch"},
    ],
}


class StubModel:
    configured = True
    required = False

    def __init__(self, response):
        self.response = response

    async def complete_json(self, _system, _user):
        return self.response


def required_analysis(value, ontology=ONTOLOGY):
    model = StubModel(value)
    model.required = True
    return asyncio.run(AnalystAgent(model).analyse('按分行看贷款情况', [], ontology, {}))


def test_null_fields_during_clarification_do_not_create_a_query_plan():
    value = dict.fromkeys([
        'metrics', 'dimensions', 'fields', 'filters', 'calculations', 'confidence',
        'aggregation_rules', 'output_fields', 'assumptions', 'analysis_steps',
        'relationship_path', 'clarification_options',
    ])
    value['clarifications'] = [{
        'id': 'metric', 'field': 'metrics', 'question': '您想比较余额还是逾期率？',
        'reason': '不同指标会改变结果', 'options': None,
    }]
    outcome = required_analysis(value)
    assert outcome.model_mode == 'llm'
    assert outcome.plan is None
    assert outcome.requirement.status == 'awaiting_clarification'
    assert outcome.requirement.clarifications[0].options == []
    assert outcome.requirement.metrics == []
    assert outcome.requirement.confidence == {}
    assert value['metrics'] is None  # Do not mutate the original model response.


def test_null_legacy_clarification_options_and_nested_description():
    outcome = required_analysis({'clarification_question': '请补充统计目标', 'clarifications': None, 'clarification_options': None})
    assert outcome.plan is None and outcome.requirement.clarifications[0].options == []
    outcome = required_analysis({'clarification_question': '请补充统计目标', 'clarification_options': [
        {'id': 'balance', 'label': '贷款余额', 'value': '贷款余额', 'description': None},
    ]})
    assert outcome.requirement.clarifications[0].options[0].description == ''


def test_null_optional_fields_preserve_validated_query_and_relation_path():
    outcome = required_analysis({
        'metrics': ['loan_balance'], 'dimensions': ['branch.name'],
        'fields': None, 'filters': None, 'calculations': None,
        'clarifications': None, 'confidence': {'subject': None},
        'relationship_path': None, 'assumptions': None, 'analysis_steps': None,
    })
    assert outcome.plan.metrics == ['loan_balance']
    assert outcome.plan.filters == []
    assert outcome.plan.relationship_path == ['loan_belongs_to_branch']
    assert outcome.requirement.status == 'ready'


def test_null_object_shortlist_requests_clarification_in_large_domain():
    ontology = {**ONTOLOGY, 'objects': [{'name': f'object_{i}'} for i in range(17)]}
    outcome = required_analysis({'objects': None}, ontology)
    assert outcome.plan is None
    assert outcome.requirement.status == 'awaiting_clarification'


@pytest.mark.parametrize('key,bad_value', [
    ('metrics', 'loan_balance'), ('metrics', [None]), ('filters', {}),
    ('clarifications', [None]), ('clarification_options', [None]), ('confidence', []),
])
def test_non_null_malformed_fields_fail_closed(key, bad_value):
    with pytest.raises(ValueError):
        required_analysis({'metrics': ['loan_balance'], 'clarification_question': '请确认业务口径', key: bad_value})


def test_analyst_accepts_only_published_ontology_terms():
    model = StubModel(
        {
            "intent": "按机构统计贷款余额",
            "metrics": ["loan_balance"],
            "dimensions": ["branch.name"],
            "fields": [],
            "filters": [{"field": "loan.status", "operator": "eq", "value": "active"}],
            "grain": "branch",
            "assumptions": ["有效贷款"],
        }
    )

    outcome = asyncio.run(
        AnalystAgent(model).analyse("按分行看贷款余额", [], ONTOLOGY, {"branch_ids": [1]})
    )

    assert outcome.model_mode == "llm"
    assert outcome.plan is not None
    assert outcome.plan.metrics == ["loan_balance"]
    assert outcome.plan.relationship_path == ["loan_belongs_to_branch"]


def test_optional_model_invalid_field_falls_back_without_executing_it():
    model = StubModel(
        {
            "intent": "越界查询",
            "metrics": ["loan_balance"],
            "dimensions": ["customer.secret"],
            "fields": [],
            "filters": [],
        }
    )

    outcome = asyncio.run(
        AnalystAgent(model).analyse("按分行看贷款余额", [], ONTOLOGY, {"branch_ids": [1]})
    )

    assert outcome.model_mode == "deterministic"
    assert outcome.plan is not None
    assert "customer.secret" not in outcome.plan.dimensions


def test_required_model_invalid_field_fails_closed():
    model = StubModel(
        {
            "intent": "越界查询",
            "metrics": ["loan_balance"],
            "dimensions": ["customer.secret"],
            "fields": [],
            "filters": [],
        }
    )
    model.required = True

    with pytest.raises(ValueError, match="未发布字段"):
        asyncio.run(AnalystAgent(model).analyse("按分行看贷款余额", [], ONTOLOGY, {"branch_ids": [1]}))


def test_deterministic_analysis_selects_all_metrics_mentioned_by_synonym():
    outcome = AnalystAgent(StubModel({}))._deterministic_analysis(
        "按分行查询贷款余额和90天逾期率", [], ONTOLOGY, {"branch_ids": [1]}
    )

    assert outcome.plan is not None
    assert outcome.plan.metrics == ["loan_balance"]

    ontology = {
        **ONTOLOGY,
        "objects": ONTOLOGY["objects"]
        + [
            {
                "name": "payment",
                "label": "还款表现",
                "properties": [
                    {"name": "overdue_amount", "label": "逾期金额"},
                    {"name": "days_overdue", "label": "逾期天数"},
                ],
            }
        ],
        "relations": ONTOLOGY["relations"]
        + [{"name": "loan_has_payment", "from": "loan", "to": "payment"}],
        "metrics": ONTOLOGY["metrics"]
        + [
            {
                "name": "overdue_90_rate",
                "label": "90+逾期率",
                "aliases": ["90天逾期率"],
                "expression": "SUM(payment.overdue_amount WHERE payment.days_overdue >= 90) / SUM(loan.outstanding_balance)",
                "grain": "branch",
            }
        ],
    }
    outcome = AnalystAgent(StubModel({}))._deterministic_analysis(
        "按分行查询贷款余额和90天逾期率", [], ontology, {"branch_ids": [1]}
    )
    assert outcome.plan is not None
    assert outcome.plan.metrics == ["loan_balance", "overdue_90_rate"]
    assert outcome.plan.relationship_path == ["loan_belongs_to_branch", "loan_has_payment"]
