"""隔离验收汇总必须依据实际JUnit，不得遗漏模块或吞掉跳过。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def runner():
    """导入仓库固定验收入口，缺实现时明确失败。"""
    path = Path(__file__).resolve().parents[3] / "scripts/acceptance/finance_runner.py"
    spec = importlib.util.spec_from_file_location("finance_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def junit(tmp_path: Path, case: str) -> Path:
    """写入与pytest相同结构的测试结果。"""
    p = tmp_path / "result.xml"
    p.write_text('<testsuites><testsuite name="pytest">' + case + "</testsuite></testsuites>")
    return p


def test_real_cases_not_untrusted_suite_totals(tmp_path: Path) -> None:
    """汇总实际case而非伪造testsuite总数，并确认固定模块实际执行。"""
    module = "packages/xuanpu-finance/tests/test_worker.py"
    p = junit(
        tmp_path,
        '<testcase classname="packages.xuanpu-finance.tests.test_worker" name="test_original"/>',
    )
    result = runner().summarize(p, returncode=0, expected_modules=[module], suite="quick")
    assert result["status"] == "passed" and result["tests"] == 1
    assert result["production_status"] == "not_verified"


@pytest.mark.parametrize(
    "case,returncode",
    [
        ("", 0),
        ('<testcase classname="other" name="x"/>', 0),
        (
            '<testcase classname="packages.xuanpu-finance.tests.test_worker" name="x"><failure message="actual failure"/></testcase>',
            0,
        ),
        (
            '<testcase classname="packages.xuanpu-finance.tests.test_worker" name="x"><skipped/></testcase>',
            0,
        ),
        ('<testcase classname="packages.xuanpu-finance.tests.test_worker" name="x"/>', 2),
    ],
)
def test_empty_missing_failed_skip_and_process_failure_never_pass(
    tmp_path: Path, case: str, returncode: int
) -> None:
    """进程和case必须同时真实通过，财务skip不能当验收。"""
    result = runner().summarize(
        junit(tmp_path, case),
        returncode=returncode,
        expected_modules=["packages/xuanpu-finance/tests/test_worker.py"],
        suite="quick",
    )
    assert result["status"] == "failed"


def test_nonfinance_full_skip_is_explicit_partial(tmp_path: Path) -> None:
    """既有非财务skip单列，不伪装为完整通过。"""
    p = junit(
        tmp_path,
        '<testcase classname="packages.xuanpu-server.tests.workspace.test_store" name="db"><skipped message="only PG"/></testcase>',
    )
    result = runner().summarize(p, returncode=0, expected_modules=[], suite="full")
    assert result["status"] == "partial" and result["skipped"] == 1


def test_missing_and_malformed_junit_fail_closed(tmp_path: Path) -> None:
    """没有真实结果或XML损坏时返回固定失败码。"""
    for p in [tmp_path / "missing.xml", junit(tmp_path, "<broken>")]:
        result = runner().summarize(p, returncode=0, expected_modules=[], suite="quick")
        assert result["status"] == "failed"


def test_runner_rejects_remote_production_and_ambiguous_dsn() -> None:
    """脚本仅用于本机明确的finance_test独立库，不可变任意渠道工具。"""
    for dsn in [
        "postgresql://u:p@remote.example/finance_test",
        "postgresql://u:p@127.0.0.1/production",
        "postgresql://u:p@127.0.0.1/finance_test?host=remote.example",
        "postgresql:///finance_test",
    ]:
        with pytest.raises(ValueError, match="TEST_DSN_REQUIRED"):
            runner().validate_dsn(dsn)
    runner().validate_dsn("postgresql://u:p@127.0.0.1:5432/finance_test_demo")


async def test_real_test_process_failure_redacts_all_evidence(tmp_path: Path, monkeypatch) -> None:
    """真实pytest子进程失败的日志/XML不泄露连接字符串或密码。"""
    import json

    module = runner()

    async def checked(dsn):
        """此用例只测真实子进程；数据库校验由独立两库演练覆盖。"""
        module.validate_dsn(dsn)

    monkeypatch.setattr(module, "check_database", checked)
    monkeypatch.setattr(
        module, "source_state", lambda root: {"head": "unit", "worktree_sha256": "unit"}
    )
    monkeypatch.setattr(module, "QUICK_MODULES", ["test_probe.py"])
    (tmp_path / "test_probe.py").write_text(
        'import os\ndef test_failure():\n assert False, os.environ["FINANCE_TEST_DSN"]\n'
    )
    dsn = "postgresql://u:private_password_123@127.0.0.1:5432/finance_test"
    key = tmp_path / "dsn"
    key.write_text(dsn)
    key.chmod(0o600)
    result = await module.run_acceptance(
        root=tmp_path, dsn_file=key, output=tmp_path / "evidence", suite="quick", timeout=20
    )
    assert result["status"] == "failed" and result["failures"] == 1
    for p in (tmp_path / "evidence").iterdir():
        assert dsn not in p.read_text() and "private_password_123" not in p.read_text()
    saved = json.loads((tmp_path / "evidence/report.json").read_text())
    assert saved["evidence_sha256"]["junit.xml"] and saved["returncode"] != 0


async def test_timeout_terminates_actual_process_and_saves_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """期限终止真实长测子进程，保留失败证据并提示核对临时资源。"""
    module = runner()

    async def checked(dsn):
        """只隔离本测试不需要连接的数据库前置。"""
        module.validate_dsn(dsn)

    monkeypatch.setattr(module, "check_database", checked)
    monkeypatch.setattr(
        module, "source_state", lambda root: {"head": "unit", "worktree_sha256": "unit"}
    )
    monkeypatch.setattr(module, "QUICK_MODULES", ["test_probe.py"])
    (tmp_path / "test_probe.py").write_text("import time\ndef test_wait():\n time.sleep(120)\n")
    key = tmp_path / "dsn"
    key.write_text("postgresql://u:private@127.0.0.1:5432/finance_test")
    key.chmod(0o600)
    result = await module.run_acceptance(
        root=tmp_path, dsn_file=key, output=tmp_path / "evidence", suite="quick", timeout=1
    )
    assert result["status"] == "failed" and result["diagnostic"] == "ACCEPTANCE_TIMEOUT"
    assert result["cleanup_required"] and (tmp_path / "evidence/report.json").exists()


def test_cli_invalid_resource_is_redacted_and_does_not_create_report(tmp_path: Path) -> None:
    """命令行坏输入固定失败，既不连接远端也不泄露路径和正文。"""
    import subprocess
    import sys
    root=Path(__file__).resolve().parents[3]
    key=tmp_path/'sensitive-secret-path'
    key.write_text('postgresql://u:private_password@remote.example/finance_test');key.chmod(0o600)
    result=subprocess.run([sys.executable,str(root/'scripts/finance_acceptance.py'),
        '--dsn-file',str(key),'--output',str(tmp_path/'evidence')],capture_output=True,text=True,check=False)
    assert result.returncode==2 and 'ACCEPTANCE_INPUT_OR_RESOURCE_INVALID' in result.stdout
    assert 'sensitive-secret-path' not in result.stdout and 'private_password' not in result.stdout
    assert not result.stderr and not (tmp_path/'evidence').exists()


def test_entity_expansion_junit_is_rejected(tmp_path: Path) -> None:
    """JUnit不是任意XML读取入口，禁止外部实体展开。"""
    p=tmp_path/'attack.xml';p.write_text('<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuites>&x;</testsuites>')
    assert runner().summarize(p,returncode=0,expected_modules=[],suite='full')['status']=='failed'


def test_inherited_database_and_pytest_selection_are_removed(monkeypatch) -> None:
    """遗留平台库地址和测试选择参数不能绕过显式隔离DSN。"""
    monkeypatch.setenv('XUANPU_TEST_PG_DSN','postgresql://private-platform')
    monkeypatch.setenv('PGHOST','private-platform')
    monkeypatch.setenv('PYTEST_PLUGINS','untrusted_plugin')
    env=runner().child_environment('explicit-test-dsn')
    assert 'XUANPU_TEST_PG_DSN' not in env and 'PGHOST' not in env
    assert 'PYTEST_PLUGINS' not in env and env['FINANCE_TEST_DSN']=='explicit-test-dsn'


async def test_exited_group_leader_does_not_leave_pipe_descendant(tmp_path: Path,monkeypatch) -> None:
    """主pytest已退出但后代仍持stdout时，期限仍终止进程组并返回。"""
    import asyncio
    import os
    import signal
    module=runner()
    async def checked(dsn):
        """只检查输入，避免本子进程机制测试依赖数据库。"""
        module.validate_dsn(dsn)
    monkeypatch.setattr(module,'check_database',checked)
    monkeypatch.setattr(module,'source_state',lambda root:{'head':'unit','worktree_sha256':'unit'})
    monkeypatch.setattr(module,'QUICK_MODULES',['test_probe.py'])
    (tmp_path/'test_probe.py').write_text('import subprocess,sys\nfrom pathlib import Path\ndef test_orphan(capfd):\n with capfd.disabled():\n  p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"])\n  Path("descendant.pid").write_text(str(p.pid))\n')
    key=tmp_path/'dsn';key.write_text('postgresql://u:private@127.0.0.1:5432/finance_test');key.chmod(0o600)
    child_pid=None
    try:
        result=await asyncio.wait_for(module.run_acceptance(root=tmp_path,dsn_file=key,
            output=tmp_path/'evidence',suite='quick',timeout=1),8)
        child_pid=int((tmp_path/'descendant.pid').read_text())
        assert result['diagnostic']=='ACCEPTANCE_TIMEOUT' and result['status']=='failed'
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid,0)
    finally:
        if child_pid is None and (tmp_path/'descendant.pid').exists():
            child_pid=int((tmp_path/'descendant.pid').read_text())
        if child_pid is not None:
            try:
                os.kill(child_pid,signal.SIGKILL)
            except ProcessLookupError:
                child_pid=None
