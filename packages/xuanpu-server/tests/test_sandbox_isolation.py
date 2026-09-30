"""沙盒隔离：业务包里的脚本与命令出不了自己的工作区，也读不到别的租户的数据。"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"), reason="本机隔离后端只支持 macOS 与 Linux"
)

PROBE = """\
import json, os, sys
out = {}
workspace = os.environ["WORKSPACE"]
targets = json.loads(sys.argv[1])
try:
    open(os.path.join(workspace, "ok.txt"), "w").write("ok")
    out["write_workspace"] = True
except OSError as exc:
    out["write_workspace"] = str(exc)
for name, path in targets.items():
    try:
        open(path, "w").write("leak")
        out["write_" + name] = True
    except OSError:
        out["write_" + name] = False
    try:
        out["read_" + name] = open(path).read()[:40]
    except OSError:
        out["read_" + name] = None
out["env"] = sorted(os.environ)
try:
    import socket
    socket.create_connection(("127.0.0.1", int(targets.get("port", "9"))), timeout=2)
    out["network"] = True
except (OSError, ValueError):
    out["network"] = False
print(json.dumps(out))
"""


async def test_script_is_confined(
    world: Any, step: Any, read_turn: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脚本只能写工作区；平台数据目录里别处的文件读不到也写不了；拿不到宿主环境变量。"""
    monkeypatch.setenv("HOST_ONLY_SECRET", "host-leak-value")
    archive_root = world.package_dir / "probe"
    world.archive()
    (world.package_dir / "1.0.0-standard-False").rename(archive_root)
    skill_dir = archive_root / "skills" / "triage"
    (skill_dir / "scripts" / "probe.py").write_text(PROBE)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text().replace(
            "scripts:\n",
            "scripts:\n"
            "  - name: probe\n"
            "    path: scripts/probe.py\n"
            "    language: python\n"
            "    description: 探测沙盒边界\n"
            "    timeout_seconds: 30\n"
            "    args_schema:\n"
            "      type: object\n"
            "      properties:\n"
            "        targets: {type: string}\n",
            1,
        )
    )

    from xuanpu_spec import pack_package

    await world.platform.packages.publish(world.admin, pack_package(archive_root), activate=True)
    session = await world.platform.sessions.create(world.member, package="support-desk")

    other_tenant_file = world.data_dir / "threads" / "globex-secret.txt"
    other_tenant_file.parent.mkdir(parents=True, exist_ok=True)
    other_tenant_file.write_text("other tenant data")
    outside = tmp_path / "outside.txt"
    outside.write_text("host data")
    package_file = (
        await world.platform.packages.resolve("acme", "support-desk")
    ).root / "xuanpu.yaml"

    targets = {
        "other_tenant": str(other_tenant_file),
        "host": str(outside),
        "package": str(package_file),
    }
    world.models.script(
        step(
            calls=[
                (
                    "c1",
                    "run_script",
                    {
                        "skill_id": "triage",
                        "script_name": "probe",
                        "args": {"targets": json.dumps(targets)},
                    },
                )
            ]
        ),
        step(text="done"),
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "探测")
    )
    completed = next(e for e in events if e.msg.kind == "tool_call_completed")
    payload = completed.msg.data["output"]
    report = json.loads(payload[payload.index("{") : payload.rindex("}") + 1])

    assert report["write_workspace"] is True
    assert report["write_other_tenant"] is False
    assert report["read_other_tenant"] is None
    assert report["write_host"] is False
    assert report["read_host"] is None
    # 自己的业务包可读不可写
    assert report["write_package"] is False
    assert report["read_package"].startswith("apiVersion")
    assert report["network"] is False
    assert "HOST_ONLY_SECRET" not in report["env"]
    assert set(report["env"]) >= {"WORKSPACE", "HOME", "TMPDIR", "PATH"}

    assert other_tenant_file.read_text() == "other tenant data"
    assert outside.read_text() == "host data"
    workspaces = list((world.data_dir / "workspaces").rglob("ok.txt"))
    assert len(workspaces) == 1
    assert workspaces[0].is_relative_to(world.data_dir / "workspaces")
