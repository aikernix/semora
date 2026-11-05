from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict
from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, PrivateAttr

from .model import ModelError, PlanValidationError


class AgentSubmission(BaseModel):
    """Deliver the final public analysis or review; never include hidden reasoning."""

    payload: dict[str, Any] = Field(description='结构化需求分析或复核决定，必须满足任务约定')


class ToolCallingModel(BaseChatModel):
    """Adapt the existing configured provider without another SDK or credentials store."""

    _client: Any = PrivateAttr()
    _event: Callable = PrivateAttr()
    _models: int = PrivateAttr(default=0)
    _tools: int = PrivateAttr(default=0)
    _force_submission: bool = PrivateAttr(default=False)
    _max_models: int = PrivateAttr(default=16)
    _max_tools: int = PrivateAttr(default=12)

    def __init__(self, client, event: Callable, max_models=16, max_tools=12):
        super().__init__()
        self._client = client
        self._event = event
        self._max_models, self._max_tools = max_models, max_tools

    @property
    def _llm_type(self):
        return 'semora-tool-calling'

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(item) for item in tools], **kwargs)

    def _generate(self, *args, **kwargs):
        raise NotImplementedError('Use async Agent execution')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._models += 1
        if self._models > self._max_models:
            raise ModelError(f'Agent 已达到 {self._max_models} 轮模型调用上限，请缩小需求或补充口径')
        self._event('agent.model.started', {'iteration': self._models, 'summary': f'第 {self._models} 轮分析：根据已有证据选择下一步'})
        choice = kwargs.get('tool_choice')
        if choice == 'any':
            choice = 'required'
        if self._force_submission or self._models >= self._max_models or self._tools >= self._max_tools:
            choice = {'type': 'function', 'function': {'name': 'AgentSubmission'}}
            self._force_submission = False
        raw = await self._client.complete_messages(convert_to_openai_messages(messages), kwargs.get('tools', []), tool_choice=choice, parallel_tool_calls=False, on_request_event=self._event)
        proposed = raw.get('tool_calls') or []
        if len(proposed) > 1:
            # Some compatible providers ignore parallel_tool_calls=False.
            # Execute one observation-producing action and let the next model
            # turn choose again, instead of running a speculative batch.
            selected = next((c for c in proposed if c.get('function', {}).get('name') != 'AgentSubmission'), proposed[0])
            self._event('agent.actions.deferred', {'summary': f'本轮提出 {len(proposed)} 个动作，仅执行一个；其余需根据结果在后续轮次重新选择', 'selected_tool': selected.get('function', {}).get('name')})
            proposed = [selected]
        self._event('agent.model.completed', {'iteration': self._models, 'summary': f'模型返回 {len(proposed)} 个工具动作' if proposed else '模型返回了文本，需要检查结构化交付', 'finish_reason': raw.get('finish_reason')})
        if raw.get('finish_reason') == 'length':
            raise ModelError('模型输出被长度上限截断，无法形成完整产物；请缩小需求或调整模型输出限制')
        calls = []
        try:
            for item in proposed:
                name = item['function']['name']
                arguments = json.loads(item['function']['arguments'])
                if not isinstance(arguments, dict):
                    raise ValueError('tool arguments must be an object')
                calls.append({'name': name, 'args': arguments, 'id': item['id'], 'type': 'tool_call'})
                if name != 'AgentSubmission':
                    self._tools += 1
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError('模型返回的工具调用格式无效') from exc
        if self._tools > self._max_tools:
            raise ModelError(f'Agent 已达到 {self._max_tools} 次工具调用上限，尚未解决的问题需要补充说明')
        # Do not retain free-form model text when it is making tool calls.
        message = AIMessage(content='' if calls else str(raw.get('content') or ''), tool_calls=calls)
        return ChatResult(generations=[ChatGeneration(message=message)])


async def run_agent(client, system: str, context: dict, tools: list, memory: list,
                    event: Callable, validate: Callable | None = None,
                    submission_schema: type[AgentSubmission] = AgentSubmission,
                    max_models: int = 16, max_tools: int = 12) -> tuple[dict, list]:
    model = ToolCallingModel(client, event, max_models, max_tools)
    # The outer workflow owns durable checkpoints. Keep the transient tool
    # graph isolated; only JSON-safe, provider-filtered messages go into Run.
    agent = create_agent(model=model, tools=tools, system_prompt=system, checkpointer=False,
                         response_format=ToolStrategy(submission_schema, handle_errors=True))
    messages = messages_from_dict(memory) if memory else []
    messages.append(HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)))
    timeout_seconds = float(os.getenv('AGENT_TIMEOUT_SECONDS', '900'))
    request_stats = {'duration_ms': 0, 'timeouts': 0, 'retries': 0}
    original_event = model._event
    def observe(name, data):
        if name in {'agent.request.completed', 'agent.request.timeout'}:
            request_stats['duration_ms'] += data.get('duration_ms', 0)
        if name == 'agent.request.timeout':
            request_stats['timeouts'] += 1
        if name == 'agent.request.retry':
            request_stats['retries'] += 1
        original_event(name, data)
    model._event = observe
    try:
        async with asyncio.timeout(timeout_seconds):
            for attempt in range(3):
                result = await agent.ainvoke({'messages': messages}, config={'recursion_limit': 40})
                submission = result.get('structured_response')
                if not isinstance(submission, AgentSubmission):
                    if attempt == 2:
                        raise ModelError('模型连续 3 次未按结构化交付协议提交结果，已停止后续步骤；已调用过工具不代表最终交付一定合规')
                    event('agent.output.retry', {'summary': '模型尚未提交结构化结果，保留已取得的工具观察并要求重新提交', 'attempt': attempt + 1})
                    model._force_submission = True
                    messages = result.get('messages', messages) + [HumanMessage(content='请用 AgentSubmission 工具提交 payload，不要以普通文本结束。保留刚才取得的工具事实；有歧义就提交 clarifications，不能编造或跳过业务校验。')]
                    continue
                payload = submission.payload.model_dump(mode='json') if isinstance(submission.payload, BaseModel) else submission.payload
                try:
                    if validate:
                        validate(payload)
                    break
                except ValueError as exc:
                    event('agent.validation.failed', {'summary': f'方案校验未通过，正在修正：{str(exc)[:1000]}', 'attempt': attempt + 1})
                    if attempt == 2:
                        raise PlanValidationError(f'Agent 连续 3 次提交无效方案：{exc}') from exc
                    messages = result['messages'] + [HumanMessage(content=f'确定性校验未通过：{str(exc)[:1200]}。请查证后修正；业务信息不足则提出澄清，不能编造。')]
    except TimeoutError as exc:
        summary = (f'Agent 达到 {timeout_seconds:g} 秒总时限；已发起 {model._models} 轮模型调用、'
                   f'{model._tools} 次工具调用，模型请求超时 {request_stats["timeouts"]} 次、'
                   f'重试 {request_stats["retries"]} 次。已停止本次分析，尚未形成可执行结论。')
        event('agent.analysis.timeout', {'summary': summary, 'model_calls': model._models,
                                       'tool_calls': model._tools, **request_stats})
        raise ModelError(summary) from exc
    event('agent.analysis.completed', {'summary': '已提交结构化分析结果', 'model_calls': model._models, 'tool_calls': model._tools})
    return payload, messages_to_dict(result['messages'])
