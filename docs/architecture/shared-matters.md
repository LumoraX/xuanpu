# 共享事项与固定材料候选

本稿记录 R3-C0/C1 候选的代码边界，尚未生产部署，尚未完成 C2 上下文、C3 员工入口及 C4 整体验收；不能据此宣布 R3-C 或 R3 完成。

共享事项独立于私人工作台。负责人明确邀请员工即可协作，不要求部门。部门成员及指定职责是可选授权条件，企微可见范围和玄圃管理员身份均不赋予共享正文读取权。最小员工选择目录只返回本企业启用员工的稳定编号和姓名，不返回联系方式、管理员角色或私人数据。

## 当前原生接口

| 方法及路径 | 含义 |
| --- | --- |
| POST `/v1/shared-matters` | 明确创建，title、可选content、request_id、confirmed=true |
| GET `/v1/shared-matters` | 先授权投影，再search、limit及cursor分页；total独立于页长 |
| GET `/v1/shared-matters/{id}` | 当前完整事项或限定资料投影 |
| PATCH `/v1/shared-matters/{id}` | expected_version编辑、关闭或归档；不是签收 |
| GET `/v1/shared-matters/{id}/members` | 完整事项成员可读，限定资料成员不可枚举 |
| POST `/v1/shared-matters/{id}/members` | 负责人明确邀请员工或可选部门 |
| POST `/v1/shared-matters/{id}/members/{member_id}/revoke` | 负责人撤销授权 |
| POST `/v1/shared-matters/{id}/transfer-owner` | 明确转交唯一负责人，不改变租户角色 |
| GET `/v1/shared-matters/{id}/events` | 当前完整事项成员读取历史，治理管理员无正文特权 |
| POST `/v1/shared-matters/{id}/materials` | 本人source_item_id/source_revision，可选material_id发布新版本 |
| POST `/v1/shared-matters/{id}/material-uploads` | 有界原生上传，query明确name、request_id、expected_version、confirmed |
| GET `/v1/shared-matters/{id}/publications/{publication_id}` | 补读pending/ready/failed/withdrawn状态，无对象路径 |
| GET `/v1/shared-matters/{id}/materials` | 当前可见固定版本与SHA、字节数 |
| GET `/v1/shared-matters/{id}/materials/{material_id}/content?revision=N` | 代理有界下载，读取后再次检查授权，禁止缓存及范围续传 |
| POST `/v1/shared-matters/{id}/materials/{material_id}/withdraw` | 撤回共享副本，保留历史及SHA |

所有写入使用稳定操作号，除创建外要求确切事项版本，并有原生确认。回执仅含实体编号、版本或状态；同号异参409，不自动换新版本覆盖。写接口沿用Cookie CSRF保护，无员工403、越界事项404。

## 权属及当前授权

每个事项有且只有一名真人owner。转交同事务撤销旧owner、撤销新owner原直接成员授权并新建唯一owner。负责人停用时业务写入阻塞，不自动改派；恢复本人身份保留原权属。普通协作授权绑定身份代次，停用再启用不恢复旧授权；须由负责人重新明确邀请。无权管理员不能接管或读正文。

授权 scope=full 或 packet。packet只能只读，必须独立标题，正文只能是授权时明确选择的冻结副本，材料限定精确revision编号集合；不暴露原事项名称、成员数量、负责人和其他材料，搜索与统计也只使用该投影。部门授权检查当前任职、有效部门、当前职责及员工身份代次。

协调事务保存事项、授权、成员、正文历史、固定材料、发布意图、回执及事件；PG复用同一连接，复合外键包含租户。内存执行相同外键、版本及不可变历史约束。治理审计和outbox仅存来源编号，正文历史保存在受共享ACL保护的领域中。当前仍是试点租户快照方案，不宣称大企业吞吐。

## 发布、下载和恢复

先提交固定意图和空间预留，再在事务外读取/复制对象、验SHA，最后重新检查当前权限、身份代次、事项版本及原文件版本后公开ready revision。预留与公开各递增事项版本。上传返回202和固定pending回执，通过publication接口获取当前状态；重试不会改写原回执或重复产生副本。复制前后原件改版会要求重新确认；已公开的旧共享副本不会随原件改版变化。

单文件默认20MiB，空间默认1GiB，历史和撤回材料仍计入真实副本用量。未知大小不填0。失败意图和孤立对象分别记录并清理，租约到期后由既有单实例持有权内的恢复扫描器补读。请求取消窗口已有双后端测试；实际进程SIGKILL、冷启动、慢流撤回仍须独立验收，不能用该测试替代。

失败或撤回的意图保留永久GC墓碑，按100条循环重扫，防止过期writer在一次删除后才落盘。撤回但未清理的副本仍占预留；对象调用未明确结束或存在过期writer时，即使暂时删除成功也保守持有预留，避免把未知写入当作已经腾空。进程代次与新实例持有权不能证明旧HTTP线程或对象服务端写入已终止，因此不据此自动释放未知预留；此类空间需另行运维核定。服务关闭会取消并等待在途发布，墓碑仍持续兜底回收迟到对象。该机制未在本稿冒充实际SIGKILL验收。

下载使用当前授权、ready revision和真实字节SHA。原文件删除与共享撤回分别操作。撤回阻断新请求及续传，不宣称已交付字节可以追回。材料派生来源结构和上下文依赖表已经预建，但面向模型的来源写入与全路径保护尚待C2启用；当前不提供共享内容自动进入模型或R2沙盒的入口。

## 迁移与灰度

0017一次建立事项、成员、grant、grant精确版本关联、材料及revision、发布意图、固定来源关联、受保护上下文、来源依赖、消息sources空默认值及站内通知来源。不得回改已发布SQL。候选默认shared_matters.enabled=false、tenants=[]，关闭后保留已有读取、归档、授权撤销及材料撤回。生产发布还需上下文保护、员工入口、独立复核和分层验收；本稿不构成这些工作的通过记录。
