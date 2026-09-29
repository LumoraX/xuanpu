# 架构总览（活文档）

> 本目录只描述**当前生效的设计**；为什么这么定见 [../decisions/](../decisions/README.md)。

## 现状

工程骨架：uv workspace 与 CI 门禁就位，只有 `xuanpu-spec` 一个子包，schema 尚未定义。

## 分层与依赖方向

```
业务包（声明 + 进程外扩展）
        │ 提交
xuanpu
  ├─ xuanpu-spec          业务包规范（对外契约，不依赖其他组件）
  ├─ xuanpu-cli           本地开发：校验 / 本地运行 / 评测 / 部署
  ├─ xuanpu-server        运行时（引擎承载、会话放置、执行存活、事件流、HITL 待办）
  │                       + 控制面（租户、业务包版本、策略、配额、审计）
  ├─ xuanpu-mcp-gateway   业务扩展唯一通道（凭据注入、出网控制、副作用分类、审计）
  ├─ xuanpu-console       业务视角控制台
  └─ xuanpu-sdk-*         业务应用调用平台的客户端
        │ 只依赖稳定层
taifeng-sandbox · taifeng-stores
        │
taifeng
```

依赖只能自上而下；下层永远不 import 上层，也不出现上层概念。

## 租户隔离

taifeng 不知道租户。xuanpu 在组装每个引擎时注入按租户隔离的资源：存储（库 / schema / 前缀）、沙盒执行器、MCP 凭据、策略。租户边界完全由平台保证。

## 组件状态

| 组件 | 状态 |
| --- | --- |
| `xuanpu-spec` | 骨架 |
| `xuanpu-cli` | 未开始 |
| `xuanpu-server` | 未开始 |
| `xuanpu-mcp-gateway` | 未开始（先评估现成方案） |
| `xuanpu-console` | 未开始 |
| `xuanpu-sdk-*` | 未开始 |
