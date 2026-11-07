"""Same bounded estimate policy for single-source and sequential source steps."""
import asyncio

from .executor_errors import ExecutorOperationError


async def estimate_query(workflow,run,artifact):
    for attempt in range(2):
        try:
            result=await workflow.estimate(artifact.physical_sql,artifact.parameters,artifact.data_source_id)
            rows=result.get('estimated_rows')
            if isinstance(rows,bool) or not isinstance(rows,int) or rows<0:
                raise ExecutorOperationError('unknown')
            return result
        except ExecutorOperationError as exc:
            if exc.kind=='capability':
                message='成本估算不可用，未做行数预判；查询仍受执行限制和审批策略约束。'
                if message not in run.delivery_warnings:run.delivery_warnings.append(message)
                workflow.emit(run,'cost.estimate.degraded',run.status,{'summary':message,**exc.public()},'executor')
                return {'estimated_rows':None,'source':'unavailable'}
            if exc.retryable and attempt==0:
                workflow.emit(run,'cost.estimate.retry',run.status,{'summary':'估算连接暂时异常，仅重试一次','attempt':2,**exc.public()},'executor')
                await asyncio.sleep(.2)
                continue
            run.agent_memory['execution_failure']=[exc.public()]
            raise
