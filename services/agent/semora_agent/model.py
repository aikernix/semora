from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx


class ModelError(RuntimeError):
    pass


class PlanValidationError(ModelError):
    """Keep diagnostic details for callers, with a separate user-facing message."""
    public_message = '模型暂未能将已确认口径完整落实为可执行方案，已停止本次分析。请重新分析，详细原因可在分析记录中查看。'


class StructuredModel:
    """OpenAI-compatible structured model client; hidden reasoning is never requested or stored."""

    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        self.mode = os.getenv("LLM_MODE", "optional").lower()
        self.timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
        self.request_retries = max(0, min(2, int(os.getenv('LLM_REQUEST_RETRIES', '1'))))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def required(self) -> bool:
        return self.mode == "required"

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        if not self.configured:
            raise ModelError("LLM 未配置")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"模型调用失败：{type(exc).__name__}") from exc
        if not isinstance(content, str):
            raise ModelError("模型未返回 JSON 文本")
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelError("模型返回的 JSON 无法解析") from exc
        if not isinstance(value, dict):
            raise ModelError("模型返回必须是 JSON 对象")
        return value

    async def complete_messages(self, messages: list[dict], tools: list[dict], **kwargs: Any) -> dict:
        """Native tool calling; do not request or forward model-private reasoning."""
        if not self.configured:
            raise ModelError('LLM 未配置')
        body = {'model': self.model, 'temperature': 0, 'messages': messages, 'tools': tools}
        if kwargs.get('tool_choice') is not None:
            body['tool_choice'] = kwargs['tool_choice']
        if kwargs.get('parallel_tool_calls') is not None:
            body['parallel_tool_calls'] = kwargs['parallel_tool_calls']
        event = kwargs.get('on_request_event') or (lambda *args: None)
        metadata = {'model': self.model, 'message_count': len(messages), 'tool_count': len(tools),
                    'request_bytes': len(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))}
        for attempt in range(self.request_retries + 1):
            started = time.monotonic()
            event('agent.request.started', {**metadata, 'attempt': attempt + 1, 'summary': f'正在请求模型，第 {attempt + 1} 次尝试；{len(messages)} 条消息，约 {metadata["request_bytes"]} 字节'})
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(self.endpoint, headers={'Authorization': f'Bearer {self.api_key}'}, json=body)
                    response.raise_for_status()
                    choice = response.json()['choices'][0]
                    message = choice['message']
                if not isinstance(message, dict):
                    raise ValueError('invalid message')
                event('agent.request.completed', {'attempt': attempt + 1, 'duration_ms': round((time.monotonic()-started)*1000), 'summary': '模型请求已返回'})
                return {'content': message.get('content'), 'tool_calls': message.get('tool_calls') or [], 'finish_reason': choice.get('finish_reason')}
            except httpx.ReadTimeout as exc:
                event('agent.request.timeout', {**metadata, 'attempt': attempt + 1, 'duration_ms': round((time.monotonic()-started)*1000), 'summary': f'等待模型响应超过 {self.timeout_seconds:g} 秒'})
                if attempt < self.request_retries:
                    event('agent.request.retry', {'attempt': attempt + 2, 'summary': f'模型响应超时，正在进行第 {attempt + 1}/{self.request_retries} 次重试；本次请求尚未产生工具执行'})
                    continue
                raise ModelError(f'模型响应读取超时（ReadTimeout）：单次等待上限 {self.timeout_seconds:g} 秒，已重试 {self.request_retries} 次；请稍后重试或检查模型服务响应速度') from exc
            except httpx.TimeoutException as exc:
                raise ModelError(f'模型请求超时（{type(exc).__name__}），请检查模型服务连接及网络') from exc
            except httpx.HTTPStatusError as exc:
                raise ModelError(f'模型服务返回 HTTP {exc.response.status_code}；请检查服务状态、认证、额度和接口参数') from exc
            except httpx.RequestError as exc:
                raise ModelError(f'模型网络请求失败（{type(exc).__name__}），请检查网络与模型服务连接') from exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ModelError(f'模型响应格式无效（{type(exc).__name__}），未取得有效的消息结构') from exc


structured_model = StructuredModel()
