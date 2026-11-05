"""Bounded, deterministic A→B execution. Agents never receive intermediate rows."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from semora_agent.multisource import output_alias, property_definition, source_ontology, type_family, validate_source_steps
from semora_contracts import ExecutionResult, GateDecision, QueryFilter, QueryPlan, RunStatus, SystemSettings

from .gate import review_sql
from .planner import adapt_mysql_artifact
from .datasources import datasource_service


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def normalize_key(value, family):
    if value is None:
        return None
    if family == 'text' and isinstance(value, str):
        return value
    if family == 'integer' and isinstance(value, int) and not isinstance(value, bool):
        return value
    if family == 'number' and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
            if number.is_finite():
                return number
        except InvalidOperation:
            pass
    raise ValueError('关联键返回类型与本体不一致，禁止猜测或有损转换')


class SourceExecution:
    def __init__(self, workflow, run, ontology, settings):
        self.workflow, self.run, self.ontology = workflow, run, ontology
        self.settings = SystemSettings.model_validate(settings)
        self.steps = run.plan.source_steps
        validate_source_steps(run.plan, ontology)
        allowed = run.authorization.allowed_data_source_ids if run.authorization else None
        if allowed is not None and any(s.data_source_id not in allowed for s in self.steps):
            raise ValueError('当前身份无权访问跨源计划中的数据源')
        self.binding = digest({'plan': run.plan.model_dump(mode='json'),
                               'ontology': run.ontology.model_dump(mode='json') if run.ontology else None,
                               'authorization': run.authorization.model_dump(mode='json') if run.authorization else None,
                               'requirement': run.requirement.content_hash if run.requirement else None,
                               'policy': self.settings.policy_version})

    async def compile_step(self, step, keys=None):
        query = QueryPlan.model_validate(step.query.model_dump(mode='json'))
        for binding in step.inputs:
            query.filters.append(QueryFilter(field=binding.target_field, operator='in', value=keys if keys else [None]))
        ontology = source_ontology(self.ontology, step.data_source_id)
        source = self.workflow._datasource_payload(step.data_source_id)
        artifact = await self.workflow._compile(query, ontology)
        if source['type'] == 'mysql':
            artifact = adapt_mysql_artifact(artifact)
        if step.name != self.steps[-1].name:
            # Executor fetches max_rows+1. Asking SQL for the extra row is
            # essential: LIMIT 1000 alone hides an incomplete key collection.
            artifact.physical_sql, changed = re.subn(r'\bLIMIT\s+1000\s*;?\s*$',
                f'LIMIT {self.settings.max_result_rows + 1}', artifact.physical_sql, flags=re.I)
            if changed != 1:
                raise ValueError('无法为中间步骤设置完整性探测上限')
        artifact.requirement_version = self.run.requirement.version if self.run.requirement else ''
        artifact.requirement_hash = self.run.requirement.content_hash if self.run.requirement else ''
        artifact.artifact_hash = digest({'sql': artifact.physical_sql, 'parameters': artifact.parameters,
                                         'source': step.data_source_id, 'binding': self.binding})
        artifact.compilation.update({'source_step': step.name, 'execution': 'sequential_sources',
                                      'plan_binding': self.binding,
                                      'data_source_label': datasource_service.repository.get(step.data_source_id).name,
                                      'inputs_bound': not step.inputs or keys is not None,
                                      'input_dependencies': [b.model_dump(mode='json') for b in step.inputs]})
        return artifact

    async def compile_all(self):
        self.run.source_artifacts = {step.name: await self.compile_step(step) for step in self.steps}
        self.run.sql = self.run.source_artifacts[self.steps[-1].name]
        self.workflow.store.save_run(self.run)

    def gate(self, step, artifact, estimated_rows=None):
        # Scope filters are inserted by the trusted single-source compiler, and
        # sourceScopes plus the immutable authorization were verified above.
        return review_sql(artifact.physical_sql, dialect=artifact.dialect, require_organization_scope=False,
            approval_mode=self.settings.approval_mode, sensitive_columns=set(artifact.sensitive_columns),
            approve_sensitive_detail=self.settings.approve_sensitive_detail,
            estimated_rows=estimated_rows, large_result_threshold=self.settings.large_result_threshold,
            approve_large_result=self.settings.approve_large_result,
            cross_organization=bool(step.query.data_scope.get('cross_organization')),
            approve_cross_organization=self.settings.approve_cross_organization,
            max_sql_rows=self.settings.max_result_rows + 1 if step.name != self.steps[-1].name else 1000)

    def template_gate(self):
        gates = [self.gate(step, self.run.source_artifacts[step.name]) for step in self.steps]
        return GateDecision(allowed=all(g.allowed for g in gates),
            risk_level='high' if any(g.risk_level == 'high' for g in gates) else 'low',
            requires_approval=any(g.requires_approval for g in gates),
            checks=[check.model_copy(update={'name': f'{step.name}.{check.name}'}) for step, gate in zip(self.steps, gates) for check in gate.checks])

    def keys(self, binding):
        result = self.run.source_results[binding.source_step]
        if result.truncated:
            raise ValueError('中间查询结果被截断，不能把部分关联键当作完整条件继续查询')
        alias = output_alias(binding.source_field)
        if alias not in result.columns:
            raise ValueError('上一步未返回约定的关联键')
        if alias in result.summary.get('masked_columns', []):
            raise ValueError('关联键已脱敏，不能用脱敏值继续查询其他数据源')
        index = result.columns.index(alias)
        _, prop = property_definition(binding.source_field, self.ontology)
        family = type_family(prop['type'])
        values = {}
        attributes = {}
        indices = [result.columns.index(output_alias(f)) for f in binding.include_source_fields]
        for row in result.rows:
            value = normalize_key(row[index], family)
            if value is not None:
                values.setdefault(value, value)
                attrs = [row[i] for i in indices]
                if value in attributes and attributes[value] != attrs:
                    raise ValueError('关联键对应多条不同记录，须先明确汇总粒度，未执行后续查询')
                attributes[value] = attrs
        if len(values) > min(1000, self.settings.max_result_rows):
            raise ValueError('关联键超出完整查询上限，需先按关联键汇总；未执行后续查询')
        return list(values.values())

    def enrich(self, step, result):
        if not step.inputs or not step.inputs[0].include_source_fields:
            return result
        binding = step.inputs[0]
        source = self.run.source_results[binding.source_step]
        key_index = source.columns.index(output_alias(binding.source_field))
        indices = [source.columns.index(output_alias(field)) for field in binding.include_source_fields]
        family = type_family(property_definition(binding.source_field, self.ontology)[1]['type'])
        lookup = {}
        for row in source.rows:
            key = normalize_key(row[key_index], family)
            if key is None:
                continue
            values = [row[i] for i in indices]
            if key in lookup and lookup[key] != values:
                raise ValueError('上一步关联键对应多条不同记录，无法唯一补充字段；请先明确汇总粒度')
            lookup[key] = values
        target_index = result.columns.index(output_alias(binding.target_field))
        columns = [f'{binding.source_step}__{output_alias(field)}' for field in binding.include_source_fields]
        if set(columns) & set(result.columns):
            raise ValueError('跨源结果列名冲突')
        result.rows = [row + lookup.get(normalize_key(row[target_index], family), [None] * len(columns)) for row in result.rows]
        result.columns += columns
        result.bytes_returned = len(json.dumps(result.rows, ensure_ascii=False, default=str).encode())
        if result.bytes_returned > self.settings.max_result_mb * 1024 * 1024:
            raise ValueError('跨源合并结果超过大小上限')
        result.summary['masked_columns'] = list(result.summary.get('masked_columns', [])) + [columns[i] for i, field in enumerate(binding.include_source_fields) if output_alias(field) in source.summary.get('masked_columns', [])]
        return result

    async def execute(self, state):
        run, workflow = self.run, self.workflow
        if run.review is None or run.review.decision != 'approved':
            raise ValueError('跨源计划尚未通过Reviewer，禁止执行任何数据源查询')
        if set(run.source_artifacts) != {s.name for s in self.steps} or any(a.compilation.get('plan_binding') != self.binding for a in run.source_artifacts.values()):
            raise ValueError('跨源计划已偏离Reviewer复核版本，禁止执行')
        saved = (run.agent_memory.get('source_execution') or [{}])[-1]
        if saved and saved.get('binding') != self.binding:
            raise ValueError('跨源计划、权限或策略已变化，旧中间结果不能复用')
        run.agent_memory['source_execution'] = [{'binding': self.binding}]
        for step in self.steps:
            if step.name in run.source_results:
                event = next((e for e in reversed(run.events) if e.event in {'source.step.completed', 'source.step.skipped'} and e.data.get('step') == step.name), None)
                if not event or event.data.get('output_hash') != digest(run.source_results[step.name].model_dump(mode='json')):
                    raise ValueError('中间结果与审计摘要不一致，禁止恢复后续查询')
                continue
            keys = self.keys(step.inputs[0]) if step.inputs else None
            if step.inputs and not keys:
                columns = list(dict.fromkeys(output_alias(f) for f in step.query.dimensions + step.query.fields)) + step.query.metrics
                result = ExecutionResult(columns=columns, rows=[], row_count=0, duration_ms=0, truncated=False,
                                         summary={'short_circuit': '前置步骤没有关联键', 'masked_columns': []})
                result = self.enrich(step, result)
                run.source_results[step.name] = result
                workflow.emit(run, 'source.step.skipped', RunStatus.EXECUTING, {'step': step.name, 'reason': '前置关联键为空', 'row_count': 0, 'output_hash': digest(result.model_dump(mode='json'))}, 'executor')
                continue
            artifact = await self.compile_step(step, keys)
            run.source_artifacts[step.name] = artifact
            estimate = await workflow.estimate(artifact.physical_sql, artifact.parameters, step.data_source_id)
            gate = self.gate(step, artifact, int(estimate['estimated_rows']))
            workflow.emit(run, 'source.step.gated', RunStatus.GATING, {'step': step.name, 'gate': gate.model_dump(mode='json'), 'key_count': len(keys or [])}, 'sql_gate')
            if not gate.allowed:
                raise ValueError(f'步骤 {step.name} 未通过SQL门禁')
            run.sql, run.gate = artifact, gate
            if run.approval and run.approval.sql_hash != artifact.artifact_hash:
                if run.approval.status == 'pending':
                    run.approval.status = 'expired'
                run.approval = None
            await workflow._approval_node(state, source_step=True)
            if run.status == RunStatus.FAILED:
                return
            workflow.emit(run, 'source.step.started', RunStatus.EXECUTING, {'step': step.name, 'data_source_id': step.data_source_id,
                'key_count': len(keys or []), 'sql': artifact.physical_sql, 'parameter_hash': digest(artifact.parameters)}, 'executor')
            result = await workflow.execute(artifact.physical_sql, artifact.parameters, artifact.sensitive_columns,
                {**state['settings'], 'execution_id': run.id, 'data_source_id': step.data_source_id})
            if result.row_count != len(result.rows) or any(len(row) != len(result.columns) for row in result.rows):
                raise ValueError('执行器返回结果结构不一致')
            if result.row_count > self.settings.max_result_rows:
                raise ValueError('执行器返回行数超过限制')
            if len(json.dumps(result.rows, ensure_ascii=False, default=str).encode()) > self.settings.max_result_mb * 1024 * 1024:
                raise ValueError('执行器返回结果超过大小限制')
            if step.name != self.steps[-1].name and result.truncated:
                raise ValueError(f'步骤 {step.name} 中间结果不完整，已停止后续查询')
            result = self.enrich(step, result)
            run.source_results[step.name] = result
            workflow.emit(run, 'source.step.completed', RunStatus.EXECUTING, {'step': step.name, 'row_count': result.row_count,
                'duration_ms': result.duration_ms, 'truncated': result.truncated, 'output_hash': digest(result.model_dump(mode='json')), 'finished_at': datetime.now(timezone.utc).isoformat()}, 'executor')
        run.result = run.source_results[self.steps[-1].name].model_copy(deep=True)
        run.result.summary.update({'execution': 'sequential_sources', 'sources': [s.data_source_id for s in self.steps],
                                  'consistency': '各源为独立只读事务，不代表同一时刻的全局快照'})
        workflow.emit(run, 'execution.completed', RunStatus.EXECUTING, {'result': run.result.model_dump(mode='json')}, 'executor')
