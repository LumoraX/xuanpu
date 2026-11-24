"""规范包测试夹具：在临时目录里搭一个合法的业务包。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MANIFEST = """\
apiVersion: xuanpu/v1
kind: Package
metadata:
  name: support-desk
  version: 1.2.0
  description: 客服工单助手
spec:
  skills:
    dir: skills
    entries: [triage]
  model:
    tier: balanced
  policy:
    approval: standard
    budget:
      maxSessionTokens: 200000
      maxIterations: 40
    sandbox:
      network: false
      shell: false
  connectors:
    - name: crm
      command: ["uvx", "crm-mcp"]
      env:
        CRM_TOKEN: {secret: crm-token}
        CRM_REGION: {value: cn}
      effect: external
      tools:
        - {name: search_customer, effect: read}
        - {name: update_ticket}
  evals:
    - name: greet
      entry: triage
      input: 你好
      expect:
        contains: ["您好"]
        maxTokens: 2000
"""

TRIAGE = """\
---
name: triage
description: 工单分诊
version: 1.0.0
type: composite
entry: true
child_skills: [faq]
tool_names: [mcp__crm__search_customer]
---
# 工单分诊
先查客户信息，再决定怎么处理。
"""

FAQ = """\
---
name: faq
description: 常见问题
version: 1.0.0
type: atomic
---
# 常见问题
退款在 7 个工作日内到账。
"""


@dataclass(frozen=True)
class Samples:
    """样例文本。工作区用 importlib 导入模式，测试模块之间不能互相 import，所以经夹具共享。"""

    manifest: str = MANIFEST
    triage: str = TRIAGE
    faq: str = FAQ


@pytest.fixture
def samples() -> Samples:
    """合法业务包的样例文本。"""
    return Samples()


@pytest.fixture
def make_package(tmp_path: Path) -> Callable[..., Path]:
    """返回一个工厂：按需覆盖清单或 skill 内容，得到一个业务包目录。"""

    def _make(
        *,
        manifest: str = MANIFEST,
        skills: dict[str, str] | None = None,
        name: str = "pkg",
    ) -> Path:
        root = tmp_path / name
        root.mkdir()
        (root / "xuanpu.yaml").write_text(manifest, encoding="utf-8")
        contents = {"triage": TRIAGE, "faq": FAQ} if skills is None else skills
        for skill, body in contents.items():
            directory = root / "skills" / skill
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(body, encoding="utf-8")
        return root

    return _make
