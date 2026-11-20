# R7 小院：管家与匠人的执行过程可视化（设计）

状态：用户于 2026-11-19 认可原型方向并要求按本设计实施。原型见 [prototypes/2026-11-19-r7-courtyard-demo.html](prototypes/2026-11-19-r7-courtyard-demo.html)（手写事实驱动，自检入口 `#auto,para`）。

## 为什么

R5 已能把一次请求的执行记成 trace/span，入口里的「进展卡」用执行树和瀑布展示。用户认为这「像 DAG 排布，不友好」，希望：

- 管家有可爱、可定制的角色形象，交代子任务时用「写信 / 打电话 / 当面说」等动作表达；
- 匠人像在真实世界里干活：去档案柜、电脑、书架、工作台、看板、小床；
- 能看到细节：管家与匠人各自做了什么、想了什么、用了哪些技能和工具——但要「友好」，像群聊一样用自己的话讲，而不是日志。

## 目标

1. 员工发一句话 → 管家与匠人真实运行 → 进展卡里的小院按 trace 实时演出；刷新后由持久记录重建。
2. 「经过」面板：按时间像群聊一样列出每个角色用自己的话说的每一步，技术细节折叠在「细节」里。
3. 角色可定制：管家的样子、颜色、配饰、称呼由员工自己定；匠人的默认形象由业务包名稳定推导（同一匠人对谁都长一样），员工可在本设备上改。
4. 执行树与瀑布保留，放到「执行树」页签。

## 非目标

- 不展示模型推理（`assistant_reasoning`）、不展示工具原始参数与原始输出。
- 不在小院里直接做审批与裁决：轮到本人时显示红色提示并引导到既有的待处理入口（`/v1/assistant/pending` 答复卡、会话待确认）。
- 不做像素精灵或 AI 生成形象（后续可选）；不做服务端保存的角色设置（本期存本设备）。
- 不改业务包规范（`xuanpu-spec`）：工具归到哪件家具由平台按名称归类，不要求业务方声明。

## 反目标（技术上能跑也算失败）

- 动画替代事实：画面上出现记录里没有的动作或结论。每个动作必须对应一条已有事实。
- 卡住对话：采集新字段让事件登记变慢或抛异常。
- 越权：别人的派工、trace 细节能被读到。

## 约束

- 红线 1、3、5 不变；只依赖 `taifeng.__all__` 与内核稳定事件（`assistant_text`、`tool_call_started` 是稳定事件种类）。
- ADR 0017 决策 5「span 不存正文」需要放宽一处：新增 ADR 0022 说明（只增不改）。
- 前端仍是无构建的原生脚本；入口资源带指纹；375px 无横向溢出；`prefers-reduced-motion` 时不播动画、直接画终态。
- 本人可读：与 `/v1/traces` 一样只给当前启用的员工本人，他人的 404，机器身份 403。

## 方案比较

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| A. 匠人的「旁白」进 span（采用） | 采集器把同一执行在调用工具前说的话（`assistant_text`）截成一句，挂在该工具 span 的 `say` 上；派工细节另开只读接口 | 话来自匠人本来就会对员工说的内容（直连会话里员工本来就看得到）；不碰推理与工具参数；只多存≤160字 |
| B. 开放工具原始参数与输出 | span 存 arguments/output | 最完整，但长、难读，且会把连接器规则刻意挡住的数据放出来 |
| C. 只用模板句 | 前端按工具名与家具拼句子 | 不改存储，但永远只能是「用了某工具」，说不出为什么 |

采用 A，且在没有旁白时退回 C 的模板句。B 不做。

## 设计

### 后端

1. **旁白（`say`）**
   - `TraceCollector` 为每个执行维护一段缓冲：收到 `assistant_text` 追加 `delta`（缓冲上限 2000 字，超出丢头部）；收到同一执行的 `tool_call_started` 时取出缓冲、清洗成一句挂到该工具 span 的 `say` 并清空；执行一轮结束（`turn_completed/failed/suspended`）清空。
   - 清洗（纯函数 `tracing/narration.py: narration(text) -> str | None`）：去掉成对 `<think>…</think>` 块、去控制字符、折叠空白；不超过 160 字整段保留，超过时从结尾往前按整句保留（单句过长从开头截并加「…」）；空串返回 None。
   - `assistant_reasoning` 一律不读。
   - `SpanFact.say` / `SpanRecord.say`：可空，≤160 字；首次出现即定，之后不同值不覆盖也不标冲突（迟到或重复投递无害）。span 以 JSONB 存，无需迁移。`span_view` 输出 `say`。OTel 不导出。
2. **匠人说话约定**：派工发给匠人的说明末尾追加平台约定一行「（每次动手前，先用一句话告诉员工你要做什么。）」。只在投递时追加（会话的首条消息、任务的 text），派工记录里的 `instruction` 与幂等指纹不变。
3. **派工细节** `GET /v1/assistant/dispatches/{dispatch_id}`（只给本人）：
   ```
   dispatch_id, trace_id, group_id, artisan{package, entry}, host(task|session), state, end_reason,
   instruction, created_at, updated_at,
   checks: {terminal, succeeded, outputs, no_pending, no_unknown} | null   # 最近一次带验收的决定
   result: str | null          # 最近一次决定的 artisan_text，截到 600 字
   items: [{kind(question|approval|ruling|proxied), question, state, answer, source_quote}]
   ```
   需要 `WakeStore.events_for_dispatch(owner, dispatch_id)`（内存与 PostgreSQL 各一份，按创建先后）。

### 前端（`xuanpu-console/static`）

| 文件 | 职责 |
| --- | --- |
| `courtyard-art.js` | 角色画法（6 种样子 × 7 色 × 7 配饰）、表情与道具；`lookFor(package)` 稳定推导；`loadLooks/saveLooks` 读写本设备 |
| `courtyard-model.js` | 纯函数：spans + 派工细节 + 编组细节 → 角色表、事件节拍（beats）、「经过」条目；工具名 → 家具；结束原因 → 中文 |
| `courtyard-scene.js` | SVG 小院、角色移动与动作、按节拍排队播放；快照只画终态 |
| `courtyard-card.js` | 接进进展卡：订阅 trace 增量、按需取派工与编组细节、渲染小院 + 「经过」+「执行树」页签、角色定制面板 |
| `courtyard.css` | 小院样式（场景内固定色板，外层用入口令牌） |

- **事实 → 画面**（与原型一致）：
  - 管家根 span → 管家；派工 span → 匠人上垫（按包名去重，最多 5 位，多出的显示「+N」）。
  - 交代方式：编组 consult → 圆桌当面；任务承载 → 写信；会话承载 → 走过去当面说。
  - 工具 span：按名称归到家具 —— `read_skill`/`read_*`/`search_*doc*` → 书架；`run_script`/`exec*` → 工作台；名字含 `.` 或 `__`（连接器、MCP）→ 电脑；`report*`/`dashboard*`/`*_trend` → 看板；`call_skill`/`await_skills`/`wait*` 或 waiting_children → 小床；其余（`query_*`、`list_*`、`get_*` 等）→ 档案柜。
  - 管家工具：`list_artisans` 翻名册，`answer_artisan` 接电话，`rate_divergence` 放大镜；`propose` 拿纸笔。
  - 匠人 span `waiting_input`：派工细节里有 open 的 question → 打电话；approval → 红色「等你盖章」；ruling → 管家红色「等你裁决」。
  - 派工终态 → 匠人交回（信 / 卷轴）、管家放大镜验收（勾的数目取 `checks`）；失败 → 晕。
  - 根 span 完成 → 管家回话（文字在对话里，场景只播动作）。
- **「经过」**：条目来源与文案
  - 匠人每一步：`say` 原话；没有时模板「我在{家具}用了「{工具}」。」；进行中加省略号；「细节」里给工具名、用时、是否出错。
  - 派工：信纸样式的任务说明（`instruction`）。问题、代答（带出处）、等盖章、裁决：取 `items`。结果：`result`。验收：`checks`。
  - 顶部一句总结：状态 + 匠人数 + 工具次数 + 用时。点角色只看它；再点回到全部。
- **轮到你**：红色条目带「去处理」按钮，滚到页面里对应的待处理卡（既有 `cards.js` 的待处理答复卡）；本期不新增写接口。
- **降级**：浏览器没有 SVG（`document.createElementNS` 不存在）或脚本出错时，进展卡照旧显示执行树。
- **角色定制**：小院右上角「换角色」打开面板（原型下半部分）；管家称呼可改，匠人名字来自包名不可改。

## 验收

- 服务端：采集器测试（`assistant_text` → 工具 span 的 `say`、推理不进、超长截断、一轮结束清空）；存储契约（`say` 落库、迟到不覆盖）；派工细节接口（本人可读、他人 404、机器 403、结果截断、问题与代答列出）；派工投递带约定、指纹不变。
- 前端：`courtyard_behavior.cjs`（Node）测纯模型：家具归类、节拍顺序、经过条目、模板退回、等你提示；入口测试仍过（没有 SVG 时退回执行树）；静态测试资源指纹。
- 门禁：`uv run pytest`、ruff、mypy 全绿；浏览器手工验收截图记入发布记录（本机无真实模型时用原型自检）。

## 留给实现者

动画时长、角色与家具的具体坐标、文案措辞细节，以原型为准即可。
