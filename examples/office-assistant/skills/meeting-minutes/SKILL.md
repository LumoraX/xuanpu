---
name: meeting-minutes
description: 从文本、DOCX或可提取正文PDF整理纪要与待确认行动项
version: 1.0.3
type: composite
tool_names: [file_read, file_write]
scripts:
  - name: extract
    path: scripts/extract.py
    language: python
    description: 安全提取固定输入正文，返回用于原文引用的准确字符区间
    timeout_seconds: 30
    max_output_bytes: 1048576
    args_schema:
      type: object
      properties:
        inputs: {type: string, description: 固定输入相对名称组成的JSON数组}
      required: [inputs]
  - name: cite
    path: scripts/cite.py
    language: python
    description: 从固定正本计算唯一原文引文的准确Unicode字符区间
    timeout_seconds: 30
    max_output_bytes: 16384
    args_schema:
      type: object
      properties:
        input_name: {type: string, description: 固定输入相对名称，不加inputs前缀}
        quote: {type: string, description: 从extract正文复制的完整唯一原文}
      required: [input_name, quote]
  - name: timestamp
    path: scripts/timestamp.py
    language: python
    description: 按明确时区将墙钟时间换算为准确UTC秒，拒绝DST歧义
    timeout_seconds: 30
    max_output_bytes: 4096
    args_schema:
      type: object
      properties:
        local_time: {type: string, description: 原文明确的ISO当地时间}
        timezone: {type: string, description: 用户明确的IANA时区}
      required: [local_time, timezone]
---
先运行 extract 读取指定材料。只基于提取的完整正文整理，不使用截断数据生成完整纪要。
纪要保存 outputs/minutes.md，区分讨论、已决定事项、待核实事项；不把讨论的日期自动当截止日期。
行动项保存 outputs/actions.json，最多32项。每项严格格式：
{"kind":"todo或event","title":"标题","description":"说明","owner":"self或other或unresolved","due_at":null,"start_at":null,"end_at":null,"remind_at":null,"source":{"input_name":"input_1.txt","start":0,"end":10,"quote":"与该字符区间完全相同的原文"}}。
start/end是extract返回字符串的Unicode字符位置。只能引用原文真实区间；不得编造引用。
时间用UTC秒，按用户消息中的参考时间和IANA时区解释；含糊日期或起止不明时保留null和待核实说明。
owner只有原文明示本人承担且可核实时才self，其他人的承诺用other，负责人不明用unresolved；确认也只能创建本人记录。
remind_at只在用户明确要求提醒且有确切时间时填写，不能自行默认提醒。
不创建任务以外的业务记录；结果保存后由玄圃给本人逐项确认，可以只接受部分或全部取消。
最后保存 outputs/manifest.json，kind=meeting_minutes，列出纪要minutes及行动项actions两项。

extract参数示例：{"inputs":"[\"input_1.txt\"]"}，inputs必须是JSON数组的字符串，不是input_path，也不加inputs/前缀。
每条source都必须先调用cite脚本，例如{"input_name":"input_1.txt","quote":"我负责归档资料。"}，直接采用返回的input_name/start/end/quote，不能手工估算字符位置。不存在或不唯一时先澄清或使用更完整原文，不可伪造引文。
最终清单必须逐字采用这个结构（字段是artifacts，不是files；name必填）：
{"kind":"meeting_minutes","artifacts":[{"path":"outputs/minutes.md","name":"会议纪要.md","role":"minutes","mime_type":"text/markdown"},{"path":"outputs/actions.json","name":"行动项.json","role":"actions","mime_type":"application/json"}]}。

所有明确日期必须调用timestamp脚本换算，例如{"local_time":"2026-10-24T17:00:00","timezone":"Asia/Shanghai"}，逐项复制返回的timestamp；due_at/start_at/end_at/remind_at都不能心算Unix秒。写入前对照脚本返回的local_time和原文逐字核对，不一致时拒绝猜测。未明确的日期仍保持null。
