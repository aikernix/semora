"""Bounded, deterministic A→B execution. Agents never receive intermediate rows."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from semora_query_core.sources import output_alias, property_definition, source_ontology, type_family, validate_source_steps
from semora_contracts import BoundKeyFilter, ExecutionResult, GateDecision, QueryFilter, QueryPlan, RunStatus, SystemSettings

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
        self.active_step=None
        self.active_operation=None
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
        if step.regroup:
            query.aggregate_precision='raw'
        for binding in step.inputs:
            if len(binding.pairs) == 1:
                query.filters.append(QueryFilter(field=binding.pairs[0].target_field, operator='in', value=keys if keys else [None]))
            else:
                query.bound_key_filters.append(BoundKeyFilter(fields=[p.target_field for p in binding.pairs], rows=[list(k) for k in (keys or [])]))
        ontology = source_ontology(self.ontology, step.data_source_id)
        source = self.workflow._datasource_payload(step.data_source_id)
        artifact = await self.workflow._compile(query, ontology)
        if source['type'] == 'mysql':
            artifact = adapt_mysql_artifact(artifact)
        if query.limit is not None and not re.search(rf'\bLIMIT\s+{query.limit}\s*;?\s*$', artifact.physical_sql, flags=re.I):
            raise ValueError('编译后的SQL没有保留用户指定的Top N，禁止执行')
        if query.limit is not None and query.limit > self.settings.max_result_rows:
            raise ValueError('请求的Top N超过当前结果行数策略，不能静默截断')
        if (step.name != self.steps[-1].name or step.regroup or step.membership) and query.limit is None:
            # Executor fetches max_rows+1. Asking SQL for the extra row is
            # essential: LIMIT 1000 alone hides an incomplete key collection.
            artifact.physical_sql, changed = re.subn(r'\bLIMIT\s+1000\s*;?\s*$',
                f'LIMIT {self.settings.max_result_rows + 1}', artifact.physical_sql, flags=re.I)
            if changed != 1:
                raise ValueError('无法为中间步骤设置完整性探测上限')
        if len(artifact.physical_sql) > 100_000:
            raise ValueError('绑定关联键后的SQL超过100000字符上限，已停止后续查询')
        artifact.requirement_version = self.run.requirement.version if self.run.requirement else ''
        artifact.requirement_hash = self.run.requirement.content_hash if self.run.requirement else ''
        artifact.artifact_hash = digest({'sql': artifact.physical_sql, 'parameters': artifact.parameters,
                                         'source': step.data_source_id, 'binding': self.binding})
        artifact.compilation.update({'source_step': step.name, 'execution': 'sequential_sources',
                                      'plan_binding': self.binding,
                                      'data_source_label': datasource_service.repository.get(step.data_source_id).name,
                                      'inputs_bound': not step.inputs or keys is not None,
                                      'input_dependencies': [b.model_dump(mode='json') for b in step.inputs]})
        if step.name != self.steps[-1].name or step.regroup or step.membership:
            artifact.compilation['completeness_check']={
                'state':'enforced_at_execution','max_accepted_rows':self.settings.max_result_rows,
                'on_row_overflow':'fail_no_result','on_truncated':'fail_no_result',
                'checked_before':['enrichment','key_transfer','regroup','membership']}
        return artifact

    async def compile_all(self):
        self.run.source_artifacts = {step.name: await self.compile_step(step) for step in self.steps}
        self.run.sql = self.run.source_artifacts[self.steps[-1].name]
        self.workflow.store.save_run(self.run)

    def gate(self, step, artifact, estimated_rows=None,estimate_unavailable=False):
        # Scope filters are inserted by the trusted single-source compiler, and
        # sourceScopes plus the immutable authorization were verified above.
        return review_sql(artifact.physical_sql, dialect=artifact.dialect, require_organization_scope=False,
            approval_mode=self.settings.approval_mode, sensitive_columns=set(artifact.sensitive_columns),
            approve_sensitive_detail=self.settings.approve_sensitive_detail,
            estimated_rows=estimated_rows,estimate_unavailable=estimate_unavailable, large_result_threshold=self.settings.large_result_threshold,
            approve_large_result=self.settings.approve_large_result,
            cross_organization=bool(step.query.data_scope.get('cross_organization')),
            approve_cross_organization=self.settings.approve_cross_organization,
            max_sql_rows=self.settings.max_result_rows + 1 if step.name != self.steps[-1].name or step.regroup or step.membership else 1000)

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
        aliases = [output_alias(pair.source_field) for pair in binding.pairs]
        if any(alias not in result.columns for alias in aliases):
            raise ValueError('上一步未返回约定的关联键')
        if set(aliases) & set(result.summary.get('masked_columns', [])):
            raise ValueError('关联键已脱敏，不能用脱敏值继续查询其他数据源')
        key_indices = [result.columns.index(alias) for alias in aliases]
        families = [type_family(property_definition(pair.source_field, self.ontology)[1]['type']) for pair in binding.pairs]
        values = {}
        attributes = {}
        indices = [result.columns.index(output_alias(f)) for f in binding.include_source_fields]
        for row in result.rows:
            if any(row[index] is None for index in key_indices):
                continue
            parts = tuple(normalize_key(row[index], family) for index, family in zip(key_indices, families))
            value = parts[0] if len(parts) == 1 else parts
            if all(part is not None for part in parts):
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
        key_indices = [source.columns.index(output_alias(pair.source_field)) for pair in binding.pairs]
        indices = [source.columns.index(output_alias(field)) for field in binding.include_source_fields]
        families = [type_family(property_definition(pair.source_field, self.ontology)[1]['type']) for pair in binding.pairs]
        lookup = {}
        for row in source.rows:
            if any(row[index] is None for index in key_indices):
                continue
            key = tuple(normalize_key(row[index], family) for index, family in zip(key_indices, families))
            if any(value is None for value in key):
                continue
            values = [row[i] for i in indices]
            if key in lookup and lookup[key] != values:
                raise ValueError('上一步关联键对应多条不同记录，无法唯一补充字段；请先明确汇总粒度')
            lookup[key] = values
        target_indices = [result.columns.index(output_alias(pair.target_field)) for pair in binding.pairs]
        columns = [f'{binding.source_step}__{output_alias(field)}' for field in binding.include_source_fields]
        if set(columns) & set(result.columns):
            raise ValueError('跨源结果列名冲突')
        enriched_rows = []
        for row in result.rows:
            key = tuple(normalize_key(row[index], family) for index, family in zip(target_indices, families))
            if key not in lookup:
                raise ValueError('目标结果的关联键无法与来源精确匹配，请核对键值和数据库排序规则；未合并不确定结果')
            enriched_rows.append(row + lookup[key])
        result.rows = enriched_rows
        result.columns += columns
        result.bytes_returned = len(json.dumps(result.rows, ensure_ascii=False, default=str).encode())
        if result.bytes_returned > self.settings.max_result_mb * 1024 * 1024:
            raise ValueError('跨源合并结果超过大小上限')
        result.summary['masked_columns'] = list(result.summary.get('masked_columns', [])) + [columns[i] for i, field in enumerate(binding.include_source_fields) if output_alias(field) in source.summary.get('masked_columns', [])]
        return result

    async def execute(self, state):
        run, workflow = self.run, self.workflow
        if run.review is None or (run.review.decision != 'approved' and run.review.disposition!='advisory'):
            raise ValueError('跨源计划尚未通过Reviewer，禁止执行任何数据源查询')
        if set(run.source_artifacts) != {s.name for s in self.steps} or any(a.compilation.get('plan_binding') != self.binding for a in run.source_artifacts.values()):
            raise ValueError('跨源计划已偏离Reviewer复核版本，禁止执行')
        from .control_state import execution_binding,record_receipt,restore_receipt
        saved=execution_binding(run)
        if saved and saved != self.binding:
            raise ValueError('跨源计划、权限或策略已变化，旧中间结果不能复用')
        run.execution_binding=self.binding
        for step in self.steps:
            self.active_step=step.name
            if step.name in run.source_results:
                event = next((e for e in reversed(run.events) if e.event in {'source.step.completed', 'source.step.skipped'} and e.data.get('step') == step.name), None)
                receipt=restore_receipt(run,step.name)
                result_hash=digest(run.source_results[step.name].model_dump(mode='json'))
                if not event or not receipt or receipt.output_hash!=result_hash or event.data.get('output_hash') != result_hash:
                    raise ValueError('中间结果与审计摘要不一致，禁止恢复后续查询')
                continue
            keys = self.keys(step.inputs[0]) if step.inputs else None
            # A global aggregate over an empty relation still returns one row:
            # COUNT=0, SUM/AVG/MIN/MAX=NULL (or a published formula's value).
            # Let the database evaluate it with the bound empty-key predicate;
            # synthesizing [] would change SQL semantics.
            global_aggregate = bool(step.query.metrics) and not (step.query.dimensions or step.query.fields)
            if step.inputs and not keys and not global_aggregate:
                columns = list(dict.fromkeys(output_alias(f) for f in step.query.dimensions + step.query.fields)) + step.query.metrics
                result = ExecutionResult(columns=columns, rows=[], row_count=0, duration_ms=0, truncated=False,
                                         summary={'short_circuit': '前置步骤没有关联键', 'masked_columns': []})
                result = self.enrich(step, result)
                run.source_results[step.name] = result
                payload={'step':step.name,'reason':'前置关联键为空','row_count':0,'output_hash':digest(result.model_dump(mode='json'))}
                record_receipt(run,payload,skipped=True)
                workflow.emit(run,'source.step.skipped',RunStatus.EXECUTING,payload,'executor')
                continue
            self.active_operation='语义编译'
            artifact = await self.compile_step(step, keys)
            run.source_artifacts[step.name] = artifact
            self.active_operation='成本估算'
            from .cost_estimation import estimate_query
            estimate = await estimate_query(workflow,run,artifact)
            gate = self.gate(step, artifact, estimate['estimated_rows'],estimate_unavailable=estimate.get('source')=='unavailable')
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
            self.active_operation='只读查询'
            result = await workflow.execute(artifact.physical_sql, artifact.parameters, artifact.sensitive_columns,
                {**state['settings'], 'execution_id': run.id, 'data_source_id': step.data_source_id})
            if result.row_count != len(result.rows) or any(len(row) != len(result.columns) for row in result.rows):
                raise ValueError('执行器返回结果结构不一致')
            if result.row_count > self.settings.max_result_rows:
                raise ValueError('执行器返回行数超过限制')
            if len(json.dumps(result.rows, ensure_ascii=False, default=str).encode()) > self.settings.max_result_mb * 1024 * 1024:
                raise ValueError('执行器返回结果超过大小限制')
            if (step.name != self.steps[-1].name or step.regroup or step.membership) and result.truncated:
                raise ValueError(f'步骤 {step.name} 返回结果被截断（当前上限{self.settings.max_result_rows}行/{self.settings.max_result_mb}MB），无法完整传递关联键；请使用明确排序的Top N、缩小范围或按关联键汇总，已停止后续查询')
            result = self.enrich(step, result)
            run.source_results[step.name] = result
            payload={'step':step.name,'row_count':result.row_count,'duration_ms':result.duration_ms,
                'truncated':result.truncated,'output_hash':digest(result.model_dump(mode='json')),'finished_at':datetime.now(timezone.utc).isoformat()}
            record_receipt(run,payload)
            workflow.emit(run,'source.step.completed',RunStatus.EXECUTING,payload,'executor')
        run.result = run.source_results[self.steps[-1].name].model_copy(deep=True)
        if self.steps[-1].membership:
            from semora_query_core.results import membership_result
            spec=self.steps[-1].membership
            run.result=membership_result(self.steps[-1],run.source_results[spec.source_step],run.result,max_rows=self.settings.max_result_rows,ontology=self.ontology)
            run.result.bytes_returned=len(json.dumps(run.result.rows,ensure_ascii=False,default=str).encode())
            if run.result.bytes_returned>self.settings.max_result_mb*1024*1024:raise ValueError('存在性筛选结果超过大小限制')
            workflow.emit(run,'source.result.membership',RunStatus.EXECUTING,{
                'present':spec.present,'source_rows':run.source_results[spec.source_step].row_count,
                'row_count':run.result.row_count,'output_hash':digest(run.result.model_dump(mode='json'))},'executor')
        if self.steps[-1].regroup:
            from semora_query_core.results import regroup_result
            run.result=regroup_result(self.steps[-1],run.result,max_rows=self.settings.max_result_rows,ontology=self.ontology)
            run.result.bytes_returned=len(json.dumps(run.result.rows,ensure_ascii=False,default=str).encode())
            if run.result.bytes_returned>self.settings.max_result_mb*1024*1024:
                raise ValueError('再汇总结果超过大小上限')
            workflow.emit(run,'source.result.regrouped',RunStatus.EXECUTING,{
                'group_by':self.steps[-1].regroup.group_by,'partial_rows':run.source_results[self.steps[-1].name].row_count,
                'row_count':run.result.row_count,'output_hash':digest(run.result.model_dump(mode='json'))},'executor')
        run.result.summary.update({'execution': 'sequential_sources', 'sources': [s.data_source_id for s in self.steps],
                                  'consistency': '各源为独立只读事务，不代表同一时刻的全局快照'})
        workflow.emit(run, 'execution.completed', RunStatus.EXECUTING, {'result': run.result.model_dump(mode='json')}, 'executor')
