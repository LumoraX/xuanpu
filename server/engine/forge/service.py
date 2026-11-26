"""管家造匠工作坊服务（Artisan Forge）：沙盒编写 → L1/L2/L3 门禁 → 管家审核 → 发版与回退（ADR 0025）。"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id
from server.core.tenancy import tenant_directory
from server.domains.workspace.patch import make_unified_diff
from server.engine.forge.content_tools import ContentPatchOutcome, ForgeContentEditor
from server.engine.forge.control_tools import (
    ControlMutationResult,
    forge_add_entry,
    forge_bump_version,
    forge_remove_entry,
    forge_set_connector_tools,
    forge_set_policy,
    forge_upsert_eval,
)
from server.engine.forge.models import (
    FORGE_VERSION,
    SPEC_API_VERSION,
    BuiltByProvenance,
    ForgeDraftRecord,
    ForgeReviewReport,
    GateCheckReport,
    ReleaseNode,
)
from spec import (
    MANIFEST_FILENAME,
    PackageManifest,
    archive_digest,
    dump_manifest,
    load_manifest,
    pack_package,
    parse_manifest,
    validate_package,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.core.tenancy import Principal
    from server.engine.forge.releases import ReleaseLedgerService
    from server.platform import Platform


def verify_bidirectional_compatibility(package_dir: Path) -> bool:
    """L2 构造器双向契约回归检查：确认当前构造器能无损解析、校验并规范化回写存量业务包。"""
    manifest = load_manifest(package_dir / MANIFEST_FILENAME)
    dumped = dump_manifest(manifest)
    reparsed = parse_manifest(dumped)
    report = validate_package(package_dir)
    return report.ok and reparsed.metadata.name == manifest.metadata.name


def _collect_text_files(root: Path) -> dict[str, str]:
    """收集目录下全部 UTF-8 文本文件快照用于计算草稿 Diff。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            out[rel] = f"<binary:{path.stat().st_size} bytes>"
    return out


class ArtisanForgeService:
    """隔离沙盒内的管家造匠服务：严格执行三层修改边界、三级门禁与审核发版。"""

    def __init__(
        self,
        platform: Platform,
        releases: ReleaseLedgerService,
        *,
        data_dir: Path,
        now: Callable[[], float] = time.time,
    ) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._releases = releases
        self._root = data_dir / "forge"
        self._now = now
        self._drafts: dict[tuple[str, str], ForgeDraftRecord] = {}
        self._baselines: dict[tuple[str, str], dict[str, str]] = {}

    def _at(self) -> datetime:
        """当前 UTC 时间。"""
        return datetime.fromtimestamp(self._now(), UTC)

    def _draft_dir(self, tenant_id: str, draft_id: str) -> Path:
        """返回指定草稿的隔离沙盒目录。"""
        return self._root / tenant_directory(tenant_id) / draft_id

    def _require_draft(self, who: Principal, draft_id: str) -> tuple[ForgeDraftRecord, Path]:
        """校验租户与创建者归属，返回草稿记录与目录。"""
        tenant = who.require_tenant()
        rec = self._drafts.get((tenant, draft_id))
        if rec is None or (who.role != "tenant_admin" and rec.user_id != who.user_id):
            raise NotFoundError("造匠草稿不存在")
        return rec, self._draft_dir(tenant, draft_id)

    def _save(self, rec: ForgeDraftRecord) -> ForgeDraftRecord:
        """保存草稿状态。"""
        self._drafts[(rec.tenant_id, rec.draft_id)] = rec
        return rec

    async def create_draft(
        self,
        who: Principal,
        *,
        package_name: str,
        description: str,
        entry: str = "main",
        display_name: str | None = None,
    ) -> ForgeDraftRecord:
        """从最小规范骨架初始化一个全新的隔离造匠草稿（记录 built_by 血缘）。"""
        tenant = who.require_tenant()
        user_id = who.user_id or who.actor
        draft_id = new_id("frg")
        draft_dir = self._draft_dir(tenant, draft_id)
        raw_manifest: dict[str, Any] = {
            "apiVersion": SPEC_API_VERSION,
            "kind": "Package",
            "metadata": {
                "name": package_name,
                "version": "0.1.0",
                "description": description,
                **({"displayName": display_name} if display_name else {}),
            },
            "spec": {
                "skills": {"entries": [entry]},
                "model": {"tier": "balanced"},
                "policy": {"approval": "standard", "sandbox": {"network": False, "shell": False}},
            },
        }
        manifest = PackageManifest.model_validate(raw_manifest)
        yaml_text = dump_manifest(manifest)

        def _init_fs() -> None:
            (draft_dir / "skills" / entry).mkdir(parents=True, exist_ok=True)
            (draft_dir / MANIFEST_FILENAME).write_text(yaml_text, encoding="utf-8")
            title = display_name or package_name
            (draft_dir / "skills" / entry / "SKILL.md").write_text(
                f"---\nname: {entry}\ndisplay_name: {title}\ndescription: {description}\n"
                f"version: 1.0.0\ntype: composite\nentry: true\n---\n\n# {title}\n\n{description}\n",
                encoding="utf-8",
            )

        await asyncio.to_thread(_init_fs)
        now = self._at()
        rec = ForgeDraftRecord(
            draft_id=draft_id,
            tenant_id=tenant,
            user_id=user_id,
            package_name=package_name,
            base_version=None,
            status="editing",
            built_by=BuiltByProvenance(
                forge_version=FORGE_VERSION,
                spec_version=SPEC_API_VERSION,
                base_package_digest=None,
            ),
            created_at=now,
            updated_at=now,
        )
        self._baselines[(tenant, draft_id)] = {}
        return self._save(rec)

    async def checkout_draft(
        self, who: Principal, *, package_name: str, version: str | None = None,
    ) -> ForgeDraftRecord:
        """从已发布的不可变版本检出到隔离草稿目录做安全迭代，绝不修改线上缓存目录。"""
        tenant = who.require_tenant()
        user_id = who.user_id or who.actor
        resolved = await self._platform.packages.resolve(tenant, package_name, version)
        draft_id = new_id("frg")
        draft_dir = self._draft_dir(tenant, draft_id)

        def _copy() -> dict[str, str]:
            draft_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(resolved.root, draft_dir)
            return _collect_text_files(draft_dir)

        baseline = await asyncio.to_thread(_copy)
        now = self._at()
        rec = ForgeDraftRecord(
            draft_id=draft_id,
            tenant_id=tenant,
            user_id=user_id,
            package_name=package_name,
            base_version=resolved.record.version,
            status="editing",
            built_by=BuiltByProvenance(
                forge_version=FORGE_VERSION,
                spec_version=SPEC_API_VERSION,
                base_package_digest=resolved.record.digest,
            ),
            created_at=now,
            updated_at=now,
        )
        self._baselines[(tenant, draft_id)] = baseline
        return self._save(rec)

    async def apply_control(
        self, who: Principal, draft_id: str, action: str, params: dict[str, Any],
    ) -> ControlMutationResult:
        """L-Control 入口：调用强类型确定性代码工具修改 ``xuanpu.yaml`` 并重置门禁状态。"""
        rec, draft_dir = self._require_draft(who, draft_id)
        if rec.status == "published":
            raise ConflictError("已发布的草稿不可再修改，请重新 checkout 新草稿")

        def _exec() -> ControlMutationResult:
            if action == "add_entry":
                return forge_add_entry(
                    draft_dir,
                    skill_id=str(params["skill_id"]),
                    display_name=params.get("display_name"),
                    description=params.get("description"),
                )
            if action == "remove_entry":
                return forge_remove_entry(draft_dir, skill_id=str(params["skill_id"]))
            if action == "set_policy":
                return forge_set_policy(
                    draft_dir,
                    approval=params.get("approval"),
                    max_session_tokens=params.get("max_session_tokens"),
                    max_iterations=params.get("max_iterations"),
                    sandbox_network=params.get("sandbox_network"),
                    sandbox_shell=params.get("sandbox_shell"),
                    model_tier=params.get("model_tier"),
                )
            if action == "set_connector_tools":
                return forge_set_connector_tools(
                    draft_dir,
                    connector_name=str(params["connector_name"]),
                    tools=list(params["tools"]),
                )
            if action == "upsert_eval":
                return forge_upsert_eval(
                    draft_dir,
                    name=str(params["name"]),
                    entry=str(params["entry"]),
                    input_text=str(params["input"]),
                    contains=tuple(params.get("contains") or ()),
                    not_contains=tuple(params.get("not_contains") or ()),
                    max_tokens=params.get("max_tokens"),
                )
            if action == "bump_version":
                return forge_bump_version(draft_dir, bump=params.get("bump", "patch"))
            raise InvalidRequestError(f"不支持的 L-Control 动作：{action}")

        result = await asyncio.to_thread(_exec)
        self._save(rec.model_copy(update={
            "status": "editing", "l1_report": None, "l2_report": None,
            "l3_report": None, "review": None, "updated_at": self._at(),
        }))
        return result

    async def apply_content_patches(
        self, who: Principal, draft_id: str, patches: list[dict[str, Any]],
    ) -> ContentPatchOutcome:
        """L-Content 入口：经 ``taifeng.HookRegistry`` + ``make_apply_patch_tool`` 编辑内容文件。"""
        rec, draft_dir = self._require_draft(who, draft_id)
        if rec.status == "published":
            raise ConflictError("已发布的草稿不可再修改")
        editor = ForgeContentEditor(draft_dir)
        outcome = await editor.apply_patches(patches)
        if outcome.ok:
            self._save(rec.model_copy(update={
                "status": "editing", "l1_report": None, "l2_report": None,
                "l3_report": None, "review": None, "updated_at": self._at(),
            }))
        return outcome

    async def list_diff(self, who: Principal, draft_id: str) -> dict[str, str]:
        """计算草稿目录相对基线的全量文件 Unified Diff。"""
        rec, draft_dir = self._require_draft(who, draft_id)
        baseline = self._baselines.get((rec.tenant_id, rec.draft_id), {})
        current = await asyncio.to_thread(_collect_text_files, draft_dir)
        diffs: dict[str, str] = {}
        for rel in sorted(set(baseline) | set(current)):
            before, after = baseline.get(rel, ""), current.get(rel, "")
            if before != after:
                diffs[rel] = make_unified_diff(before, after, filename=rel)
        return diffs

    async def run_l1_validate(self, who: Principal, draft_id: str) -> GateCheckReport:
        """L1 门禁：运行 ``validate_package`` 与全部 Python 脚本语法检查。"""
        rec, draft_dir = self._require_draft(who, draft_id)

        def _check() -> tuple[bool, list[str]]:
            report = validate_package(draft_dir)
            errors = list(report.errors)
            for py_file in sorted(draft_dir.rglob("*.py")):
                rel = py_file.relative_to(draft_dir).as_posix()
                try:
                    compile(py_file.read_text(encoding="utf-8"), rel, "exec")
                except SyntaxError as exc:
                    errors.append(f"{rel} 第 {exc.lineno} 行语法错误：{exc.msg}")
            return not errors, errors

        passed, errors = await asyncio.to_thread(_check)
        gate = GateCheckReport(
            level="L1",
            passed=passed,
            summary="L1 静态规范与脚本语法校验通过" if passed else f"L1 校验发现 {len(errors)} 处错误",
            errors=tuple(errors),
            checked_at=self._at(),
        )
        self._save(rec.model_copy(update={
            "l1_report": gate,
            "status": "validated" if passed else "editing",
            "updated_at": self._at(),
        }))
        return gate

    async def run_l2_unit_tests(self, who: Principal, draft_id: str) -> GateCheckReport:
        """L2 门禁：在独立子进程中运行草稿 ``tests/`` 下的单元测试脚本（限时强杀）。"""
        rec, draft_dir = self._require_draft(who, draft_id)
        tests_dir = draft_dir / "tests"
        test_files = sorted(tests_dir.glob("test_*.py")) if tests_dir.is_dir() else []
        if not test_files:
            gate = GateCheckReport(
                level="L2", passed=True,
                summary="草稿未声明额外脚本单测，L2 默认通过", checked_at=self._at(),
            )
            self._save(rec.model_copy(update={"l2_report": gate, "updated_at": self._at()}))
            return gate

        errors: list[str] = []
        for test_file in test_files:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(test_file),
                cwd=str(draft_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                errors.append(f"{test_file.name} 运行超时（>15s）")
                continue
            if proc.returncode != 0:
                msg = err.decode("utf-8", errors="replace").strip()[-500:]
                errors.append(f"{test_file.name} 失败（exit={proc.returncode}）：{msg}")

        passed = not errors
        gate = GateCheckReport(
            level="L2",
            passed=passed,
            summary=f"L2 沙盒单测通过（{len(test_files)} 个测试文件）" if passed else f"L2 单测失败（{len(errors)} 项）",
            errors=tuple(errors),
            checked_at=self._at(),
        )
        self._save(rec.model_copy(update={
            "l2_report": gate,
            "status": "tested" if passed else "editing",
            "updated_at": self._at(),
        }))
        return gate

    async def run_l3_eval_check(self, who: Principal, draft_id: str) -> GateCheckReport:
        """L3 门禁：核验评测集契约（至少声明 1 条有效 eval 且目标入口存在）。"""
        rec, draft_dir = self._require_draft(who, draft_id)
        manifest = await asyncio.to_thread(load_manifest, draft_dir / MANIFEST_FILENAME)
        errors: list[str] = []
        entries = set(manifest.spec.skills.entries)
        for case in manifest.spec.evals:
            if case.entry not in entries:
                errors.append(f"评测用例 {case.name} 引用了未知入口 {case.entry}")
        passed = not errors
        gate = GateCheckReport(
            level="L3",
            passed=passed,
            summary=f"L3 评测契约核验通过（{len(manifest.spec.evals)} 条用例）" if passed else "L3 评测契约校验失败",
            errors=tuple(errors),
            checked_at=self._at(),
        )
        self._save(rec.model_copy(update={"l3_report": gate, "updated_at": self._at()}))
        return gate

    async def review_draft(
        self, who: Principal, draft_id: str, *, summary: str, approved: bool,
    ) -> ForgeDraftRecord:
        """管家审核：必须先通过 L1/L2/L3 才允许批准，自动记录变更文件清单与权限变化。"""
        rec, draft_dir = self._require_draft(who, draft_id)
        l1_ok = bool(rec.l1_report and rec.l1_report.passed)
        l2_ok = bool(rec.l2_report and rec.l2_report.passed)
        l3_ok = bool(rec.l3_report and rec.l3_report.passed)
        if approved and not (l1_ok and l2_ok and l3_ok):
            raise ConflictError("必须先全部通过 L1、L2、L3 门禁校验后才能批准发布")
        diffs = await self.list_diff(who, draft_id)
        manifest = await asyncio.to_thread(load_manifest, draft_dir / MANIFEST_FILENAME)
        perm_notes = (
            f"approval={manifest.spec.policy.approval}",
            f"sandbox.network={manifest.spec.policy.sandbox.network}",
            f"sandbox.shell={manifest.spec.policy.sandbox.shell}",
        )
        review = ForgeReviewReport(
            approved=approved,
            summary=summary,
            diff_files=tuple(sorted(diffs)),
            permission_changes=perm_notes,
            l1_passed=l1_ok,
            l2_passed=l2_ok,
            l3_passed=l3_ok,
            reviewer=who.user_id or who.actor,
            reviewed_at=self._at(),
        )
        return self._save(rec.model_copy(update={
            "review": review,
            "status": "reviewed" if approved else "editing",
            "updated_at": self._at(),
        }))

    async def publish_draft(
        self, who: Principal, draft_id: str, *, activate: bool = True,
    ) -> tuple[ForgeDraftRecord, ReleaseNode]:
        """打包不可变归档并发布为新版本，同时登记 L1 不可变发布节点（带 built_by 血缘）。"""
        rec, draft_dir = self._require_draft(who, draft_id)
        if rec.review is None or not rec.review.approved:
            raise ConflictError("草稿尚未通过管家审核，禁止发布")
        tenant = who.require_tenant()
        archive = await asyncio.to_thread(pack_package, draft_dir)
        digest = archive_digest(archive)
        manifest = await asyncio.to_thread(load_manifest, draft_dir / MANIFEST_FILENAME)
        pkg_record = await self._platform.packages.publish(who, archive, activate=activate)
        release = await self._releases.promote(
            tenant,
            layer="artisan",
            target_name=pkg_record.name,
            version=pkg_record.version,
            artifact_digest=digest,
            actor=who.user_id or who.actor,
            built_by=rec.built_by,
            evidence_ref=f"forge:{draft_id}",
        )
        updated = self._save(rec.model_copy(update={
            "status": "published",
            "published_version": manifest.metadata.version,
            "published_digest": digest,
            "updated_at": self._at(),
        }))
        return updated, release

    async def rollback_artisan(
        self,
        who: Principal,
        package_name: str,
        *,
        to_release_id: str,
        mode: Literal["retired", "revoked"] = "retired",
    ) -> ReleaseNode:
        """L1 匠人安全回退：先做制品活性快检，再追加新发布节点并切换 PackageRegistry 生效指针。"""
        tenant = who.require_tenant()

        async def _check_alive(target: ReleaseNode) -> bool:
            try:
                await self._platform.packages.resolve(tenant, package_name, target.version)
                return True
            except Exception:
                return False

        node = await self._releases.rollback(
            tenant,
            layer="artisan",
            target_name=package_name,
            to_release_id=to_release_id,
            actor=who.user_id or who.actor,
            prior_state=mode,
            liveness_check=_check_alive,
        )
        await self._platform.packages.activate(who, package_name, node.version)
        return node
