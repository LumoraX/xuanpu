"""对真实签名API返回逐项核对发布包严格schema。"""
from __future__ import annotations

import re
from typing import Any

import jsonschema

from .test_contract import published_contract


class PublishedResponses:
    """从实际路径查对应发布操作，不用手写成功JSON替代验收。"""

    def __init__(self) -> None:
        """加载当前完整契约并累计已覆盖F3操作。"""
        self.contract = published_contract()
        self.checked: set[str] = set()

    def validate(self, method: str, path: str, status: int, value: Any) -> None:
        """路径、状态码与完整组件共同约束实际响应。"""
        routes = self.contract["paths"]
        template = next(route for route in routes if re.fullmatch(
            re.sub(r"\{[^}]+\}","[^/]+",route),path))
        operation = routes[template][method.lower()]
        response = operation["responses"][str(status)]
        if "$ref" in response:
            response = self.contract["components"]["responses"][response["$ref"].rsplit("/",1)[-1]]
        schema = response["content"]["application/json"]["schema"]
        jsonschema.Draft4Validator({**schema,"components":self.contract["components"]}).validate(value)
        if operation.get("x-delivery-phase") == "F3":
            self.checked.add(operation["operationId"])
