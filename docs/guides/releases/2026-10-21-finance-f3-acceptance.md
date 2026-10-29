# 财务 F3 开发验收记录（2026-10-21）

F3通用接口与隔离闭环已在本地验收。范围包括发票查验、用途/税务入账确认、红字专用审批与红票关联、调整材料、实际归档读回和补包审计。基线75b2552，分支feat/finance-f3，复用xuanpu-finance-f0独立检出；本轮只本地提交，未推送、合并或部署生产。

## 分层证据

| 层级 | 实际结果与证据 | 适用边界 |
| --- | --- | --- |
| 源码与回归 | `FINANCE_TEST_DSN=<专用测试库> uv run pytest -q -ra`：**2286 passed, 3 skipped，282.28s**；基线2190 passed。全量[日志](evidence/finance-f3-2026-10-21/final-pytest.log) | 3项skip均为原有非财务PG专项测试；本轮财务PG测试全部实际运行 |
| 静态门禁 | `uv run ruff check . --select F,S108,I,TC`：All checks passed；`uv run mypy packages/*/src`：Success，258 source files；新增F3复杂度门禁通过，48个修改生产Python文件的中文docstring、文件≤800/函数≤80检查通过 | [ruff](evidence/finance-f3-2026-10-21/ruff.log)、[mypy](evidence/finance-f3-2026-10-21/mypy.log)、[源码检查](evidence/finance-f3-2026-10-21/source-audit.json) |
| API与Skill | `uv run xuanpu validate examples/finance`：finance 0.4.0-f3校验通过；58个API操作、7个Skill。20个新增操作的实际响应均通过严格发布schema | [TCP闭环](evidence/finance-f3-2026-10-21/http-contract-e2e.log)、[包校验](evidence/finance-f3-2026-10-21/pack-validation.log)；模型不能调用人工执行/关联/调整/恢复接口 |
| 人工与页面 | 真实平台cookie/CSRF→HumanContextSigner→财务验签→在线身份→专用批准→原经办执行。浏览器实际走完查验、预检阻断、独立审批、原计划恢复、人工确认、最终用途confirmed | [cookie测试](evidence/finance-f3-2026-10-21/cookie-csrf-e2e.log)、[浏览器记录](evidence/finance-f3-2026-10-21/browser-status.json)、[结果截图](evidence/finance-f3-2026-10-21/browser-final.png)；模型不可用，供应商模拟 |
| 独立数据与并发 | 专属PostgreSQL16 finance_test，每个测试随机schema迁移/清理，独立AES-GCM原件；真实PG锁等待验证F1提交/审批、F2发送与F3蓝票锁一致，外部红字ID跨票竞态一成一unknown且两份原回执保全 | 不访问平台数据库或平台对象存储凭据；归档自己的本地DB与原件卷与平台分离 |
| 归档内容与恢复 | 实际原件→查验截断回执unknown→原号GET恢复；用途/红字批准、实际负数红票原件导入查验关联、调整材料；实际ZIP上传及全部内容/manifest/seal版本字节与保留锁读回、只读审计、补包 | ZIP实际检查红票原件、调整材料原字节、税务/HITL历史和截断回执；simulation四性检测不外推正式标准 |
| 安装包与镜像 | finance wheel/sdist **2026.10.21.4**；console wheel/sdist **2026.10.21.5**；实际构建finance镜像，uid10001。89个财务、19个console文件与wheel逐字节匹配，89个财务文件同时与镜像匹配 | [制品hash](evidence/finance-f3-2026-10-21/artifacts.json)、[finance构建](evidence/finance-f3-2026-10-21/finance-build.log)、[console构建](evidence/finance-f3-2026-10-21/console-build.log)、[镜像构建](evidence/finance-f3-2026-10-21/image-build.log) |
| 最终镜像Compose | 实际镜像+专属DB/加密原件卷+真实HTTP，**两次重启**；首次发送后unknown，重启按原号查询通过且外部税务POST仍只有1次；双人HITL用途确认及归档读回；第二次重启后只读审计成功，审计外部POST=0；总外部POST=3、实际回执5份 | [Compose证据](evidence/finance-f3-2026-10-21/compose-smoke.json)；接口服务为隔离样例，独立项目和卷已删除 |
| 审查与清理 | 规格复核、网络/归档质量及最终质量复核均Approved；发现问题已回归。专用PG、HTTP、临时密钥、浏览器和Compose资源逐项清理 | [审查记录](evidence/finance-f3-2026-10-21/review-record.md)、[清理记录](evidence/finance-f3-2026-10-21/cleanup.json) |

最终镜像：`xuanpu-finance:2026.10.21.4`，ID `sha256:6f20ac8e1fc4a1a039233d0d7aa95bda919c489c834ab5775d2c37e61e61d0f6`。制品与代码通过实际字节比对，不能仅凭版本号推定一致。

## 关键验收与修复

当前政策TTL收紧立即约束原始通过时间；pending不会延长已有有效期。authority_revision按供应商/config/epoch划分，旧源回执保全且不覆盖当前源。发票内容与合规版本分离；部分红冲准确投影毛额、税额和具体行，影响F1/F2可用额度但不释放既有付款历史。

制度/岗位/渠道撤销后恢复不会复活旧授权。同一税务维度、父包和事件有在途占用。红票需要独立原件、通过查验和精确关联；材料received不表示申报成功。正式来源不能经导入、重复链接、补件或调整材料洗成simulation。

归档按真实sealed_at和固定anchor_rule核对最短保留，内容、manifest、seal逐对象版本读回，哈希/长度/媒体/锁/hold任一不符不能成功。空/截断/HTML等原始失败回执也保存原字节，零字节仅限受控raw receipt。补包读取当前目标版本并追加父hash及实际来源事件，原封存快照保持不可变。

确认卡金额/税额来自固定bindings，展示实际审批步骤；异步读取期间关闭确认，旧响应不能重新打开执行。所有执行仍由服务端重新验证当前身份、岗位、政策、版本和原批准摘要。

保留有意义的[TTL回归RED](evidence/finance-f3-2026-10-21/projection-red.log)、[确认金额RED](evidence/finance-f3-2026-10-21/plan-amount-red.log)、[补包页面版本RED](evidence/finance-f3-2026-10-21/supplement-ui-red.log)、[空回执RED](evidence/finance-f3-2026-10-21/raw-empty-red.log)。最终全量均GREEN。收尾时文档曾先引用尚未落盘的浏览器/清理证据，链接检查未通过；证据补齐后文档与样例154项通过，最后再次完整运行2286 passed、3 skipped，未跳过失败用例。Compose调试期间曾在验收脚本中误用读取主体与顶层状态字段，修正脚本后实际完整重跑；[脚本断言记录](evidence/finance-f3-2026-10-21/compose-harness-assertion-error.json)保留，最终结果以compose-smoke.json为准。

## 未验收范围

真实税务/红字供应商、官方格式/签名、真实WORM及保留/灾备证明、真实模型评测、企业微信/飞书报销和生产部署均未验收。simulation-archive-v1只验证隔离协议，正式能力保持关闭；模拟结果不会升级正式资格。列表当前优先最小权限正确性，大规模查询需后续SQL分页优化。

[实施计划](../../superpowers/plans/2026-10-21-finance-f3.md)、[使用指南](../finance-f3.md)、[设计](../../superpowers/specs/2026-10-21-finance-f3-design.md)。
