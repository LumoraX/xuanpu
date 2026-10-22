---
name: read
type: composite
entry: true
version: 1.0.0
tool_names: [api__wecom__self_profile]
description: 核对当前员工本人的企业微信基础资料
---

只使用 `api__wecom__self_profile`，参数必须为空。不要要求员工提供access_token、Secret、userid或他人账号。

缺少同意或绑定时，引导员工到“功能 → 企业连接”由本人确认；不得替员工同意。工具失败时如实说明，不能编造姓名、岗位或部门。返回后只核对实际存在的字段，明确供应商未提供的信息，不把资料读取解释为组织审批或业务完成。
