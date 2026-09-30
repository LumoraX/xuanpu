#!/bin/sh
# 按优先级给出处理时限。参数按 SKILL.md 里 args_schema 的声明顺序传入：$1 = priority。
# 脚本在沙盒里运行：只能读业务包目录，只能写 $WORKSPACE，默认不能出网。
case "$1" in
  high) hours=4 ;;
  normal) hours=24 ;;
  low) hours=72 ;;
  *) echo "未知的优先级：$1" >&2; exit 2 ;;
esac
echo "处理时限：${hours} 小时"
