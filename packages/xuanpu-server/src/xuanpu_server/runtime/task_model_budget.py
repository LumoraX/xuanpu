"""公开模型协议包装器：每次实际stream先持久预留，子skill同样覆盖。"""
from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.budget_store import BudgetStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import taifeng

    from xuanpu_server.work_tasks.execution_context import ExecutionBinding


def _check_legacy_state(message: dict[str, Any]) -> None:
    """兼容消息中的签名和附加字段仍属于不透明provider状态。"""
    if message.get('reasoning_state'):
        raise ConflictError('block_budget：兼容消息含未验收的不透明状态')
    for call in message.get('tool_calls') or []:
        function = call.get('function', {})
        if (set(call) - {'id','type','function'} or set(function) - {'name','arguments'}):
            raise ConflictError('block_budget：工具调用含未验收的provider附加状态')


def text_request_bound(value: dict[str, Any]) -> int:
    """只对已验收的纯文本协议计算保守UTF8上界；不推断图片或隐藏引用。"""
    def check_content(content: Any) -> None:
        """内容段须为明确文本，不能只用URL字符串长度估计多模态成本。"""
        if content is None or isinstance(content,str):
            return
        if not isinstance(content,list) or any(not isinstance(part,dict) or
                part.get('type') not in {'text','input_text','output_text'} for part in content):
            raise ConflictError('block_budget：该输入类型没有可核定上界')

    for message in value.get('messages',[]):
        _check_legacy_state(message)
        check_content(message.get('content'))
    for item in value.get('input_items',[]):
        if item.get('type') not in {'message','function_call','function_call_output'}:
            raise ConflictError('block_budget：隐藏或未知输入项尚未验收')
        check_content(item.get('content'))
        if 'output' in item:
            check_content(item['output'])
    return len(json.dumps(value,ensure_ascii=False).encode('utf-8')) * 4 + 4096


class TaskModelClient:
    """任务专用计量，普通会话保持原客户端。"""

    def __init__(self, inner: taifeng.ModelClient, binding: ExecutionBinding, *,
                 verified_text_bound: bool = False, max_output_tokens: int = 4096) -> None:
        """仅经过输入类型与网关输出上限验收的模型可启用。"""
        self.inner, self.binding = inner, binding
        self.verified_text_bound, self.max_output_tokens = verified_text_bound, max_output_tokens
        capabilities = getattr(inner, 'capabilities', None)
        if capabilities is not None:
            self.capabilities = capabilities

    def session(self, *, cancel: taifeng.CancellationToken, model: str | None = None) -> Any:
        """所有内核采样和内部重试均走相同持久预算。"""
        return _TaskSession(self, self.inner.session(cancel=cancel, model=model))


class _TaskSession:
    """一次turn的会话，每次stream有独立且不可重复发送的request_id。"""

    def __init__(self, client: TaskModelClient, inner: Any) -> None:
        """保留公开协议客户端，不访问内核内部恢复或事件类型。"""
        self.client, self.inner = client, inner

    async def __aenter__(self) -> _TaskSession:
        """进入原客户端会话。"""
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """退出原客户端会话。"""
        await self.inner.__aexit__(*exc)

    async def stream(self, request: taifeng.ApiRequest) -> AsyncIterator[Any]:
        """平台记住预算阻塞，不依赖内核把异常文字原样传回。"""
        try:
            async for event in self._stream(request):yield event
        except ConflictError as error:
            if error.message.startswith('block_budget'):self.client.binding.stop_reason='block_budget'
            raise

    async def _stream(self, request: taifeng.ApiRequest) -> AsyncIterator[Any]:
        """未知响应保留上界；只有真实completed用量才核销。"""
        client, binding = self.client, self.client.binding
        if binding.stop_reason=='block_budget':
            raise ConflictError('block_budget：本次执行已因额度不足暂停')
        if not client.verified_text_bound:
            raise ConflictError('block_budget：该模型输入上界和输出限制尚未验收')
        value = request.model_dump(mode='json')
        requested = request.max_output_tokens
        if requested is not None and (type(requested) is not int or requested < 1):
            raise ConflictError('block_budget：模型输出上限必须为正整数')
        maximum = min(requested or client.max_output_tokens,client.max_output_tokens)
        # 首期预算只验收纯文本；显式关闭默认隐藏推理，返回不透明状态仍会在下次采样前阻塞。
        capped = request.model_copy(update={'max_output_tokens':maximum, 'reasoning_effort':'none'})
        value = capped.model_dump(mode='json')
        upper = text_request_bound(value) + maximum
        budget = BudgetStore(binding.store,max_task_tokens=binding.max_task_tokens)
        identifier = 'model_' + secrets.token_hex(16)
        await budget.reserve(binding.lease, identifier, upper, now=binding.now())
        async for event in self.inner.stream(capped):
            if event.kind == 'completed':
                usage = event.data.get('usage')
                if (isinstance(usage, dict) and type(usage.get('input_tokens')) is int
                        and type(usage.get('output_tokens')) is int):
                    await budget.settle(binding.lease, identifier,
                        input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'], now=binding.now())
                    if usage['input_tokens'] + usage['output_tokens'] > upper or usage['output_tokens'] > (capped.max_output_tokens or 0):
                        raise ConflictError('block_budget：网关未遵守已验收的模型上界')
            yield event
