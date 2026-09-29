# xuanpu（玄圃）

基于 [taifeng](https://github.com/LumoraX/taifeng) 的企业级 agent 平台：业务方只设计自己的业务场景，调度、恢复、隔离、存储都交给平台。

> 玄圃：昆仑山上众神居住的园子。泰逢是山海经里的神，玄圃是承载它们的地方。

> **状态**：工程骨架。只有业务包规范一个子包，尚无可用功能。

## 定位：像用数据库一样用 agent

| | 数据库 | xuanpu |
| --- | --- | --- |
| **业务方写什么** | 表结构、SQL | 业务包：skills、连接器声明、策略、评测用例 |
| **平台管什么** | 执行计划、事务、崩溃恢复、复制 | 会话调度、上下文压缩与缓存、挂起 / 恢复 / 崩溃接管、沙盒、存储、模型路由、计量审计 |
| **内核** | 存储引擎 | taifeng |
| **诊断入口** | EXPLAIN、慢查询日志 | 轨迹、回放、评测报告 |

## 分层

```
业务包（各租户提交，只含声明与进程外扩展）
        │
xuanpu（本仓）：运行时 · 控制面 · MCP 网关 · 控制台 · SDK
        │
taifeng-sandbox · taifeng-stores（内核适配包）
        │
taifeng（内核，零租户概念）
```

## 组件

| 组件 | 包 | 状态 |
| --- | --- | --- |
| 业务包规范 | `xuanpu-spec` | 骨架 |
| 本地开发 CLI | `xuanpu-cli` | 未开始 |
| 平台服务（运行时 + 控制面） | `xuanpu-server` | 未开始 |
| MCP 网关 | `xuanpu-mcp-gateway` | 未开始（先评估现成方案，见 ADR 0003） |
| 控制台 | `xuanpu-console` | 未开始 |
| 客户端 SDK | `xuanpu-sdk-py` / `xuanpu-sdk-js` | 未开始 |

## 开发

```bash
uv sync --all-packages
uv run pytest
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
```

## 许可

Apache-2.0
