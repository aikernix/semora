"""One bounded review episode, including transport retries and directed rechecks."""
import asyncio
import os
import math
from time import monotonic

from semora_agent.model import ModelError,ReviewBudgetExhaustedError,ReviewTimeoutError,ReviewOutputError


async def run_review(run, operation, runner, *args, event, legacy=False, single_submission=False,
                     can_finalize=None, revision_key=None, **kwargs):
    budgets = run.agent_memory.setdefault('review_budgets', [{}])[0]
    state = budgets.setdefault(operation, {'attempts': 0, 'invocations': 0, 'seconds': 0.0})
    seconds = float(os.getenv('REVIEW_TIMEOUT_SECONDS', '120'))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ModelError('REVIEW_TIMEOUT_SECONDS 必须是大于0的有限数值')
    remaining_seconds = seconds - state['seconds']
    # First review: up to three requests. One directed recheck: one request.
    initial_limit = 4 if legacy else 3
    limit = min(initial_limit if state['invocations'] == 0 else 1, initial_limit + 1 - state['attempts'])
    inputs = state.setdefault('reviewed_inputs', [])
    repeated = state['invocations'] >= 2 and (not revision_key or revision_key in inputs)
    if repeated or state['invocations'] >= initial_limit + 1 or limit <= 0 or remaining_seconds <= 0:
        raise ReviewBudgetExhaustedError('本阶段复核预算已用完；保留已有方案与复核意见，不重新启动复核循环。')
    state['invocations'] += 1
    if revision_key:inputs.append(revision_key)
    models = http_attempts = consumed = 0
    followup_used = False

    def observe(name, data):
        nonlocal models, http_attempts, consumed, followup_used
        if name == 'agent.model.started' and models and single_submission:
            if followup_used or not (can_finalize and can_finalize()):
                raise ReviewOutputError('复核未提交有效结论；不重复请求相同资料或反复修订交付格式。')
            followup_used = True
        proposed_models = models + (name == 'agent.model.started')
        proposed_http = http_attempts + (name == 'agent.request.started')
        # Test adapters and non-HTTP providers may emit only model events.
        proposed = max(proposed_models, proposed_http)
        if proposed > limit:
            raise ReviewBudgetExhaustedError('本次复核请求预算已用完（包含网络重试）；停止重复调用。')
        state['attempts'] += proposed - consumed
        models, http_attempts, consumed = proposed_models, proposed_http, proposed
        event(name, data)

    started = monotonic()
    expired = False
    try:
        async with asyncio.timeout(remaining_seconds):
            return await runner(*args, event=observe, max_models=limit,
                                max_tools=(3 if legacy else 2) if state['invocations'] == 1 else 0, **kwargs)
    except TimeoutError as exc:
        expired = True
        raise ReviewTimeoutError('本阶段复核达到总时间预算；停止等待，保留已有方案与结果。') from exc
    finally:
        elapsed = monotonic() - started
        state['seconds'] += max(elapsed, remaining_seconds) if expired else elapsed
