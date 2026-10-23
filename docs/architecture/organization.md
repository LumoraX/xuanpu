# 组织、来源权威与协调事务

当前为R3-B候选代码，尚未部署本候选，未签收真实目录导入或R3整体交付。

玄圃持有部门、岗位、稳定员工标识、任职与业务职责。企微是可替换只读来源；企微管理员不自动成为玄圃管理员。组织管理不授予个人正文读取权。私人工作台及R2任务维持既有归属。

## 接口

| 方法与路径 | 行为 |
| --- | --- |
| GET `/v1/organization/people` | 本企业启用员工的最小选择目录，仅姓名和稳定编号，不要求部门 |
| GET `/v1/organization/me` | 本人当前有效部门、任职与职责；无员工身份403 |
| GET `/v1/organization/directory` | 当前范围最小任职目录，含姓名、部门、岗位，不含电话邮箱 |
| GET/POST `/v1/admin/organization/units` | 部门列表／创建 |
| GET/POST `/v1/admin/organization/positions` | 岗位列表／创建 |
| GET/POST `/v1/admin/organization/memberships` | 任职列表／明确授予 |
| GET/POST `/v1/admin/organization/roles` | 业务职责列表／明确授予 |
| GET/PATCH `/v1/admin/organization/{collection}/{id}` | 读取／expected_version变更 |
| GET `/v1/admin/organization/authority` | 企业字段来源配置 |
| POST `/v1/admin/organization/authority/previews` | 冻结旧规则版本及影响数量，生成来源切换预览 |
| POST `/v1/admin/organization/authority/previews/{id}/apply` | 明确确认当前预览版本 |
| POST `/v1/admin/organization/import-previews` | 引用connection_id采集，只生成差异 |
| GET `/v1/admin/organization/import-previews/{id}` | 范围、完整性、逐页摘要、错误和冲突 |
| POST `/v1/admin/organization/import-previews/{id}/apply` | source_revision、expected_version和明确selected_changes，每批1–100条 |

员工目录people默认50、上限100，按稳定员工编号翻页，total为本企业启用员工总数，独立于当前页；不要求部门任职。任职目录仍遵守组织可见范围。

写操作使用request_id，同键同参返回原结果，异参或旧版本409；Cookie写入复用全局CSRF保护。列表默认50、最大100，以创建时间和ID组成稳定游标；total先按权限过滤再独立计算。

租户管理员可以配置组织。org_manager只能管理明确授予的部门子树，unit_operator和unit_approver不提供组织管理权。企业级来源切换和外部目录采集当前限定租户管理员，部门职责不扩大外部读取范围。

## 提交与隔离

协调层复用现有PG连接池，固定租户行→排序员工行→领域实体→outbox锁序。组织仓储接收工作单元，姓名更新登记待提交写集；实体、来源映射、姓名、幂等回执、事件及确认使用同一连接提交。事务默认5秒，覆盖池等待、锁等待和执行；异常、超时和取消均回滚。外部采集不持有协调锁。

内存后端深拷贝状态，提交前再次核对身份版本。姓名写集的同步提交区间无await切点，异常恢复姓名与版本。PG部门、岗位、员工外键包含租户。部门不可循环、超过16层或同级重名；停用ID不能复用。职责只允许org_manager/unit_operator/unit_approver，不允许供应商字段授予tenant_admin。

## 来源与差异

字段来源为local或具体安装；切换需预览影响数量，确认时复核旧版本和数量。已有身份未配置显式规则时保持旧登录刷新兼容行为；local姓名不会被外部登录覆盖，登录时间仍可刷新。企微登录保留本地姓名，不JIT建人。

采集保存起止时间、逐页摘要和明确错误。应用可见范围始终complete=false；某页失败不算空范围成功。未出现的员工不会据此停用、删除或缩权。令牌刷新和每页授权复核沿用R3-A出口。

应用前用固定委托引用重新采集，摘要变化拒绝。差异绑定本地版本、字段权威版本及固定数据；客户端不能改主体和内容。父部门先于子部门，再提交任职。姓名以身份代次、资料版本及旧姓名CAS提交。每批单独留回执，不宣称多批全局回滚。未知外部身份只列明确冲突，不按同名合并员工。目录不提供可核定离职状态，供应商停用保持未核定；人工停用仍走identity流水线。

## 即时撤权与恢复

身份表增加lifecycle_epoch与profile_version，旧行默认1。实际停用和重新启用均递增身份代次。任职、职责记录授予代次；授权按当前身份与代次检查，重新启用不恢复旧职责。

已撤销的职责记录不能重新激活；重新明确授予须新建记录，并再次检查转授组织管理职责的限制。旧操作号仍可重放原回执，不能把PATCH旧记录当成绕过新授权的入口。

停用身份事务持久生成稳定user_disabled事件，提交后才执行组织收尾。消费者逐事件确认，不以最大序号为水位。启动及周期扫描补齐漏收尾；乱序回调只撤销旧代次任职和职责，不覆盖重新明确授予的新代次。role_revoked、membership_changed保留变更前后快照与稳定事件ID。

当前为租户快照及串行试点设计，尚未声明大企业吞吐或后续共享事项、交接审批验收。
