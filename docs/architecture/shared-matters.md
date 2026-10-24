# 共享事项与固定材料

R3-C已随2026.10.24.1.r3-candidate在acme灰度部署，原生员工入口、SDK/CLI、来源通知已接入。合成浏览器、真实模型和隔离进程验收分别留证；真实员工生产试点尚待本人操作，不能据此宣布R3整体完成。

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
| POST `/v1/shared-matters/{id}/material-texts` | 原生确认发布本人受保护助手消息的固定文本及来源，不接受替换正文 |
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

单文件默认20MiB，空间默认1GiB，历史和撤回材料仍计入真实副本用量。未知大小不填0。失败意图和孤立对象分别记录并清理，租约到期后由既有单实例持有权内的恢复扫描器补读。请求取消窗口已有双后端测试；隔离PG验收另覆盖真实子进程在LocalObjectStore写入后、公开revision提交前SIGKILL，再由新恢复器公开唯一副本。独立解释器从PG读取撤权上下文被拒绝；对象慢读取期间撤权则在HTTP整包发送前拒绝。这些不证明对象服务断电持久性、已开始网络流的中途取消或生产恢复。

失败或撤回的意图保留永久GC墓碑，按100条循环重扫，防止过期writer在一次删除后才落盘。撤回但未清理的副本仍占预留；对象调用未明确结束或存在过期writer时，即使暂时删除成功也保守持有预留，避免把未知写入当作已经腾空。进程代次与新实例持有权不能证明旧HTTP线程或对象服务端写入已终止，因此不据此自动释放未知预留；此类空间需另行运维核定。服务关闭会取消并等待在途发布，墓碑仍持续兜底回收迟到对象。该机制未在本稿冒充实际SIGKILL验收。

下载使用当前授权、ready revision和真实字节SHA。原文件删除与共享撤回分别操作。撤回阻断新请求及续传，不宣称已交付字节可以追回。材料及对话均不能自动进入R2沙盒。

## 来源选择与对话

`POST /v1/shared-contexts`要求request_id、confirmed=true、事项expected_version、shared_id、kind、include_content及最多8个material_revision_ids。员工与来源足迹由服务器确定。kind为assistant_conversation时用于独立共享主对话；runtime_session时必须选本人未使用的活动普通会话，禁止已有thread/owner、已有来源记录或R2任务会话。依赖先于正文输出持久化，记录原授权编号、授权代次、事项权限代次、固定正文快照及材料revision；packet还记录完整权限范围摘要。限定投影version是全部当前可见grant ID/version/epoch的48位哈希选择号，不能按从1开始的普通计数解释；新旧授权替换或多个资料包组合变化后旧确认409。多个资料包的冻结正文在预览与上下文读取中按同一顺序聚合。来源还固定有效任职、职责的ID/版本/代次及部门路径版本；即使撤权期间未检查旧对话，重新任职或授职也不能复活原依赖。

`GET /v1/shared-contexts/{id}/read`只补读本人明确选择的快照。撤权、材料撤回、身份代次或原授权改变后，旧上下文持久标为blocked，返回403 shared_access_revoked；重新授权也不恢复旧上下文。权限仓储不可读时拒绝使用内容，不回退缓存授权。

主对话显式传shared_context_id，只生成带来源的文本和草稿；明确选中的每份UTF-8文本/JSON最多2MiB，单份提供16000字符、合计48000字符，节选明确标记，其他格式明确未读；不能同时带个人附件、私人事项、业务会话或客户端历史。每次实际模型发送、流式输出、结果保存及缓存回放都重新检查。主消息保存source_context_id及sources，历史整页同一协调工作单元投影，失效消息改为无正文、附件和元数据的占位。普通个人路由不携带共享消息，也拒绝受保护业务session_id；入口、历史补读前后及实际发送各自检查，覆盖晚绑定竞争。后台主动扫描及原证据恢复排除全部共享消息和共享业务会话，不把共享承诺派生成无来源私人跟进。

业务阅读使用公开ContextEngine与ModelClient双重检查，受保护模式关闭全文请求capture。仅安装read_shared_context及read_shared_material，工具再核SubmissionIndex真实会话及员工。材料工具仅支持明确选择的最多2MiB UTF-8文本/JSON，每次有界节选；其他格式引导本人原生阅读，不声称已提取正文。脚本、文件写入、连接器和子任务均拒绝，不把共享内容挂载到通用沙盒。独立examples/shared-reader声明这两个工具；旧业务包不会自动扩权。

业务正文、history、pending、events JSON在读取后再校验；SSE在各帧外发前检查。维护循环在引擎回调外回收失效引擎和缓存；重新装配平台仍读取持久来源。自动化已覆盖memory/PG冷平台重建、实际模型替身请求、撤权读竞争和晚绑定；此处的冷平台测试不等于真实进程SIGKILL、真实模型语义或生产验收。

原生文本发布只接受本人已保存且仍有效的受保护assistant消息。正文不能由客户端替换，来源引用由该消息取得；发布意图和ready提交分别检查当前来源。目标事项读者也必须满足所有原来源当前ACL，不能只凭目标成员身份读取。full与packet足迹不同，另一个不相关packet不能替代原范围。来源撤回后固定派生副本继续保留SHA但不可补读。

## 迁移与灰度

0017一次建立事项、成员、grant、grant精确版本关联、材料及revision、发布意图、固定来源关联、受保护上下文、来源依赖、消息sources空默认值及站内通知来源。不得回改已发布SQL。消息的sources引用同一受保护上下文的固定依赖集合；旧私人消息保持空来源及原请求指纹。候选默认shared_matters.enabled=false、tenants=[]，关闭后保留既有受保护读取、归档、授权撤销及材料撤回。生产发布还需员工入口、独立复核和分层验收；本稿不构成这些工作的通过记录。

## 员工入口、历史及来源通知

「功能 → 共享协作」提供无模型新建、编辑、关闭/归档、到人整事项或精确资料包授权、撤销、负责人转交、固定材料上传/本人文件版本发布/下载SHA验证/撤回、来源选择、独立阅读、草稿固定发布及结束阅读。部门分支仍可经正式API配置，当前原生邀请优先具体员工。动作由GET的allowed_actions给出，服务端即时复核，不以UI隐藏代替权限。共享文本发往其他事项的API存在，当前页面固定发布回本事项。

普通主对话的两处历史分页都传private_only=true，并在渲染处再次排除带source_context_id/sources或source_blocked记录。共享历史通过source_context_id在分页前过滤；本人上下文GET/list仅返元数据，close要求上下文版本并保持终态。普通助手仅可提出@shared/create的title/content草稿或@shared/manage入口，成员和权限不能由模型夹带；历史中的共享草稿独立恢复，其确认操作号由proposal ID与固定字段SHA确定，断网或刷新重试不会随机另建一单。

共享动态复用0017的来源自有站内表和既有协调outbox，每个来源/收件人有稳定语义键，按租户锁内分配sequence，逐事件及逐收件人ack。单批最多100人，晚提交的小来源ID不会被最大游标跳过。通知只存通用说明和共享引用，不保存私有标题；无外部投递。消费者使用当前有效身份代次及ACL，列表、数量、标读、SSE逐帧补读均复核。

`GET /v1/notifications`在原items之外提供独立shared分页；`GET /v1/shared-notifications`使用after_id/before_id/limit/latest，total/unread_total独立于页长；标读POST /{id}/read用稳定请求和通知版本。SSE沿用统一连接，新增shared_notification与shared_notifications_changed；共享帧不设置id以免覆盖原提醒Last-Event-ID。连接参数shared_after_id与原after_id分别记录。

visibility_revision包括可见通知记录与权限足迹；permission_revision独立覆盖全部当前可见事项的access_epoch、grant和任职/职责足迹，即使没有旧通知也覆盖撤权。普通材料上传不改变权限摘要。摘要变化或403/404时，页面先递增generation并清理旧内容、确认卡和上下文引用，迟到请求不能重新回填；再原生重新选择。普通私人历史从未缓存这些正文。身份切换清除共享和通知状态。SDK将来源SSE error转为带错误码的XuanpuError；原生流也停止并释放reader。

候选自动化门禁和分层验收记录见交付计划对应阶段。真实模型、真实员工、生产镜像、迁移、前向回退和清理仍由R3-E分别记录，不能从合成模型或本机浏览器推断通过。
