"""F15真实模型的持久资源预算、原始证据及生产客户端透明包装。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

OUTPUT_LIMIT = 4096
APPROVALS = frozenset({'api__finance__createExpenseClaim', 'api__finance__updateExpenseClaim',
                      'api__finance__previewFinanceAction'})


class HarnessFailure(RuntimeError):
    """可落盘且不可转为成功的验收停止原因。"""


def allowed_approval(target: str, arguments: dict[str, Any] | None = None) -> bool:
    """仅确认草稿写与预检，精确匹配实际平台工具名。"""
    if target == 'api__finance__previewFinanceAction':
        body = (arguments or {}).get('body', {})
        return body.get('target_type') == 'expense_claim' and body.get('action') in {
            'submit_claim', 'withdraw', 'approve', 'reject'}
    return target in APPROVALS


def encoded(value: Any) -> bytes:
    """使用真实UTF8序列化统计资源字节，不冒称token估计。"""
    return json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')


class Evidence:
    """保留原始调用与独立判定，失败状态单向不可逆。"""

    def __init__(self, directory: Path, secrets: list[str]) -> None:
        """建立仅本轮使用的证据目录与精确秘密替换表。"""
        self.directory, self.secrets = directory, secrets
        directory.mkdir(parents=True, exist_ok=True)
        self.result: dict[str, Any] = {'status': 'running', 'checks': []}

    def save(self, name: str, value: Any) -> None:
        """只写脱敏后的可解析JSON证据。"""
        text = encoded(value).decode()
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, '[REDACTED]')
        (self.directory / name).write_text(text + '\n')

    def fail(self, reason: str) -> None:
        """首个失败必须保留，后续清理不能覆盖。"""
        self.result['status'] = 'failed'
        self.result.setdefault('failure', reason)
        self.save('result.json', self.result)

    def check(self, name: str, condition: bool, actual: Any) -> None:
        """保存业务事实后才判断，不以回复或HTTP200替代。"""
        self.result['checks'].append({'name': name, 'passed': condition, 'actual': actual})
        self.save('result.json', self.result)
        if not condition:
            self.fail(name)
            raise HarnessFailure(name)

    def complete(self, *, partial: bool = False) -> None:
        """只有从未失败的旅程才允许成功。"""
        if self.result['status'] != 'failed':
            self.result['status'] = 'partial_passed' if partial else 'passed'
        if partial:
            self.result['counts_as_complete_journey'] = False
        self.save('result.json', self.result)


class Budget:
    """跨失败、基线与最终两轮共用的有限账本，初始包含真实探针。"""

    def __init__(self, path: Path, journey: str, probe: dict[str, Any] | None = None,
                 *, max_streams: int = 120) -> None:
        """恢复现有账本，禁止重置已消费资源。"""
        if type(max_streams) is not int or not 1 <= max_streams <= 120:
            raise ValueError('INVALID_JOURNEY_STREAM_LIMIT')
        self.path, self.journey, self.max_streams = path, journey, max_streams
        self.data = json.loads(path.read_text()) if path.exists() else {
            'attempts': 1, 'completed': 1, 'input_tokens': 21, 'output_tokens': 11,
            'request_bytes': 0, 'event_bytes': 0, 'seconds': 0.0, 'halted': None,
            'probe': {'input_tokens': 21, 'output_tokens': 11}, 'journeys': {},
        }
        if probe is not None and not path.exists():
            self.data['request_bytes'] = len(encoded(probe['request']))
            self.data['event_bytes'] = sum(len(encoded(e)) for e in probe['events'])
            self.data['seconds'] = probe['seconds']
        self._last_wall: float | None = None
        self.local = self.data['journeys'].setdefault(journey, {'attempts': 0, 'seconds': 0.0})
        if self.local.get('stream_limit', max_streams) != max_streams:
            raise ValueError('JOURNEY_LIMIT_MISMATCH')
        self.local['stream_limit'] = max_streams
        self.save()

    def save(self) -> None:
        """原子保存每次尝试和失败，避免异常退出丢失累计值。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_bytes(encoded(self.data))
        temporary.replace(self.path)

    def checkpoint(self) -> None:
        """以单调时钟累计模型阶段墙钟，包含模型轮次之间的工具时间。"""
        now = time.monotonic()
        if self._last_wall is not None:
            elapsed = now - self._last_wall
            self.data['seconds'] += elapsed
            self.local['seconds'] += elapsed
        self._last_wall = now
        self.save()

    def halt(self, reason: str) -> None:
        """永久停止自动消费，保留精确门槛名称。"""
        if not self.data['halted']:
            self.data['halted'] = reason
        self.save()
        raise HarnessFailure(self.data['halted'])

    def begin(self, request_bytes: int) -> None:
        """派发前检查硬门槛并预留完整4096输出token。"""
        self.checkpoint()
        if self.data['halted']:
            raise HarnessFailure('BUDGET_HALTED:' + self.data['halted'])
        checks = {'STREAM_LIMIT': self.data['attempts'] >= 240,
                  'JOURNEY_STREAM_LIMIT': self.local['attempts'] >= self.max_streams,
                  'TIME_LIMIT': self.data['seconds'] >= 5400 or self.local['seconds'] >= 1200,
                  'INPUT_TOKENS': self.data['input_tokens'] >= 3_000_000,
                  'OUTPUT_TOKENS': self.data['output_tokens'] + OUTPUT_LIMIT > 150_000,
                  'REQUEST_BYTES': request_bytes > 256 * 1024 or
                  self.data['request_bytes'] + request_bytes > 32 * 1024 * 1024}
        for reason, exceeded in checks.items():
            if exceeded:
                self.halt(reason)
        self.data['attempts'] += 1
        self.local['attempts'] += 1
        self.data['request_bytes'] += request_bytes
        self.save()

    def events(self, size: int) -> None:
        """逐事件累计原始字节，超限立刻停止。"""
        self.data['event_bytes'] += size
        self.save()
        if self.data['event_bytes'] > 128 * 1024 * 1024:
            self.halt('EVENT_BYTES')

    def finish(self, usage: dict[str, Any] | None, seconds: float) -> None:
        """实际usage结算；缺失或错误usage阻止任何后续模型调用。"""
        self.checkpoint()
        self.data['stream_seconds'] = self.data.get('stream_seconds', 0.0) + seconds
        if usage is None or not all(isinstance(usage.get(k), int) and usage[k] >= 0
                                    for k in ('input_tokens', 'output_tokens')):
            self.halt('USAGE_MISSING')
        for key in ('input_tokens', 'output_tokens'):
            self.data[key] += usage[key]
        self.data['completed'] += 1
        self.save()
        if self.data['input_tokens'] > 3_000_000 or self.data['output_tokens'] > 150_000:
            self.halt('TOKEN_LIMIT_AFTER_INFLIGHT')
        if self.data['seconds'] > 5400 or self.local['seconds'] > 1200:
            self.halt('TIME_LIMIT')


class RecordingSession:
    """只收紧输出上限并记录，模型请求和响应保持真实。"""

    def __init__(self, inner: Any, recorder: RecordingFactory) -> None:
        """保存生产会话和本轮记录器。"""
        self.inner, self.recorder = inner, recorder

    async def __aenter__(self) -> RecordingSession:
        """进入实际生产会话。"""
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """退出实际生产会话。"""
        await self.inner.__aexit__(*exc)

    async def stream(self, request: Any):
        """保存每次原始请求、事件及失败；不自动重试或补写输出。"""
        async with self.recorder.lock:
            request = request.model_copy(update={'max_output_tokens': min(
                request.max_output_tokens or OUTPUT_LIMIT, OUTPUT_LIMIT)})
            raw = request.model_dump(mode='json')
            budget, evidence = self.recorder.budget, self.recorder.evidence
            budget.begin(len(encoded(raw)))
            number = budget.data['attempts']
            trace: dict[str, Any] = {'request': raw, 'events': [], 'phase': self.recorder.phase,
                                      'requested_tier_or_model': request.model,
                                      'effective_model': self.recorder.effective_models.get(request.model),
                                      'started_at': time.time(),
                                      'actor_id': self.recorder.actor_id,
                                      'session_id': self.recorder.session_id}
            name = f'model-{number:03d}.json'
            evidence.save(name, trace)
            usage, started = None, time.monotonic()
            try:
                async with asyncio.timeout(min(90, 1200 - budget.local['seconds'],
                                               5400 - budget.data['seconds'])):
                    async for event in self.inner.stream(request):
                        item = event.model_dump(mode='json')
                        trace['events'].append(item)
                        evidence.save(name, trace)
                        budget.events(len(encoded(item)))
                        if event.kind == 'completed':
                            usage = event.data.get('usage')
                        if event.kind == 'error':
                            raise HarnessFailure('MODEL_EVENT_ERROR')
                        yield event
            except BaseException as error:
                trace['failure'] = type(error).__name__ + ':' + str(error)
                raise
            finally:
                evidence.save(name, trace)
                budget.finish(usage, time.monotonic() - started)


class RecordingClient:
    """转交生产客户端能力及会话，不编造模型响应。"""

    def __init__(self, inner: Any, recorder: RecordingFactory) -> None:
        """保留生产能力声明。"""
        self.inner, self.recorder = inner, recorder
        if hasattr(inner, 'capabilities'):
            self.capabilities = inner.capabilities

    def session(self, **kwargs: Any) -> RecordingSession:
        """记录每个真实stream，包括路由和后台调用。"""
        return RecordingSession(self.inner.session(**kwargs), self.recorder)


class RecordingFactory:
    """生产Gateway工厂的有限资源透明包装，不替换运行时。"""

    def __init__(self, inner: Any, budget: Budget, evidence: Evidence,
                 effective_models: dict[str, str] | None = None) -> None:
        """注入唯一账本和证据输出。"""
        self.inner, self.budget, self.evidence = inner, budget, evidence
        self.effective_models = effective_models or {}
        self.actor_id: str | None = None
        self.session_id: str | None = None
        self.phase = 'bootstrap'
        self.lock = asyncio.Lock()

    async def create(self, tenant_id: str, tier: str) -> RecordingClient:
        """真实工厂完成档位和网关模型选择。"""
        return RecordingClient(await self.inner.create(tenant_id, tier), self)
