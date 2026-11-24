"""准备报告只允许非敏感检查和不可逆版本摘要。"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class ReadinessCheck(TypedDict):
    """单项结果只承载固定代码及固定解释。"""

    code: str
    status: Literal["passed", "blocked", "not_verified"]
    detail: str


class ReadinessReport(TypedDict):
    """配置检查不构成逐单执行许可或正式外部验收。"""

    status: Literal["configured_simulation", "blocked", "not_verified"]
    production_status: Literal["not_verified"]
    checks: list[ReadinessCheck]
    versions: list[dict[str, Any]]
    configuration_digest: str
    external_verifications: list[str]


EXTERNAL_VERIFICATIONS = [
    "formal_policy_approval",
    "provider_integration",
    "official_invoice_and_signature",
    "worm_retention",
    "offsite_disaster_recovery",
    "real_model",
    "production_load",
]


def check(code: str, passed: bool, detail: str) -> ReadinessCheck:
    """使用固定解释生成检查，禁止直接塞入异常和持久正文。"""
    return {"code": code, "status": "passed" if passed else "blocked", "detail": detail}
