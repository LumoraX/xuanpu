"""执行追踪（R5-B）：一次请求一条 trace，每个执行单元一个 span。

span 只存元数据（身份、父子、状态、时间、用量），不存正文、工具参数与输出、推理内容或凭据。
详情从既有会话历史与审计按原权限读取。设计见
``docs/superpowers/specs/2026-11-14-r5b-trace-span-design.md``。
"""

from __future__ import annotations
