#!/bin/sh
# 按报销制度核对一笔费用。参数按 SKILL.md 里 args_schema 的声明顺序传入：$1 = amount，$2 = has_invoice（yes/no）。
# 脚本在沙盒里运行：只能读业务包目录，只能写 $WORKSPACE，默认不能出网。
amount=$(printf '%.0f' "$1" 2>/dev/null) || { echo "金额不对：$1" >&2; exit 2; }
case "$2" in
  yes) if [ "$amount" -gt 5000 ]; then echo "可以报销：有发票；超过 5000 元，需部门负责人与财务负责人确认"; else echo "可以报销：有发票，部门负责人确认即可"; fi ;;
  no) if [ "$amount" -le 200 ]; then echo "可以报销：无发票但不超过 200 元，需写明原因并由部门负责人确认"; else echo "不能直接报销：无发票且超过 200 元，需要先补开发票"; fi ;;
  *) echo "has_invoice 只能是 yes 或 no：$2" >&2; exit 2 ;;
esac
