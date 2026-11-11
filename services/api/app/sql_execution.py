"""Sequential SQL execution and bounded, error-directed repair."""

import asyncio
import re
from semora_contracts import ExecutionResult, RunStatus
from .executor_errors import ExecutorOperationError
from .sql_errors import SQLIssue, issue_data, repairable
from .sql_dialect import compile_step
from .result_engine import local_query


class SQLExecution:
    def __init__(self, workflow, run, ontology, settings):
        self.w, self.run, self.ontology, self.settings = workflow, run, ontology, settings

    def remember_rejection(self, step, artifact, error, stage):
        from .sql_feedback import record_sql_rejection
        try:
            entry = record_sql_rejection(step, artifact, error, stage)
        except Exception:
            self.run.delivery_warnings.append("本次SQL兼容性记录暂未保存，继续尝试定向修复。")
            return
        if entry:
            self.w.emit(self.run, "sql.compatibility.recorded", self.run.status,
                        {"step": step.name, "summary": "已记录数据库拒绝该SQL的原因，进入定向修复", **entry}, "executor")

    def artifact(self, step, results=None, *, template=False):
        value = compile_step(
            step,
            self.ontology,
            results,
            template=template,
            row_limit=int(self.settings.get("max_result_rows", 1000)),
        )
        if self.run.requirement:
            value.requirement_version = self.run.requirement.version
            value.requirement_hash = self.run.requirement.content_hash
        return value

    async def compile(self):
        self.run.source_artifacts = {}
        prior = {}
        for step in self.run.plan.sql_program.steps:
            while True:
                try:
                    artifact = self.artifact(step, prior, template=True)
                    break
                except SQLIssue as exc:
                    if not repairable(exc):
                        raise
                    from .sql_authoring import repair_step

                    await repair_step(self.w, self.run, step.name, issue_data(exc))
            self.run.source_artifacts[step.name] = artifact
            prior[step.name] = ExecutionResult(
                columns=step.output_columns, rows=[], row_count=0, duration_ms=0
            )
        self.run.sql = self.run.source_artifacts[self.run.plan.sql_program.steps[-1].name]
        self.run.sql_explanation = self.run.plan.sql_program.summary
        self.w.emit(
            self.run,
            "sql.ready",
            RunStatus.COMPILING,
            {"sql": self.run.sql.model_dump(mode="json")},
            "sql_author",
        )

    async def validate(self):
        for step in self.run.plan.sql_program.steps:
            artifact = self.run.source_artifacts[step.name]
            if step.engine == "local":
                artifact.compilation["validation"] = {
                    "state": "pending",
                    "method": "explain_after_inputs",
                }
                continue
            while True:
                error_stage = "baseline_validation"
                try:
                    artifact = self.artifact(step, template=True)
                    self.run.source_artifacts[step.name] = artifact
                    error_stage = "explain"
                    response = await self.w.estimate(
                        artifact.physical_sql, artifact.parameters, artifact.data_source_id
                    )
                    if response.get("source") == "unavailable":
                        raise ExecutorOperationError("capability")
                    artifact.compilation["validation"] = {
                        "state": "template_validated" if step.inputs else "validated",
                        "method": "explain",
                    }
                    self.w.emit(
                        self.run,
                        "sql.validation.completed",
                        RunStatus.COMPILING,
                        {
                            "step": step.name,
                            "summary": "数据库已验证查询模板" if step.inputs else "数据库已验证SQL",
                        },
                        "executor",
                    )
                    break
                except (ExecutorOperationError, SQLIssue) as exc:
                    if isinstance(exc, ExecutorOperationError) and exc.kind == "capability":
                        artifact.compilation["validation"] = {
                            "state": "unavailable",
                            "method": "explain",
                        }
                        self.run.delivery_warnings.append(
                            step.name + "：数据库预校验能力不可用，将通过实际只读执行验证。"
                        )
                        break
                    if not repairable(exc):
                        raise
                    artifact.compilation["validation"] = {
                        "state": "invalid",
                        "error": issue_data(exc, error_stage),
                    }
                    self.remember_rejection(step, artifact, exc, error_stage)
                    from .sql_authoring import repair_step

                    await repair_step(self.w, self.run, step.name, issue_data(exc, error_stage))
        self.run.sql = self.run.source_artifacts[self.run.plan.sql_program.steps[-1].name]

    async def execute(self):
        results = {}
        from semora_agent.model_diagnostics import debug_payload

        for step in self.run.plan.sql_program.steps:
            self.w.emit(
                self.run,
                "source.step.started",
                RunStatus.EXECUTING,
                {"step": step.name, "summary": step.purpose},
                "executor",
            )
            revalidate = False
            while True:
                error_stage = "baseline_validation"
                try:
                    artifact = self.artifact(step, results)
                    self.run.source_artifacts[step.name] = artifact
                    self.run.sql = artifact
                    debug_payload(
                        "sql.execute.request",
                        {
                            "step": step.name,
                            "sql": artifact.physical_sql,
                            "parameters": artifact.parameters,
                            "data_source_id": step.data_source_id,
                            "engine": step.engine,
                        },
                    )
                    if step.engine == "local":
                        error_stage = "execution"
                        try:
                            result = await asyncio.to_thread(
                                local_query, artifact, results, self.settings
                            )
                        except Exception as exc:
                            if isinstance(exc, SQLIssue):
                                raise
                            # Only binder/parser/conversion errors are SQL repairs.
                            import duckdb

                            if isinstance(
                                exc,
                                (
                                    duckdb.BinderException,
                                    duckdb.ParserException,
                                    duckdb.ConversionException,
                                    duckdb.CatalogException,
                                ),
                            ):
                                detail = re.sub(r"'(?:[^']|'')*'", "'<value>'", str(exc))
                                raise SQLIssue(detail[:1600]) from exc
                            raise
                    else:
                        if revalidate:
                            error_stage = "explain"
                            try:
                                await self.w.estimate(
                                    artifact.physical_sql, artifact.parameters, step.data_source_id
                                )
                            except ExecutorOperationError as exc:
                                if exc.kind != "capability":
                                    raise
                        error_stage = "execution"
                        result = await self.w.execute(
                            artifact.physical_sql,
                            artifact.parameters,
                            [],
                            {
                                **self.settings,
                                "data_source_id": step.data_source_id,
                                "execution_id": self.run.id,
                            },
                        )
                    if result.columns != step.output_columns:
                        raise SQLIssue(
                            "实际输出列与步骤契约不一致：实际"
                            + str(result.columns)
                            + "；应为"
                            + str(step.output_columns)
                        )
                    if result.truncated and step.name != self.run.plan.sql_program.steps[-1].name:
                        raise SQLIssue(
                            "本步骤结果超过中间结果上限，不能完整传递给后续查询。"
                            "请保持确认口径，检查是否可将用户要求的排序和Top N、去重或聚合移到本源SQL中；"
                            "不能任意截取明细或缩小用户范围。当前上限："
                            + str(self.settings.get("max_result_rows", 1000))
                        )
                    break
                except Exception as exc:
                    if not repairable(exc):
                        raise
                    if step.name in self.run.source_artifacts:
                        self.remember_rejection(step, self.run.source_artifacts[step.name], exc, error_stage)
                    if step.name in self.run.source_artifacts:
                        self.run.source_artifacts[step.name].compilation["validation"] = {
                            "state": "invalid",
                            "error": issue_data(exc, error_stage),
                        }
                    from .sql_authoring import repair_step

                    await repair_step(self.w, self.run, step.name, issue_data(exc, error_stage))
                    revalidate = True
                    # The next iteration revalidates and executes the repaired
                    # statement. Earlier successful source queries are not replayed.
            artifact.compilation["validation"] = {
                "state": "validated",
                "method": "execution",
                "inputs_bound": True,
            }
            results[step.name] = result
            self.run.source_results[step.name] = result
            from .sql_learning import record_sql_success
            try:
                record_sql_success(step, result)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("SQL capability learning was not saved")
            from .control_state import record_receipt
            from .run_state import digest

            receipt = {
                "step": step.name,
                "row_count": result.row_count,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "output_hash": digest(result.model_dump(mode="json")),
            }
            record_receipt(self.run, receipt)
            self.w.emit(
                self.run,
                "source.step.completed",
                RunStatus.EXECUTING,
                {**receipt, "summary": step.purpose},
                "executor",
            )
            self.w.store.save_run(self.run)
        self.run.result = results[self.run.plan.sql_program.steps[-1].name]
        if self.run.result.truncated:
            self.run.delivery_warnings.append("最终结果达到返回上限，当前展示为截断结果。")
        self.w.emit(
            self.run,
            "execution.completed",
            RunStatus.EXECUTING,
            {"result": self.run.result.model_dump(mode="json")},
            "executor",
        )
