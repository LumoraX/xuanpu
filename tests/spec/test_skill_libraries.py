"""分类技能目录、共享技能库（exports / uses）与渐进检索配置的规范校验。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from spec import ManifestError, parse_manifest, validate_package
from spec.skills import scan_skills

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, text: str) -> None:
    """写测试文件并自动建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_skills_supports_nested_categories_and_ignores_references(tmp_path: Path) -> None:
    """支持按分类嵌套目录，自动派生 category，且不向下扫描 skill 内部的 references/。"""
    skills_dir = tmp_path / "skills"
    _write(
        skills_dir / "geo" / "location" / "SKILL.md",
        "---\nname: location\ndescription: 当前定位\ntags: [定位, GPS]\n---\n正文\n",
    )
    _write(
        skills_dir / "geo" / "location" / "references" / "nested" / "SKILL.md",
        "---\nname: should-ignore\ndescription: 不应扫到\n---\n",
    )
    _write(
        skills_dir / "environment" / "weather" / "SKILL.md",
        "---\nname: weather\ncategory: env/weather\ndescription: 天气查询\n---\n正文\n",
    )
    _write(
        skills_dir / "flat-skill" / "SKILL.md",
        "---\nname: flat-skill\ndescription: 平铺技能\n---\n正文\n",
    )

    scanned = {item.skill_id: item for item in scan_skills(skills_dir)}
    assert set(scanned) == {"location", "weather", "flat-skill"}
    assert scanned["location"].category == "geo"
    assert scanned["location"].rel_path == "geo/location"
    assert scanned["location"].tags == ("定位", "GPS")
    assert scanned["weather"].category == "env/weather"
    assert scanned["flat-skill"].category is None


def test_library_manifest_and_recall_bounds() -> None:
    """纯共享技能库可省略 entries 只声明 exports；两者皆空或 topK 倒挂被拒绝。"""
    lib = parse_manifest(
        "apiVersion: xuanpu/v1\n"
        "metadata: {name: common-skills, version: 1.0.0}\n"
        "spec:\n"
        "  skills:\n"
        "    exports: [location, weather]\n"
        "    recall: {threshold: 5, defaultTopK: 4, maxTopK: 10}\n"
    )
    assert lib.is_library
    assert lib.spec.skills.exports == ("location", "weather")
    assert lib.spec.skills.recall.threshold == 5

    with pytest.raises(ManifestError, match="spec.skills.entries"):
        parse_manifest(
            "apiVersion: xuanpu/v1\n"
            "metadata: {name: empty-skills, version: 1.0.0}\n"
            "spec: {skills: {entries: []}}\n"
        )

    with pytest.raises(ManifestError, match="maxTopK"):
        parse_manifest(
            "apiVersion: xuanpu/v1\n"
            "metadata: {name: bad-recall, version: 1.0.0}\n"
            "spec: {skills: {entries: [main], recall: {defaultTopK: 10, maxTopK: 5}}}\n"
        )


def test_cross_package_library_validation_and_conflicts(tmp_path: Path) -> None:
    """提供 libraries 映射时，校验导出项、分类筛选、重名冲突与缺失 child_skills。"""
    lib_dir = tmp_path / "common-skills"
    _write(
        lib_dir / "xuanpu.yaml",
        "apiVersion: xuanpu/v1\n"
        "metadata: {name: common-skills, version: 1.0.0}\n"
        "spec:\n"
        "  skills:\n"
        "    exports: [location, weather]\n",
    )
    _write(
        lib_dir / "skills" / "geo" / "location" / "SKILL.md",
        "---\nname: location\ndescription: 定位\ntype: atomic\n---\n正文\n",
    )
    _write(
        lib_dir / "skills" / "environment" / "weather" / "SKILL.md",
        "---\nname: weather\ndescription: 天气\ntype: atomic\n---\n正文\n",
    )
    lib_report = validate_package(lib_dir)
    assert lib_report.ok and lib_report.warnings == ()

    artisan_dir = tmp_path / "travel-planner"
    _write(
        artisan_dir / "xuanpu.yaml",
        "apiVersion: xuanpu/v1\n"
        "metadata: {name: travel-planner, version: 1.0.0}\n"
        "spec:\n"
        "  skills:\n"
        "    entries: [trip-planner]\n"
        "    uses:\n"
        "      - package: common-skills\n"
        "        categories: [geo, environment]\n"
        "  evals:\n"
        "    - name: smoke\n"
        "      entry: trip-planner\n"
        "      input: 规划路线\n",
    )
    _write(
        artisan_dir / "skills" / "travel" / "trip-planner" / "SKILL.md",
        "---\nname: trip-planner\ndescription: 出行规划\ntype: composite\nentry: true\n"
        "child_skills: [location, weather]\n---\n正文\n",
    )
    ok_report = validate_package(artisan_dir, libraries={"common-skills": lib_dir})
    assert ok_report.ok and ok_report.warnings == (), (ok_report.errors, ok_report.warnings)

    # 制造重名冲突：本地再放一个同名 weather skill
    _write(
        artisan_dir / "skills" / "local" / "weather" / "SKILL.md",
        "---\nname: weather\ndescription: 本地同名天气\ntype: atomic\n---\n正文\n",
    )
    conflict = validate_package(artisan_dir, libraries={"common-skills": lib_dir})
    assert not conflict.ok
    assert any("重名" in err for err in conflict.errors)

    # 自引用拒绝
    self_ref = tmp_path / "self-ref"
    _write(
        self_ref / "xuanpu.yaml",
        "apiVersion: xuanpu/v1\n"
        "metadata: {name: self-ref, version: 1.0.0}\n"
        "spec: {skills: {entries: [a], uses: [{package: self-ref}]}}\n",
    )
    _write(
        self_ref / "skills" / "a" / "SKILL.md",
        "---\nname: a\ndescription: 自引用\ntype: composite\nentry: true\n---\n正文\n",
    )
    self_report = validate_package(self_ref)
    assert not self_report.ok
    assert any("不能引用业务包自身" in err for err in self_report.errors)
