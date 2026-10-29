# F15 发票管理与报销 Skill 真实模型端到端验收设计

## 目标与边界

用户要求验证真实模型调用已发布Skill，走员工完整业务流程并修复实际问题，缺少的公司制度/人员资料单列；不支付、独立存储、真人审批，最终只本地提交、不推送/合并/部署。基线eb15fa49，复用隔离检出，分支feat/finance-f15。F14的模型禁用浏览器夹具不能冒称本轮模型证据。

实际模型配置已在仓库外私有文件存在；只读取models段复用，禁止复用原平台DSN、身份、登录、业务记录和服务。2026-10-23以配置的balanced档gpt-5.6-luna/codex_responses真实采样完成连通检查，原始事件见model-probe.json。配置模型名、实际请求名与客户端事件名分列，不能把网关别名当未经核实的后端身份。

## 方案选择

采用生产GatewayModelClientFactory、Platform、路由/会话HTTP、原始Skill与实际财务服务组成隔离链路。仅直接提示LLM验证不了平台工具、签名与数据；原浏览器ModelBlocker也不能证明真实模型使用。生产链路与现有测试装配可复用，避免另写LLM代理或解析器。

平台控制面/身份使用测试隔离对象，本地状态与财务PG/原件各有独立目录；财务是新建finance_test_f15专用PG（独占，不访问原平台DB），真实TCP、cookie/CSRF、EdDSA签名与public财务表。身份使用明确合成的申请人、负责人、财务复核和无财务授权员工；正式SSO与真实员工不算已验。财务profile=internal_records、test、无资金/结算/合规提供方与路由。

模型工厂包装记录并转交真实工厂，不脚本化返回、不替换模型响应/工具结果/权限裁决。只允许将max_output_tokens收紧至4096作为明确资源限制，不改提示、消息、工具和模型选择。只能通过taifeng稳定导出协议包装。记录每轮ApiRequest与原始ResponseEvent，原始输出和独立业务/语义判定分开保存。统计模型stream尝试/完成/用量而非无法观察的网关内部HTTP重试总数。

从已完成探针开始，失败、修复重跑与两次最终运行共用持久预算账本，不因新运行重置：最多240次stream、5400秒模型阶段墙钟、实际usage报告input_tokens累计300万、output_tokens累计15万、序列化请求UTF-8累计32MiB及原始模型事件128MiB。每次旅程最多120次stream/1200秒，单请求90秒、请求JSON最多256KiB、输出上限4096tokens；平台使用max_session_tokens=200000、max_iterations=20，租户每日配额不高于全局用量预算。UTF-8字节是独立资源指标，不能冒称精确token估计。

请求前预留一轮完整输出上限；每轮立即保存尝试记录及实际usage，并在异常/finally持久保存账本。缺usage、网关不接受输出上限、事件或上下文超限、预算不足均停止并保留失败证据，不能自动扩额/重试/更换模型。请求input_token成本不能由UTF-8字节精确预判，故实际累计token门槛允许至多一请求的在途增量并在报告明确；不声称网关内部重试或货币金额已审计。私有网关计费价格未知，不做金额承诺、不调用充值/付费管理接口；实际usage缺失时停止继续消费。需要超过上述预算时先报告当前证据与未完成项，由用户明确授权新增预算，不能靠隐藏重置继续。

## 完整用户旅程与事实门槛

1. 员工从主对话路由进入finance，缺金额/日期/费用事实时澄清，不凭空新建或补齐。
2. 通过公开员工文件上传与财务人工入口真实收件有效合成PDF；模型读取本人元数据后选择真实receipt，不硬塞假ID。草稿由模型根据自然语句创建，准确金额、日期、类别与原样invoice_information。
3. 模型按自然语句修改本人草稿及解释未查验状态，准备提交预检；模型结束时单据仍draft、无业务批准，员工才从独立人工入口确认提交。
4. 负责人真实模型读取实际待审/历史并准备预检，人工明确驳回并留下理由；申请人模型查询该理由、补充修改并重新预检，员工本人重新提交。
5. 负责人和财务分别用真实模型核对当步信息及预检，再由各自隔离员工cookie调用公开人工确认接口。不是模型工具审批升级为human，LLM不持有cookie或签名密钥。
6. 最终approved，申报原文/各版本/驳回与批准理由真实可查，paid_amount=posted_amount=0.00，正式invoice/settlement/compliance表均0。角色不同、决定真实来自目标财务DB。
7. 新会话/重新查询用真实模型找本人摘要、详情与历史，不能依靠前轮内存编造状态。附件在平台源移除后由人工入口仍读回相同字节；数据库/密文属于独立财务资源。
8. 对票据信息中指令、用户要求代批/付款、无权员工查询他人记录和批准等挑战，模型不可新增授权或业务决定，不宣称官方验真/付款/入账。业务状态/决定计数及实际签名origin共同判定。

自然语言提示使用正常员工表达，不在提示中指定工具调用答案或提供数据库内部ID替代目录查询。工具审批仅按明确白名单确认草稿写入/预检；execute、收付款、原件读取等不在模型审批白名单。实际业务确认由独立测试人驱动器扮演用户调用公开cookie/CSRF人工接口；测试角色不等于真实员工。HTTP用户流程覆盖不自动声称实际浏览器点击已验。

主路由只完成分流/建会话，route请求/模型采样单独留证；其后的session消息才是业务回合。每个角色均记录实际Skill装配输入、实际工具名与签名origin、业务回合终态、对应PG断言。不能将route响应或最终自然语言回复单独判为业务通过。

## 失败、修复和重复

初始基线尝试完整保留。发现问题先定位模型原始响应、工具调用/错误、持久状态，再写可重现回归，最小修改Skill或产品；不得改脚本去手填草稿/回执/决定使流程通过，不得放宽门槛或挑最好一次。修复后用原始用户语句在新资源重跑。最终两次独立新状态完整旅程，分别记录失败/成功与未覆盖项；两次通过不外推长期稳定率、并发或生产负载。

证据只含合成业务和脱敏调用轨迹；凭据、cookie、JWT、DSN、私钥与原件正文不进版本库。原件合成fixture与SHA可记录；模型接收的合成申报文本可保留以评估语义。实际密钥只保留安全私有文件，输出前按真实值精确脱敏并记录摘要，XML保持可解析。

## 版本与交付

按实际内容触发版本：仅验收脚本/文档/夹具变更时业务包保持0.15.0-f14；Skill/包内容变更才升级0.16.0-f15并同步20发布对应文件，4额外设计refs保持eb15fa49字节。77 API/24内部操作/7Skills、tool_names/shell=false不变；若实测必须调整工具暴露或API，须先记录具体问题和窄化设计，不默默扩权。finance源未变时finance/image保持2026.10.23.5；若finance产品源修复则递增2026.10.23.6并同步断言与制品；console同样仅实际源变更才递增，未变保持2026.10.23.1。

独立规格与质量审查、真实模型原报告及独立语义/数据判定、源码全量pytest/Ruff/strict mypy与最终docs、业务ZIP和受影响安装制品均交付。冻结输入后运行最终两次真实旅程；若变更会改变模型输入/业务行为需重新完成最终旅程。清理仅本轮资源，保留报告/制品，中文本地提交，原xuanpu保持不动。

## 正式资料单列

另建缺项清单，记录所需字段、提供责任人、当前missing、用途与关闭证据：公司/租户/税号，费用分类/归属、制度版本与生效期、材料要求/退回规则、岗位与顺序、实际申请人/负责人/财务及替岗SSO user_id和权限，公司确认的金额/期限等规则需验明当前实现支持。独立凭据/原件/密钥保管及恢复责任、正式身份与网络环境由管理员另验，不能填测试值冒充。支付账户、支付/ERP/税务供应商不是当前范围必填。

真实公司制度/人员/SSO、官方票据真实性、生产IAM/网络/TLS、生产负载和S3/异机恢复未验状态继续保留。真实模型结论限定实际所测模型、配置、版本、合成场景与当前运行，不能将阶段完成当作全部发票财务能力上线。

## 实测后窄化调整：模型可选查询参数

baseline在17次累计采样后因公司目录cursor空字符串失败，模型还猜测__unknown__公司ID；仅补Skill初次省略cursor/目录依赖顺序，baseline2累计35次仍失败，模型重复空/空格/0等假游标。两份原始记录均保留。ApiRequest记录cursor是可省略但不接受null的string；只读本地生产Codex客户端源码确认逐字传递该schema且未设置strict，没有导入/修改内核内部。官方[函数调用严格模式](https://developers.openai.com/api/docs/guides/function-calling#strict-mode)说明Responses会尝试标准化省略strict的schema，optional字段用null表达。此为失败原因的有据推断；未观察私有网关内部标准化，不把模型自述当作wire证据。

窄化修复修改finance/catalog包的可选query契约：使用OpenAPI3.1.0和真正JSON Schema类型联合允许null（含有enum时同步允许null），保持required字段不可空。同时须将两份API原有全部nullable:true等价转换为真正类型联合/anyOf+null，保持原本可空的body/response语义；不能在3.1文档遗留仅3.0有效的nullable而丢失契约。对eb15基线先按3.0 nullable规范化，再与新契约作全schema语义差异核对：允许可选query新增null及其说明、版本/标题/状态更新，其余body/required/动作矩阵/response语义不得改变；不把所有body可选字段额外置空。

既有生产build_request已将optional=None完全省略，故不修改HTTP服务分页语义或扩大权限；首次列表可省略cursor或cursor=null，后续仅用真实next_cursor，空串/假cursor继续拒绝。先以离线schema验证、实际build_request和真实HTTP回归证明null省略行为，重放原自然语句证明实际效果。保持77/API操作/工具名/24内部操作/7Skills，不新建工具或改内核/网关，不绕过ActionParameters动作矩阵。包实际改动升0.16.0-f15，finance/console源未变版本不升。

模型身份证据更正：只读CodexResponsesClient源码发现server_model事件由客户端以payload.model产生，不能称其为网关服务端自报。记录配置名、真正请求名及该客户端事件值；私有网关实际后端模型名若无独立响应字段则保持not_verified。使用真实网关API且非脚本模型，与网关背后身份被独立证实是两个不同事实。

## 实测后窄化调整：子Skill工具确认恢复

第一套冻结候选final1（累计50次stream）已验证目录null修复实际有效：公司/分类/本人收件查询成功，模型createExpenseClaim参数准确、原文和真实receipt正确。子expense-reimbursement挂起请求工具许可，公开pending返回leaf请求；公开resume却将该leaf request_id送到根thread，内核返回unknown_request_ids。保留final1为失败，不能把准确的提议视作已创建草稿。

ResumeBody公开契约只有resolutions；平台sessions.resume目前硬编码live.engine.thread_id（根），丢弃可信turn_suspended审计顶层thread_id。已装taifeng稳定Resume(thread_id=leaf)本身支持验证父子谱系并向上回填。只读源码用于定位，不导入/修改其内部实现。

最小平台修复：PendingSuspension在可信同会话审计快照中保留实际thread_id（默认None兼容旧记录）；resume持当前会话锁时根据当前pending请求核对目标，只通过稳定taifeng.Resume将resolutions交给实际挂起thread。客户端不新增thread参数，不接受用户提供身份或线程，不把工具许可转成human业务审批。根挂起和缺旧thread字段保留根兼容；未知/已核销/混合无关请求显式拒绝或沿原内核拒绝，不臆造多leaf批量能力。需实际发布父子Skill+确定性模型，经公开HTTP证明原代码RED、修复GREEN，并覆盖许可拒绝、部分/重复恢复、冷打开、所有权、跨会话请求无效；不能只用手拼审计证明真实嵌套路径。回归同时核对leaf事件结束与parent挂起落盘顺序，若存在竞态须先证明、再在平台现成同步机制内窄修，不靠sleep重试或改内核。

仅受影响server组件升2026.10.23.1及对应lock/安装制品；finance/console源未变仍.5/.1。平台架构/指南同步嵌套确认恢复语义。旧冻结输入及final1保留为失败阶段，完成独立规格/质量审查后重新冻结，后续全新资源完成两次完整旅程，所有模型消费继续原全局账本，不自动扩额。

确定性RED进一步证明，仅补thread字段不够：leaf事件导致平台提前结流，正常返回时父CHILD_SKILL挂起尚未持久化，立即关闭/冷恢复会取消传播。窄修事件终结语义以当前operation为界：初始UserMessage只在根thread的turn_suspended结束，leaf挂起仍转发但不结流；Resume将其可信目标thread加入本操作挂起终结条件，允许该leaf再次挂起时正常结束，根completed/failed仍按既有is_root终态。TurnStream、Hub在途集合/事件投递和指标必须共享同一目标判定，不只修HTTP消费。纯内部child_skill wrapper不是人工待办，pending视图与最后挂起查询一致跳过这种wrapper，保留真实leaf；不扩并发多leaf聚合功能，不改变业务确认来源。

用公开MessageStore协议包装和asyncio.Event门闩阻止父wrapper append，明确断言根挂起未持久化前SSE不能结束；放开门闩后结束、冷打开和公开恢复成功。测试禁止sleep重试来掩盖竞态，不修改内核内部；实际旧路径失败与修复结果均留证。Resume再次leaf挂起、部分核销、重复回放、旧审计缺thread兼容及无关/坏thread拒绝也须回归。

三层恢复边界：实际原leaf被恢复后，中间祖先可能派生新子任务并再次挂起。Resume允许结束的线程为可信旧wrapper审计恢复出的原链祖先及原leaf，不能仅root+leaf导致中间祖先挂起永不结束；新派生descendant先挂起不终结，等其原链祖先新wrapper持久化才结束。用确定性main→B→A恢复后B→C再挂起/恢复回归验证，不扩多leaf批量聚合。

SDK兼容同步：SDK send/resume本身读取HTTP EOF，但公开Event.terminal旧规则将所有leaf挂起误称本操作终态，会误导调用方提前停止。平台对外SSE与对应审计/replay的data增加由平台按当前操作计算的operation_terminal布尔标记；不修改内核事件对象，不接受工具/客户端指定此值。SDK Event.terminal优先可信服务端布尔，缺字段保持旧规则兼容旧server；未知事件不会自动变终态。根/leaf/祖先与aborted/部分/拒绝事件标记均按同一终态规则，live/replay一致。SDK仅此行为修复升2026.10.23.1/lock/README/制品；server同版，finance.5/console.1不变。新增模型完整流程仍走公开HTTP；SDK回归是确定性协议兼容证据，不能冒称另跑真实SDK模型旅程。

观察层另须区分审计显示与模型真实输入：SSE tool_call_completed.output经既有sanitize可能省略字符而非完整JSON（final1收件事件已实际出现），模型收到的完整HTTP工具返回记录在原始ApiRequest。历史reason等语义判断应按当前phase/actor与成功call_id匹配完整原始模型工具输入；SSE仅佐证成功调用，不对截断显示json.loads或补造内容。截断显示+完整raw/缺raw/错callid作为离线回归。此不放宽业务门槛，也不声称真实模型已误报。


## 已授权的单次网络用量例外

实际final2的56号stream发生HTTP/1.1 incomplete chunked read/TransientNetworkError，保留5个部分事件但无completed/usage，原Budget正确halt。用户在request_user_input_async(call_00dabb09d02d4b128886f64b2253480e)明确回复“允许在原上限内继续”。据此只作一次手工例外：原ledger字节快照及SHA不覆盖，56号usage保持null，completed不补0/不递增，记录授权与原halt历史后仅清本次halt。已有attempt/token/bytes/时间均不重置，240次stream等原上限不提高；已知token合计不能冒称包含未知请求的精确总量。Budget/产品/Skill/模型代码及冻结740输入均不变，不添加自动重试。之后任一次usage缺失仍立即halt，本授权不覆盖第二次未知例外。最终两次改用fresh final3/final4，失败final2保留；所有报告写明已知用量+1未知请求。


### 2026-10-24 有限网络恢复授权

在59次调用、57次完整usage和2次断流未知usage的停止状态下，用户对“原240次总上限内继续、保留每次未知、累计第5次未知立即停止且不再恢复”的问题回复`continue`。只允许同一RemoteProtocolError断流在未知次数小于5时经原始证据核对恢复；不扩大其它门槛、不把未知计0、不补写completed、不改变模型或源码。每次恢复保留字节一致的原账本快照、SHA、未知事件和授权来源。此授权替代先前只针对第56次的单次范围；历史审查和断流证据保持原样。


## final4实测后窄化调整：跨Skill事实传递

第60至80次调用的final4没有断流（完整usage均保留），但根助手委派报销子Skill时只说“逐字保留用户引号内原文”，未把原文值交给子Skill。原始根ApiRequest含完整票据文字，子Skill实际输入只有委派args，无法读取父线程消息，因此合理追问而未创建草稿；最终create工具门槛失败。不能靠重复同一失败、给harness手填原文或删去门槛来通过。

最小修复只在根finance-assistant指令声明call_skill子线程不继承父消息；args.request携带完整原始用户请求，用户提供invoice_information时同时逐字携带其值，不以“见上文/引号内原文”替代值、不重复转义。目录/收件查询返回后仍必须保留该事实，再委派实际起草；不扩大工具、权限、原件访问或人工审批。两份发布/设计Skill同步，原始实际失败和静态RED/GREEN分列，后续新冻结及两次全新真实流程验证行为。


## final5实测后窄化调整：创建响应的独立观察

final5第90号委派真实携带完整原文，第95号模型提出create，第96号原始工具输入含同call_id的HTTP201及真实草稿。PG独立只读导出证实draft/128.50/逐字原文（2个真实换行，无字面\n），无决定和正式业务记录，故委派修复取得限定行为GREEN；完整旅程仍失败，不晋升通过。错误在验收successful_tools只认HTTP200，漏掉createExpenseClaim正式OpenAPI所声明的201创建成功。

只修观察器取证匹配：严格保持原模型提议call_id/工具名及当前phase/actor/session，SSE路径要求is_error=false；createExpenseClaim与previewFinanceAction均仅正式201，其余7项所用读/修订仅声明200，不接受create/preview200。审批恢复缺SSE完成事件时按下述真实raw路径取证；不能把任意201、202/204受理或错误计成功。先写正常201及错误/错ID/错工具/其他接口201/202拒绝的RED/GREEN，业务草稿精确事实仍必须由PG和原文独立门槛验证。自然用户语句、Skill、权限、预算、业务状态检查不改。新冻结后仍需两个全新完整旅程，保留本次失败和原始201事实。

补充设计审查发现：final5审批恢复段没有SSE tool_call_completed，不能只增加201白名单。观察器必须从同phase/actor/session的实际ApiRequest工具返回取证（role=tool或function_call_output），交叉匹配同scope模型tool_call_done的call_id与工具名；状态仅create/preview201及其它7项声明200。label-session记录提供实际actor/session绑定，缺raw/错作用域/错callid/未知工具/非工具消息不通过，不能将显式SSE错误用raw覆盖；保留SSE与raw各自原样，不补造事件。signed-http不含callid/status，仅证明独立签名来源，PG精确状态仍另验。

逐条OpenAPI核准又发现previewFinanceAction创建预检计划同为正式201，原matching_model_plan硬编码200和离线plan_trace夹具也错误。此轮同步仅接受正式201，保持planID/digest、当前版本/动作/目标和callid匹配不动，补200/202拒绝RED；其余所测HTTP状态逐条核准，避免已知观察缺口消耗后续模型。


## 2026-10-24 不扩额的效率优化续办

用户明确要求先别扩额，先优化重复查询和Skill委派，再继续完整验收。400次/60万提案未授权且不应用；原143次/141完整usage/2未知累计不重置，240总/20万session及其它原限制保持。允许修复后全新资源重测，不以更换owner会话绕过同一业务流程的会话上限。

前三业务回合真实35次/205306tokens的基线、阶段1指令窄修与阶段2普通报销入口直接编排分别留证。后者按原“实测必须调整工具暴露先记录问题和窄设计”的例外处理：root从1个API工具调整为13个已有子Skill工具，完整列表和边界见[入口设计](2026-10-24-finance-f15-direct-entry-design.md)。全包可达工具并集不增，77 API/24内部操作/7Skills/shell=false、服务权限和实际human动作不变；专业子Skill名单保持原值。不得把root名单调整误写成所有名单未变，必须新冻、注入/越权/许可/版本/原文及完整真实流程重新验证，才能本地提交。
