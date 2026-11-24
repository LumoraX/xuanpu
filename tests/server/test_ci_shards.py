"""CI 全量测试分片的覆盖核对：各分片收集到的用例并集等于全量，且两两不相交。

分片定义只写在 ``.github/workflows/ci.yml`` 的 ``tests`` 任务 matrix 里，这里直接解析它，
对全量与每个分片各做一次 ``pytest --collect-only``，按用例 ID 比对。
任何分片漏掉的用例、重复进两个分片的用例都会让本测试失败。
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

# 仓库根：本文件位于 tests/server/ 下
REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
# 全量命令不带路径参数，等价于 pyproject 里 testpaths 指定的范围
FULL_ARGS: tuple[str, ...] = ()


def _load_tests_job() -> dict[str, Any]:
    """读出 CI 工作流里的全量测试任务定义。"""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job: dict[str, Any] = workflow["jobs"]["tests"]
    return job


def _shards() -> list[tuple[str, list[str]]]:
    """按 matrix 声明顺序返回 (分片名, pytest 参数) 列表。"""
    shards = _load_tests_job()["strategy"]["matrix"]["shard"]
    return [(str(item["name"]), shlex.split(str(item["args"]))) for item in shards]


def _collect(args: tuple[str, ...] | list[str]) -> list[str]:
    """在仓库根对给定参数只做收集，返回用例 ID 列表；收集出错直接失败而不是当作空集。"""
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    result = subprocess.run(  # noqa: S603 - 参数全部来自本仓库的 CI 定义
        [*command, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"收集失败 args={args}\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
    )
    # -q 收集模式每行一个用例 ID；统计与警告行不以 tests/ 开头
    return [line for line in result.stdout.splitlines() if line.startswith("tests/")]


def test_shard_run_step_keeps_full_suite_flags() -> None:
    """每个分片的运行步骤仍带 --durations 与 faulthandler_timeout，并且确实套用了分片参数。"""
    steps = _load_tests_job()["steps"]
    runs = [str(step.get("run", "")) for step in steps if "全量测试" in str(step.get("name", ""))]
    assert len(runs) == 1, runs
    command = runs[0]
    assert "--durations=20" in command
    assert "-o faulthandler_timeout=120" in command
    assert command.rstrip().endswith("${{ matrix.shard.args }}")


def test_shards_cover_full_suite_exactly_once() -> None:
    """全部分片的用例并集等于全量，且没有用例同时落在两个分片。"""
    shards = _shards()
    assert len(shards) >= 2, shards
    names = [name for name, _ in shards]
    assert len(set(names)) == len(names), f"分片名重复：{names}"
    # 五次收集互不依赖，并发执行缩短耗时
    with ThreadPoolExecutor(max_workers=len(shards) + 1) as pool:
        full_future = pool.submit(_collect, FULL_ARGS)
        shard_futures = {name: pool.submit(_collect, args) for name, args in shards}
        full = full_future.result()
        collected = {name: future.result() for name, future in shard_futures.items()}

    assert full, "全量收集为空"
    assert len(set(full)) == len(full), "全量里有重复的用例 ID，无法按 ID 比对"
    owner: dict[str, str] = {}
    duplicated: list[str] = []
    for name, ids in collected.items():
        assert ids, f"分片 {name} 收集为空"
        for test_id in ids:
            if test_id in owner:
                duplicated.append(f"{test_id}（{owner[test_id]} 与 {name}）")
            owner[test_id] = name
    missing = sorted(set(full) - owner.keys())
    extra = sorted(owner.keys() - set(full))
    assert not duplicated, "用例落进多个分片：\n" + "\n".join(duplicated[:50])
    assert not missing, f"{len(missing)} 个用例不在任何分片：\n" + "\n".join(missing[:50])
    assert not extra, f"{len(extra)} 个分片用例不在全量里：\n" + "\n".join(extra[:50])
