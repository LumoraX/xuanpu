---
name: table-summary
description: 对CSV或XLSX原始记录按部门与币种做Decimal精确汇总并保留行证据
version: 1.0.2
type: composite
scripts:
  - name: inspect_headers
    path: scripts/inspect_headers.py
    language: python
    description: 读取CSV或XLSX每个工作表的真实表头，避免猜测列名
    timeout_seconds: 30
    max_output_bytes: 65536
    args_schema:
      type: object
      properties:
        inputs: {type: string, description: 固定输入相对名称组成的JSON数组}
      required: [inputs]
  - name: summarize
    path: scripts/summarize.py
    language: python
    description: 精确汇总金额，生成CSV、XLSX、Markdown及行级JSON证据与清单
    timeout_seconds: 60
    max_output_bytes: 65536
    args_schema:
      type: object
      properties:
        inputs: {type: string, description: 输入相对文件名的JSON数组}
        group_column: {type: string, description: 分组列名，例如部门}
        amount_column: {type: string, description: 金额列名}
        currency_column: {type: string, description: 币种列名，无此列时传空字符串}
      required: [inputs, group_column, amount_column, currency_column]
---
读取用户意图，明确原始表格的分组列、金额列和币种列后运行 summarize。用户已明确列名时直接使用，由summarize脚本核对实际表头；不得仅因为XLSX为二进制就要求重复已给出的列名。列名未明确时先运行inspect_headers读取真实表头；只有语义仍有多个可能映射时才追问，不能猜测。
脚本成功才可报告已核算；失败时引用实际错误行，不编造数字或成功清单。不能自行去重、补金额或换算币种。
CSV仅支持UTF-8。XLSX只读常量，公式、宏和外部链接一律拒绝，要求值快照；不使用公式缓存值冒充重算。
每行是一次贡献，重复行计入汇总并在报告披露；空行忽略，空或无效金额拒绝并列出文件/工作表/行号。
没有币种列时标注“未注明”，各币种分别合计，不跨币种求和。金额最多12位整数、6位小数，不做静默四舍五入。
脚本生成 outputs/summary.csv、summary.xlsx、report.md、evidence.json 和 manifest.json。
CSV按分组行加每币种合计行输出；XLSX金额保存为精确十进制文本，防止Excel浮点精度丢失，自由文本强制字符串防公式执行。
只有包含实际脚本证据与行级evidence.json的数值才能称已核算。完成后告知成果已进入校验保存，不能提前承诺业务成功。


调用参数示例（固定输入为`inputs/input_1.csv`，用户明确分组列“部门”、金额列“金额”，没有币种列）：
```json
{"skill_id":"table-summary","script_name":"summarize","args":{"inputs":"[\"input_1.csv\"]","group_column":"部门","amount_column":"金额","currency_column":""}}
```
`inputs`必须是JSON数组的字符串，数组元素是`input_1.csv`这样的文件名，不能加`inputs/`前缀；
脚本会自行在只读inputs目录解析。不要传Python对象字符串、原生数组、对象列表、input_path或null币种。
XLSX同样传`"[\"input_1.xlsx\"]"`；多个输入用同一个JSON数组字符串列出全部固定文件名。
