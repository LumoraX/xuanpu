"""进程外脚本执行器的宿主观察接口，不依赖内核私有实现或事件节选。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import taifeng
from taifeng_sandbox import python_script_executor, shell_script_executor

if TYPE_CHECKING:
    from xuanpu_server.runtime.sandbox import SandboxHandle

ScriptObserver = Callable[[taifeng.ScriptInvocation, taifeng.ScriptResult | None], Awaitable[None]]


class ObservedScriptExecutor:
    """观察实际执行结果，先持久记录启动，再保存完整有界回执。"""

    def __init__(self, inner: taifeng.ScriptExecutor, observer: ScriptObserver) -> None:
        """执行位置仍由原适配器决定，观察者不能改变输入或返回结果。"""
        self.inner, self.observer = inner, observer

    async def execute(self, invocation: taifeng.ScriptInvocation) -> taifeng.ScriptResult:
        """真实结果先交给宿主，再返回模型；存储失败时不产生已核验声明。"""
        await self.observer(invocation, None)
        result = await self.inner.execute(invocation)
        await self.observer(invocation, result)
        return result


def script_executors(handle: SandboxHandle, observer: ScriptObserver | None = None) -> dict[str, taifeng.ScriptExecutor]:
    """所有脚本沿同一沙盒执行，可选宿主回执观察不改变隔离与取消语义。"""
    executors: dict[str, taifeng.ScriptExecutor] = {
        'shell':shell_script_executor(handle.executor, env=handle.process_env()),
        'python':python_script_executor(handle.executor, env=handle.process_env()),
    }
    if observer is not None:
        return {language:ObservedScriptExecutor(executor,observer) for language,executor in executors.items()}
    return executors
