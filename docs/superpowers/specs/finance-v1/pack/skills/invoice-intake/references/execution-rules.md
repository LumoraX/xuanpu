# 执行规则

身份由平台签名出口注入，不能在参数里填写 tenant_id、user_id、状态、存储路径或对象凭据。
每次新写入生成新 Idempotency-Key；同一请求重试保留相同 key、文件版本和正文，不要换 key 重复创建。
材料补齐不等于业务批准。平台工具确认也不等于财务审批。
本阶段 production 未接入官方票据标准映射，未知格式会保全原件并进入异常；synthetic 格式仅用于开发测试。
verification_status=not_checked、authority_status=unverified 必须按原义呈现，不称已查验通过。
付款、记账、税务查验及正式归档通道当前均 unconfigured，不猜测其执行结果。
