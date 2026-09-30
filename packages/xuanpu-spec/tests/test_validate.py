"""业务包目录的整体校验。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_spec import validate_package

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_valid_package(make_package: Callable[..., Path], samples: Any) -> None:
    """合法的业务包：没有错误。"""
    report = validate_package(make_package())
    assert report.ok, report.errors
    assert report.manifest is not None
    assert report.warnings == ()


def test_missing_manifest(tmp_path: Path, samples: Any) -> None:
    """没有清单。"""
    report = validate_package(tmp_path)
    assert not report.ok
    assert report.manifest is None
    assert "读不到清单" in report.errors[0]


def test_invalid_manifest_lists_every_problem(
    make_package: Callable[..., Path], samples: Any
) -> None:
    """清单有多处问题时逐条列出，而不是只报第一条。"""
    broken = samples.manifest.replace("version: 1.2.0", "version: one").replace(
        "tier: balanced", "tier: huge"
    )
    report = validate_package(make_package(manifest=broken))
    assert not report.ok
    assert len(report.errors) == 2
    assert any("metadata.version" in error for error in report.errors)
    assert any("spec.model.tier" in error for error in report.errors)


def test_entry_missing_from_skills_dir(make_package: Callable[..., Path], samples: Any) -> None:
    """清单声明的入口在 skill 目录里找不到。"""
    report = validate_package(make_package(skills={"faq": samples.faq}))
    assert any("入口 skill 'triage'" in error and "找不到" in error for error in report.errors)


def test_entry_not_declared_in_skill_file(make_package: Callable[..., Path], samples: Any) -> None:
    """清单把它列为入口，但 SKILL.md 自己没声明 entry: true。"""
    report = validate_package(
        make_package(
            skills={"triage": samples.triage.replace("entry: true\n", ""), "faq": samples.faq}
        )
    )
    assert any("没有声明 entry: true" in error for error in report.errors)


def test_undeclared_entry_is_a_warning(make_package: Callable[..., Path], samples: Any) -> None:
    """SKILL.md 声明了入口但清单没列：不算错，但提醒它不会对外开放。"""
    extra = samples.triage.replace("name: triage", "name: backdoor").replace(
        "tool_names: [mcp__crm__search_customer]", "tool_names: []"
    )
    report = validate_package(
        make_package(skills={"triage": samples.triage, "faq": samples.faq, "backdoor": extra})
    )
    assert report.ok
    assert any("backdoor" in warning for warning in report.warnings)


def test_empty_or_missing_skills_dir(make_package: Callable[..., Path], samples: Any) -> None:
    """skill 目录为空。"""
    report = validate_package(make_package(skills={}))
    assert any("不存在或为空" in error for error in report.errors)


def test_broken_skill_files_reported(make_package: Callable[..., Path], samples: Any) -> None:
    """SKILL.md 没有 frontmatter、没闭合、缺 name，都要报出来。"""
    report = validate_package(
        make_package(
            skills={
                "triage": samples.triage,
                "faq": samples.faq,
                "no-front": "# 只有正文\n",
                "unclosed": "---\nname: x\n",
                "no-name": "---\ndescription: 没名字\n---\n正文\n",
            }
        )
    )
    joined = "\n".join(report.errors)
    assert "'no-front'" in joined
    assert "'unclosed'" in joined
    assert "'no-name'" in joined
    assert len(report.errors) == 3


def test_duplicate_skill_ids(make_package: Callable[..., Path], samples: Any) -> None:
    """两个目录声明了同一个 skill 名。"""
    report = validate_package(
        make_package(skills={"triage": samples.triage, "faq": samples.faq, "faq-copy": samples.faq})
    )
    assert any("重复出现" in error for error in report.errors)


def test_skill_references_unknown_connector(
    make_package: Callable[..., Path], samples: Any
) -> None:
    """skill 用了清单里没有的连接器。"""
    triage = samples.triage.replace("mcp__crm__search_customer", "mcp__erp__list_orders")
    report = validate_package(make_package(skills={"triage": triage, "faq": samples.faq}))
    assert any("没有连接器 'erp'" in error for error in report.errors)


def test_skill_references_tool_outside_allowlist(
    make_package: Callable[..., Path], samples: Any
) -> None:
    """skill 用了连接器白名单之外的工具。"""
    triage = samples.triage.replace("mcp__crm__search_customer", "mcp__crm__delete_customer")
    report = validate_package(make_package(skills={"triage": triage, "faq": samples.faq}))
    assert any("工具白名单" in error for error in report.errors)


def test_skills_dir_cannot_escape_package(make_package: Callable[..., Path], samples: Any) -> None:
    """skill 目录不能指到包外面。"""
    report = validate_package(
        make_package(manifest=samples.manifest.replace("dir: skills", "dir: ../up"))
    )
    assert any("相对路径" in error for error in report.errors)


def test_permissive_policy_and_missing_evals_warn(
    make_package: Callable[..., Path], samples: Any
) -> None:
    """宽松审批叠加命令执行、没有评测用例：可以发布，但给出提醒。"""
    text = samples.manifest.replace("approval: standard", "approval: relaxed").replace(
        "shell: false", "shell: true"
    )
    text = text[: text.index("  evals:")]
    report = validate_package(make_package(manifest=text))
    assert report.ok, report.errors
    assert len(report.warnings) == 2
    assert any("relaxed" in warning for warning in report.warnings)
    assert any("评测用例" in warning for warning in report.warnings)
