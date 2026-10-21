---
name: meeting-minutes
description: 从文本、DOCX或可提取正文PDF整理纪要与待确认行动项
version: 1.0.1
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
