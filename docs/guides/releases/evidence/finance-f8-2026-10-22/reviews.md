# F8 独立审查记录

本记录整理本轮规格和质量审查的实际结论，不替代原始测试输出。

- f8_gap_audit：设计Approved；后端本人权限/分页快照/逐项回执责任链/部分成功与取消可选Approved。
- f8_quality：后端、74 API及发布契约与Skill边界Approved；7项网关测试和18项早期harness复核通过。
- 前端首轮规格发现放弃线索旧确认仍能POST、坏标识限制宽于真实契约；原实现者补红绿并修复，规格复审Approved。
- 前端质量发现跨窗口旧query可能删除新pointer；实现者绑定完整lineage核对和清理，规格及质量复审Approved。
- 实际浏览器发现原下载名被ASCII占位覆盖、旧blob链接在撤销后仍可点、后台模型绕过runtimes.get；分别补网关、链接生命周期与平台构造前严格拒绝工厂。规格和质量复审均Approved，后者22项harness独立复核通过。
- 最后下载生命周期质量审查：原Set和新Map同步清理，旧锚移除href/download、标记失效，新读取维持权限/版本/字节SHA；行为与静态12 passed，Source Approved。
- 最终证据质量审查：逐文件独立比对finance129/console23的wheel/sdist、20文件业务ZIP和镜像UID10001/129源码；核对浏览器PNG/DOM、6版本3human决定的DB快照、原文及附件映射、0外部操作与0模型派发/2被拒后台尝试。
- 下载独立复核：3份浏览器下载与确定性重建且匹配已记录原始SHA的测试PDF逐字节一致，pypdf可读取。明确原始临时fixture路径已经清理。
- 最终测试复核：全量2577 cases=2574 passed+3既有skip、0failure/error；quick147 passed、0skip/failure/error。报告如实区分最后下载控件定向回归与先前全量启动源码。
- 最终清理独立复核：本轮容器不存在、3端口关闭、DSN/ready和本轮下载不存在，原检出xuanpu干净，保留制品存在。最终证据/制品Approved，无剩余审查阻断。

批准范围为本地开发与隔离验收；未扩大到生产人员/制度、真实模型、生产负载、官方税务/档案或外部支付。审查者未代替根执行本地Git提交；提交事实由分支git log单独核对。
