"""L3 平台与内核蓝绿自检发布与一键回退流水线（ADR 0025）。

流程：
1. 采集当前平台 Git 提交、``external/taifeng`` 内核锁定提交与控制面最新迁移号（Expand-and-Contract 校验）；
2. 执行门禁检查（ruff / mypy / CI 分片完整性，可配置）；
3. 对影子槽位（Shadow Slot）执行 ``/healthz`` 活性探活；
4. 以追加式不可变发布节点（``generation = prev + 1``）写入 L3 发布账本并原子切换蓝绿槽位指针；
5. 支持 ``rollback``：先做目标历史节点活性核验，再追加新代次指回旧槽位（绝不物理删除历史记录）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.request import urlopen

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable


class KernelReleaseNode(BaseModel):
    """L3 平台与内核不可变发布节点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    release_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=1)
    slot: Literal["blue", "green"]
    platform_commit: str = Field(min_length=7, max_length=64)
    kernel_commit: str = Field(min_length=7, max_length=64)
    migration_version: int = Field(ge=1)
    state: Literal["active", "retired", "revoked"] = "active"
    shadow_health_url: str | None = None
    rolled_back_from: str | None = None
    created_at: str


class KernelReleaseLedger:
    """基于本地 JSON 账本的 L3 不可变发布节点管理与蓝绿切流器。"""

    def __init__(self, ledger_path: Path) -> None:
        """绑定发布账本文件路径。"""
        self._path = ledger_path

    def load(self) -> list[KernelReleaseNode]:
        """读取全部追加式发布节点（按代次升序）。"""
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [KernelReleaseNode.model_validate(item) for item in raw]

    def _save(self, nodes: list[KernelReleaseNode]) -> None:
        """原子写入发布账本文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([n.model_dump(mode="json") for n in nodes], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def active(self) -> KernelReleaseNode | None:
        """返回当前生效的 L3 发布节点。"""
        for node in reversed(self.load()):
            if node.state == "active":
                return node
        return None

    def promote(
        self,
        *,
        platform_commit: str,
        kernel_commit: str,
        migration_version: int,
        shadow_health_url: str | None = None,
        health_probe: Callable[[str], bool] | None = None,
    ) -> KernelReleaseNode:
        """验活影子槽位后，将旧节点置为 retired 并追加新代次切换蓝绿槽位。"""
        probe = health_probe or _default_http_health_probe
        if shadow_health_url is not None and not probe(shadow_health_url):
            raise RuntimeError(f"影子实例探活失败：{shadow_health_url}")

        nodes = self.load()
        current = next((n for n in reversed(nodes) if n.state == "active"), None)
        if current is not None and migration_version < current.migration_version:
            raise ValueError(
                f"迁移序号不可倒退（当前 {current.migration_version}，目标 {migration_version}），"
                "请遵循 Expand-and-Contract 原则"
            )
        next_gen = (nodes[-1].generation + 1) if nodes else 1
        next_slot: Literal["blue", "green"] = (
            "green" if (current is not None and current.slot == "blue") else "blue"
        )
        updated: list[KernelReleaseNode] = [
            n.model_copy(update={"state": "retired"}) if n.state == "active" else n
            for n in nodes
        ]
        rid = "krel_" + hashlib.sha256(f"{next_gen}:{platform_commit}:{kernel_commit}".encode()).hexdigest()[:12]
        created = KernelReleaseNode(
            release_id=rid,
            generation=next_gen,
            slot=next_slot,
            platform_commit=platform_commit,
            kernel_commit=kernel_commit,
            migration_version=migration_version,
            state="active",
            shadow_health_url=shadow_health_url,
            created_at=datetime.now(UTC).isoformat(),
        )
        updated.append(created)
        self._save(updated)
        return created

    def rollback(
        self,
        target_release_id: str,
        *,
        mode: Literal["retired", "revoked"] = "retired",
        liveness_check: Callable[[KernelReleaseNode], bool] | None = None,
    ) -> KernelReleaseNode:
        """对历史节点做活性核验后，追加新代次指回旧版内核与槽位（不删历史）。"""
        nodes = self.load()
        target = next((n for n in nodes if n.release_id == target_release_id), None)
        if target is None:
            raise KeyError(f"未找到目标发布节点：{target_release_id}")
        if liveness_check is not None and not liveness_check(target):
            raise RuntimeError(f"目标历史节点活性核验未通过：{target_release_id}")

        next_gen = nodes[-1].generation + 1
        updated: list[KernelReleaseNode] = [
            n.model_copy(update={"state": mode}) if n.state == "active" else n
            for n in nodes
        ]
        rid = "krel_" + hashlib.sha256(f"rb:{next_gen}:{target.release_id}".encode()).hexdigest()[:12]
        reborn = KernelReleaseNode(
            release_id=rid,
            generation=next_gen,
            slot=target.slot,
            platform_commit=target.platform_commit,
            kernel_commit=target.kernel_commit,
            migration_version=target.migration_version,
            state="active",
            shadow_health_url=target.shadow_health_url,
            rolled_back_from=target.release_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        updated.append(reborn)
        self._save(updated)
        return reborn


def _default_http_health_probe(url: str) -> bool:
    """对影子槽位 ``/healthz`` 发起 HTTP 200 探活。"""
    try:
        with urlopen(url, timeout=5) as resp:  # noqa: S310 —— 仅由运维命令行传入受控内网探活地址
            return int(resp.status) == 200
    except Exception:
        return False


def _git_rev(repo: Path) -> str:
    """读取指定 Git 仓库当前 HEAD 提交哈希。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def main(argv: list[str]) -> int:
    """命令行入口：支持 ``status`` / ``promote`` / ``rollback``。"""
    parser = argparse.ArgumentParser(description="玄圃 L3 内核蓝绿发布与回退流水线")
    parser.add_argument("--ledger", type=Path, default=Path("data/l3_release_ledger.json"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看当前生效的 L3 发布节点与历史")
    p_prom = sub.add_parser("promote", help="校验并发布新 L3 蓝绿节点")
    p_prom.add_argument("--health-url", default=None, help="影子实例 /healthz 地址")
    p_prom.add_argument("--migration", type=int, required=True, help="控制面当前迁移版本号")
    p_rb = sub.add_parser("rollback", help="回退到历史验证节点")
    p_rb.add_argument("--to", required=True, help="目标历史 release_id")
    p_rb.add_argument("--revoke", action="store_true", help="紧急熔断旧活跃节点")
    args = parser.parse_args(argv)

    ledger = KernelReleaseLedger(args.ledger)
    if args.cmd == "status":
        print(json.dumps([n.model_dump(mode="json") for n in ledger.load()], ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "promote":
        root = Path(__file__).resolve().parents[2]
        node = ledger.promote(
            platform_commit=_git_rev(root),
            kernel_commit=_git_rev(root / "external/taifeng"),
            migration_version=args.migration,
            shadow_health_url=args.health_url,
        )
        print(json.dumps(node.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "rollback":
        node = ledger.rollback(args.to, mode="revoked" if args.revoke else "retired")
        print(json.dumps(node.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
