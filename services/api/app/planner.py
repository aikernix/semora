from __future__ import annotations

import hashlib
import re
from typing import Any
import sqlglot
from sqlglot import exp

from semora_contracts import QueryPlan, SQLArtifact

try:
    from .query_stages import QueryStages
except ImportError:  # Semantic planner deploys these two modules at its root.
    from query_stages import QueryStages


class PlanningError(ValueError):
    pass


def adapt_mysql_artifact(artifact: SQLArtifact) -> SQLArtifact:
    """Translate the compiled SQL while preserving driver-bound parameters."""
    names: dict[str, str] = {}
    def protect(match):
        token = f"__semora_param_{len(names)}__"
        names[token] = match.group(0)
        return token
    protected = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", protect, artifact.physical_sql)
    ast = sqlglot.parse_one(protected, read="postgres")
    # The published MDL uses public as its default PG namespace. MySQL uses
    # the selected database; explicit non-public schema mappings stay intact.
    from sqlglot import exp
    # MySQL's bare DECIMAL defaults to scale zero and would lose cents.
    for cast in ast.find_all(exp.Cast):
        target = cast.args.get("to")
        if isinstance(target, exp.DataType) and target.this == exp.DataType.Type.DECIMAL and not target.expressions:
            cast.set("to", exp.DataType.build("DECIMAL(38, 10)", dialect="mysql"))
    for table in ast.find_all(exp.Table):
        if table.db == "public":
            table.set("db", None)
        if table.catalog == "semora":
            table.set("catalog", None)
    sql = ast.sql(dialect="mysql", unsupported_level=sqlglot.ErrorLevel.RAISE)
    for token, placeholder in names.items():
        sql = re.sub(rf"\b{token}\b", lambda _: placeholder, sql)
    return artifact.model_copy(update={
        "physical_sql": sql, "dialect": "mysql",
        "artifact_hash": hashlib.sha256(f"{artifact.artifact_hash}\nmysql\n{sql}".encode()).hexdigest(),
    })


def compile_plan(plan: QueryPlan, ontology: dict[str, Any]) -> SQLArtifact:
    if plan.source_steps:
        raise PlanningError('跨源步骤必须由编排器分别编译，禁止转换成单连接SQL')
    objects = {item["name"]: item for item in ontology.get("objects", [])}
    relations = {item["name"]: item for item in ontology.get("relations", [])}
    metrics = {item["name"]: item for item in ontology.get("metrics", [])}
    for calculation in plan.calculations:
        if calculation.name in metrics:
            raise PlanningError('临时计算不能覆盖已发布指标')
        known = {f"{obj['name']}.{prop['name']}" for obj in objects.values() for prop in obj.get('properties',[])}
        known.update(f'{stage.name}.{measure.name}' for stage in plan.stages for measure in stage.measures)
        if calculation.field and calculation.field not in known:
            raise PlanningError('临时计算引用了未映射字段')
        if calculation.function != 'count' and not calculation.field:
            raise PlanningError('临时计算缺少字段')
        argument = calculation.field or '*'
        expression = f"COUNT(DISTINCT {argument})" if calculation.function == 'count_distinct' else f"{calculation.function.upper()}({argument})"
        metrics[calculation.name] = {'name':calculation.name,'label':calculation.label or calculation.name,'expression':expression,'grain':plan.grain,'format':'number'}
    if not objects:
        raise PlanningError("本体没有可编译的对象")
    ontology_datasource = str(ontology.get("dataSourceId") or "")
    if not ontology_datasource or plan.data_source_id != ontology_datasource:
        raise PlanningError("QueryPlan 数据源与本体绑定不一致")
    if any(name not in metrics for name in plan.metrics):
        raise PlanningError("QueryPlan 引用了未发布指标")
    try:
        stages = QueryStages(plan, ontology, metrics)
    except ValueError as exc:
        raise PlanningError(str(exc)) from exc

    properties = {
        f"{object_name}.{item['name']}": item["column"]
        for object_name, object_value in objects.items()
        for item in object_value.get("properties", [])
    }
    property_definitions = {
        f"{object_name}.{item['name']}": item
        for object_name, object_value in objects.items()
        for item in object_value.get("properties", [])
    }
    for object_name, object_value in objects.items():
        if object_value.get("primaryKey"):
            properties[f"{object_name}.id"] = object_value["primaryKey"]

    def physical_ref(reference: str) -> str:
        if reference in stages.virtual_fields:
            return stages.virtual_fields[reference]
        object_name = reference.split(".", 1)[0]
        if reference not in properties:
            if object_name in objects:
                return reference
            raise PlanningError(f"字段未映射：{reference}")
        return f"{object_name}.{properties[reference]}"

    def replace_refs(expression: str) -> str:
        pattern = r"'(?:''|\\.|[^'])*'|\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b"
        return re.sub(pattern, lambda match: match.group(0) if match.group(0).startswith("'") else physical_ref(match.group(0)), expression)

    def metric_expression(metric: dict[str, Any]) -> str:
        expression = metric["expression"]
        conditional = re.compile(r"SUM\((\w+\.\w+)\s+WHERE\s+(.+?)\)", re.IGNORECASE)
        expression = conditional.sub(lambda match: f"SUM(CASE WHEN {match.group(2)} THEN {match.group(1)} ELSE 0 END)", expression)
        expression = replace_refs(expression)
        tree = sqlglot.parse_one(expression, read='postgres')
        for division in tree.find_all(exp.Div):
            division.set('this', exp.Mul(this=exp.Literal.number('1.0'), expression=division.this.copy()))
            if not isinstance(division.expression, exp.Nullif):
                division.set('expression', exp.Nullif(this=division.expression.copy(), expression=exp.Literal.number(0)))
        expression = tree.sql(dialect='postgres')
        if metric.get("format") == "percentage":
            expression = f"100.0 * ({expression})"
        return f"ROUND(({expression})::numeric, 2)"

    selected_relations = []
    for name in plan.relationship_path:
        if name not in relations:
            raise PlanningError(f"QueryPlan 引用了未发布关系：{name}")
        selected_relations.append(relations[name])

    required_objects = {item.split(".", 1)[0] for item in plan.dimensions + plan.fields}
    required_objects.update(item.field.split(".", 1)[0] for item in plan.filters)
    required_objects.update(stages.required_objects())
    selected_objects = required_objects | {r[key] for r in selected_relations for key in ('from', 'to')}
    if any(str(objects[name].get('dataSourceId') or ontology_datasource) != plan.data_source_id for name in selected_objects):
        raise PlanningError('不同数据源对象不能直接JOIN，请使用source_steps分步查询')
    scope_field = str(ontology.get("scopeField") or "")
    if plan.data_scope.get("branch_ids"):
        if not scope_field:
            raise PlanningError("本体未定义机构权限 scopeField，已安全停止编译")
        physical_ref(scope_field)
        required_objects.add(scope_field.split(".", 1)[0])
    requested_root = str(plan.grain or "")
    root = requested_root if requested_root in objects else sorted(required_objects or set(objects))[0]

    joined = {root}
    joins: list[str] = []
    pending = list(selected_relations)
    while pending:
        progress = False
        for relation in list(pending):
            source, target = relation["from"], relation["to"]
            if source in joined and target not in joined:
                next_object = target
            elif target in joined and source not in joined:
                next_object = source
            elif source in joined and target in joined:
                pending.remove(relation)
                progress = True
                continue
            else:
                continue
            join_keyword = "LEFT JOIN" if relation.get("optional") else "JOIN"
            if next_object in stages.by_source:
                join_keyword = 'LEFT JOIN' if stages.by_source[next_object].join_type == 'left' else 'JOIN'
            table = stages.cte_names.get(next_object, objects[next_object]['table'])
            joins.append(f"{join_keyword} {table} {next_object} ON {replace_refs(relation['condition'])}")
            joined.add(next_object)
            pending.remove(relation)
            progress = True
        if not progress:
            raise PlanningError("关系路径不连续，无法生成 JOIN")
    if not required_objects.issubset(joined):
        missing = ", ".join(sorted(required_objects - joined))
        raise PlanningError(f"关系路径未覆盖对象：{missing}")

    select_parts: list[str] = []
    semantic_parts: list[str] = []
    group_parts: list[str] = []
    sensitive_columns: list[str] = []
    for reference in dict.fromkeys(plan.dimensions + plan.fields):
        alias = reference.replace(".", "_")
        expression = physical_ref(reference)
        select_parts.append(f"{expression} AS {alias}")
        semantic_parts.append(reference)
        if property_definitions.get(reference, {}).get("sensitive"):
            sensitive_columns.append(alias)
        if plan.metrics:
            group_parts.append(expression)
    for name in plan.metrics:
        compiled_metric = {**metrics[name], 'expression': stages.expressions[name].sql(dialect='postgres')}
        select_parts.append(f"{metric_expression(compiled_metric)} AS {name}")
        semantic_parts.append(f"{metrics[name]['expression']} AS {name}")
        if stages.sensitive_metric(name, property_definitions):
            sensitive_columns.append(name)
    if not select_parts:
        raise PlanningError("QueryPlan 没有指标或字段")

    parameters: dict[str, Any] = {}
    stage_sql = stages.render(physical_ref, parameters)
    predicates: list[str] = []
    operators = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    for index, item in enumerate(plan.filters):
        parameter = f"filter_{index}"
        field = physical_ref(item.field)
        if item.operator in {'is_null', 'is_not_null'}:
            predicates.append(f"{field} IS {'NOT ' if item.operator == 'is_not_null' else ''}NULL")
        elif item.operator in operators:
            predicates.append(f"{field} {operators[item.operator]} %({parameter})s")
            parameters[parameter] = item.value
        elif item.operator == "in":
            if not isinstance(item.value, list) or not item.value or len(item.value) > 1000:
                raise PlanningError("IN 过滤值必须是 1 到 1000 项的列表")
            names = []
            for value_index, value in enumerate(item.value):
                name = f"{parameter}_{value_index}"
                names.append(f"%({name})s")
                parameters[name] = value
            predicates.append(f"{field} IN ({', '.join(names)})")
        elif item.operator == "between" and isinstance(item.value, list) and len(item.value) == 2:
            predicates.append(f"{field} BETWEEN %({parameter}_start)s AND %({parameter}_end)s")
            parameters[f"{parameter}_start"], parameters[f"{parameter}_end"] = item.value
        else:
            raise PlanningError(f"不支持的过滤操作：{item.operator}")

    if plan.time_range:
        match = re.search(r"(?:最近|过去|近)\s*(\d+)\s*个?月", plan.time_range)
        time_field = str(ontology.get("timeField") or "")
        if not time_field:
            time_field = next(
                (
                    reference
                    for reference, definition in property_definitions.items()
                    if reference.split(".", 1)[0] in joined and definition.get("type") in {"date", "datetime", "timestamp"}
                ),
                "",
            )
        if match and time_field:
            months = int(match.group(1))
            if months < 1 or months > 120:
                raise PlanningError("时间范围必须在 1 到 120 个月之间")
            if time_field.split(".", 1)[0] not in joined:
                raise PlanningError("本体 timeField 不在查询关系路径中")
            if time_field.split('.', 1)[0] in stages.by_source:
                if not any(f.field == time_field for stage in plan.stages for f in stage.filters):
                    raise PlanningError('时间字段已被预聚合，请将明确日期条件落实到对应stages.filters，不能在聚合后忽略时间或猜测日期')
            else:
                predicates.append(f"{physical_ref(time_field)} >= CURRENT_DATE - INTERVAL '{months} months'")
        elif plan.time_range.strip() not in {'全部','全部数据','不限','全量','所有历史','无时间限制'}:
            temporal_fields={reference for reference, definition in property_definitions.items() if definition.get('type') in {'date','datetime','timestamp'} or re.search(r'date|time|日期|时间',str(definition.get('label',''))+' '+str(definition.get('column','')),re.I)}
            temporal_filters = plan.filters + [f for stage in plan.stages for f in stage.filters]
            if not any(item.field in temporal_fields for item in temporal_filters):
                raise PlanningError('时间范围尚未转成明确日期字段和过滤条件，请先补充确认，禁止忽略时间范围')

    branch_ids = plan.data_scope.get("branch_ids")
    if branch_ids:
        scope_object = scope_field.split(".", 1)[0]
        if scope_object not in joined:
            raise PlanningError("机构权限范围存在但关系路径未包含 scopeField 对象")
        if not isinstance(branch_ids, list) or len(branch_ids) > 1000:
            raise PlanningError("机构权限范围必须是最多 1000 项的列表")
        names = []
        for branch_index, branch_id in enumerate(branch_ids):
            name = f"branch_id_{branch_index}"
            names.append(f"%({name})s")
            parameters[name] = branch_id
        predicates.append(f"{physical_ref(scope_field)} IN ({', '.join(names)})")

    physical_lines = [
        "SELECT " + ",\n       ".join(select_parts),
        f"FROM {stages.cte_names.get(root, objects[root]['table'])} {root}",
        *joins,
    ]
    if predicates:
        physical_lines.append("WHERE " + "\n  AND ".join(predicates))
    if group_parts:
        physical_lines.append("GROUP BY " + ", ".join(group_parts))
    if plan.metrics:
        physical_lines.append(f"ORDER BY {plan.metrics[0]} DESC")
    physical_lines.append("LIMIT 1000")
    physical_sql = stage_sql + "\n".join(physical_lines)

    semantic_sql = "SELECT " + ",\n       ".join(semantic_parts) + f"\nFROM {root}"
    if plan.relationship_path:
        semantic_sql += "\nUSING RELATIONSHIPS " + ", ".join(plan.relationship_path)
    semantic_sql += "\nLIMIT 1000"
    artifact_hash = hashlib.sha256(
        f"{ontology_datasource}\n{semantic_sql}\n{physical_sql}\n{sorted(parameters.items())}\n{sorted(sensitive_columns)}".encode()
    ).hexdigest()
    return SQLArtifact(
        semantic_sql=semantic_sql,
        physical_sql=physical_sql,
        data_source_id=ontology_datasource,
        parameters=parameters,
        sensitive_columns=sensitive_columns,
        artifact_hash=artifact_hash,
        compilation=stages.summary(),
    )
