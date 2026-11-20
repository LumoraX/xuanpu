#!/bin/sh
# 按付款日期算等待天数。参数按 SKILL.md 里 args_schema 的声明顺序传入：$1 = paid_on，$2 = today。
# 脚本在沙盒里运行：只能读业务包目录，只能写 $WORKSPACE，默认不能出网。
to_days() { date -j -f "%Y-%m-%d" "$1" "+%s" 2>/dev/null || date -d "$1" "+%s" 2>/dev/null; }
paid=$(to_days "$1") || { echo "付款日期格式不对：$1" >&2; exit 2; }
today=$(to_days "$2") || { echo "今天的日期格式不对：$2" >&2; exit 2; }
[ -n "$paid" ] && [ -n "$today" ] || { echo "日期格式应为 YYYY-MM-DD" >&2; exit 2; }
days=$(( (today - paid) / 86400 ))
if [ "$days" -gt 2 ]; then echo "已等待 ${days} 天，超过 2 天发货承诺"; else echo "已等待 ${days} 天，仍在 2 天承诺内"; fi
