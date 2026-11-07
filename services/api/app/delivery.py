"""Owner-controlled delivery policy. Model review is separate from hard checks."""
from semora_contracts import ReviewDecision,QueryDelivery,RunStatus
from semora_agent.model import (ModelServiceError,ReviewBudgetExhaustedError,ReviewTimeoutError,
                               ReviewOutputError,NoDeliveryError,PlanValidationError)
from semora_agent.progress import NoProgressError
from semora_agent.context_budget import ContextBudgetError
from semora_agent.tool_protocol import ToolProtocolError

RECOVERABLE_REVIEW_ERRORS=(ModelServiceError,ReviewBudgetExhaustedError,ReviewTimeoutError,
                          ReviewOutputError,NoDeliveryError,PlanValidationError,NoProgressError,ContextBudgetError,ToolProtocolError)


def best_effort(run):
    saved=run.agent_memory.get('delivery_policy') or []
    if saved:return saved[-1]['mode']=='best_effort'
    from .settings import settings_service
    return settings_service.get().delivery_mode=='best_effort'


def advisory(workflow,run,review,phase):
    if review.decision=='approved':return review
    messages=review.issues or [review.rationale]
    for message in messages:
        note=phase+'：'+message
        if note not in run.delivery_warnings:run.delivery_warnings.append(note)
    workflow.emit(run,'review.advisory',run.status,{'phase':phase,'issues':messages,
        'summary':'按尽力交付策略继续，保留复核意见供后续调整'},'conversation_owner')
    return review.model_copy(update={'disposition':'advisory'})


def unavailable_review(error,phase='pre_execution'):
    return ReviewDecision(decision='revise',issues=['独立复核暂未完成（'+type(error).__name__+'）'],
        rationale='本次模型复核未能交付有效意见，尚不能视为复核通过。',phase=phase,reviewer_mode='unavailable')


def next_action(run):
    error=(run.error or '').lower()
    failure=(run.agent_memory.get('execution_failure') or [{}])[-1]
    kind=failure.get('error_type')
    if kind=='auth':return '核对数据源账号、密码和来源授权，修正后继续；已有需求保留。'
    if kind=='permission':return '核对数据源及字段授权；不要通过修改SQL绕过权限。'
    if kind in {'connect','timeout','transient'}:return '连接恢复后可继续当前需求，复用仍有效的成果并重新校验；本次尝试已结束。'
    if kind in {'syntax','schema','unsupported_sql'}:return '查看数据库错误码与字段映射，修正执行方案后继续当前需求。'
    if run.agent_memory.get('interruption'):
        return '继续当前需求，系统将复用仍有效的已有成果并重新校验执行条件。'
    if any(word in error for word in ('1045','登录被拒绝','认证失败','password authentication','access denied')):
        return '在数据源配置中核对账号、密码及来源授权；修正后继续当前需求。'
    if any(word in error for word in ('权限','越权','门禁','无权')):
        return '查看未通过的权限或SQL检查项，修正配置或查询范围后继续；已有口径保留。'
    if any(word in error for word in ('编译','映射','未发布字段')):
        return '查看具体字段与编译检查项；核对本体映射或修正方案后继续，不必重建需求。'
    if any(word in error for word in ('复核','模型','超时','timeout')):
        return '可继续当前需求重试未完成步骤；已有口径与SQL保留，无需重复填写。'
    if any(word in error for word in ('连接','数据源','502')):
        return '检查数据源服务与连接配置，恢复后继续当前需求。'
    return '可以继续补充当前需求；已有口径将保留。'


def build_delivery(run):
    warnings=list(run.delivery_warnings)
    if run.plan and run.plan.query_intent:
        warnings+=['本次未完成：'+goal for goal in run.plan.query_intent.unfulfilled_goals]
    warnings=list(dict.fromkeys(warnings))
    if run.status==RunStatus.COMPLETED:
        return QueryDelivery(kind='result',title='查询结果（含待核对项）' if warnings else '查询已完成',
            summary=run.answer or '已返回实际查询结果。',warnings=warnings,
            next_action='可以继续补充口径或指出需要调整的部分。',
            result_verified=bool(run.result_review and run.result_review.decision=='approved'))
    if run.status==RunStatus.CLARIFYING:
        return QueryDelivery(kind='clarification',title='请补充当前查询',
            summary=run.requirement_draft.summary if run.requirement_draft else '请补充希望查询的对象、范围或指标。',
            warnings=warnings,next_action='回答澄清卡片或直接输入补充说明。')
    if run.status==RunStatus.FAILED:
        cancelled='取消' in (run.error or '')
        interrupted=bool(run.agent_memory.get('interruption'))
        kind='cancelled' if cancelled else ('interrupted' if interrupted else 'blocked')
        stage='已取得查询数据，但尚未完成本次交付。' if run.result else ('已生成SQL，但尚未完成查询。' if run.sql else '已保留当前需求与确认口径，尚未形成可执行结果。')
        summary='本次查询已取消。' if cancelled else stage+' '+(run.error or '当前步骤受阻。')
        return QueryDelivery(kind=kind,title='查询已取消' if cancelled else ('上次处理已中断，可继续' if interrupted else '当前交付与继续方式'),
            summary=summary,warnings=warnings,next_action=next_action(run))
    return None
