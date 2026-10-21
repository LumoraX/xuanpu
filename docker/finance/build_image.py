"""从白名单目录创建镜像上下文，避免发送平台私密文件。"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    """只复制财务/规范包源码和锁定依赖，不包含运行配置与数据。"""
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="xuanpu-finance-build-") as directory:
        context = Path(directory)
        for name in ("Dockerfile", "requirements.lock"):
            shutil.copyfile(root / "docker/finance" / name, context / name)
        for name in ("xuanpu-spec", "xuanpu-finance"):
            source = root / "packages" / name
            target = context / "packages" / name
            target.mkdir(parents=True)
            for filename in ("pyproject.toml", "README.md"):
                shutil.copyfile(source / filename, target / filename)
            shutil.copytree(source / "src", target / "src",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run(["docker", "build", "-t", "xuanpu-finance:2026.10.21.5", str(context)],
                       check=True)


if __name__ == "__main__":
    main()
