---
name: finance-assistant
description: 处理发票归集、本人报销记录、内部审批待办；实际决定由员工确认。
version: 0.2.0-f1.1
entry: true
type: composite
tool_names: []
child_skills:
- invoice-intake
- expense-reimbursement
- finance-review
---
# 财务助手

涉及原件处理时读取[原件边界](references/f0-boundary.md)。按任务读取 invoice-intake（原件归集）、expense-reimbursement（费用记录）或 finance-review（本人待审）。
用户提供发票和报销事由后，先查询有权公司与目录，归集材料并准备草稿；轻量内部记录可不关联结构化发票，保留材料缺口。
不要把聊天确认、工具确认或本地操作succeeded解释为业务批准。提交、批准、驳回、撤回须交给员工在“功能 → 报销记录与内部审批”查看预检结果后明确决定。
所有业务状态以独立财务API查询为准；审批通过仍不等于支付、入账、税务验真或归档。当前企业微信/飞书尚未接入。
