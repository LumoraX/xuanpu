"""配置的解析与校验。"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from xuanpu_server.bootstrap import _sandboxes, object_store
from xuanpu_server.config import ConfigError, expand, load_config, parse_config
from xuanpu_server.objectstore import LocalObjectStore, S3ObjectStore
from xuanpu_server.runtime import DockerSandboxProvider, LocalSandboxProvider

if TYPE_CHECKING:
    from pathlib import Path

KEY = base64.b64encode(b"k" * 32).decode()

BASE: dict[str, Any] = {
    "data_dir": "/var/lib/xuanpu",
    "root_key_hash": "a" * 64,
    "master_keys": {"k1": KEY},
    "primary_master_key": "k1",
    "models": {
        "base_url": "http://gateway:4000/v1",
        "api_key": "${GATEWAY_KEY}",
        "tiers": {
            "fast": {"model": "small"},
            "balanced": {"model": "medium"},
            "deep": {"model": "large", "timeout_seconds": 600},
        },
    },
}
VARIABLES = {"GATEWAY_KEY": "sk-gateway", "PG": "postgresql://u:p@db/x"}


def edited(**changes: Any) -> str:
    """在合法配置的基础上按双下划线路径改字段；值为 ``...`` 表示删除。"""
    data = yaml.safe_load(yaml.safe_dump(BASE))
    for path, value in changes.items():
        *parents, last = path.split("__")
        node = data
        for key in parents:
            node = node.setdefault(key, {})
        if value is ...:
            del node[last]
        else:
            node[last] = value
    return yaml.safe_dump(data)


def test_minimal_config_and_defaults() -> None:
    """最小配置可用；缺省值偏保守：只监听本机、内存控制面、本机沙盒、不留提示词。"""
    config = parse_config(edited(), variables=VARIABLES)
    assert config.models.api_key == "sk-gateway"
    assert config.models.tiers["deep"].timeout_seconds == 600
    assert (config.listen.host, config.listen.port) == ("127.0.0.1", 8420)
    assert config.control_plane.backend == "memory"
    assert config.state.backend == "local"
    assert config.sandbox.backend == "local"
    assert config.audit.capture_requests is False
    assert config.decoded_master_keys() == {"k1": b"k" * 32}


def test_postgres_sections() -> None:
    """Postgres 后端的连接配置。"""
    config = parse_config(
        edited(
            control_plane={"backend": "postgres", "postgres": {"dsn": "${PG}", "schema": "cp"}},
            state={"backend": "postgres", "postgres": {"dsn": "${PG}", "pool_max": 20}},
        ),
        variables=VARIABLES,
    )
    assert config.control_plane.postgres is not None
    assert config.control_plane.postgres.dsn == VARIABLES["PG"]
    assert config.control_plane.postgres.schema_name == "cp"
    assert config.state.postgres is not None
    assert (config.state.postgres.schema_name, config.state.postgres.pool_max) == ("public", 20)


def test_postgres_timeouts() -> None:
    """连接与语句的时限：缺省保守但有限，可以改；必须是正数。"""
    config = parse_config(
        edited(
            control_plane={"backend": "postgres", "postgres": {"dsn": "${PG}"}},
            state={
                "backend": "postgres",
                "postgres": {
                    "dsn": "${PG}",
                    "connect_timeout_seconds": 3,
                    "command_timeout_seconds": 15.5,
                },
            },
        ),
        variables=VARIABLES,
    )
    assert config.control_plane.postgres is not None
    defaults = config.control_plane.postgres
    assert (defaults.connect_timeout_seconds, defaults.command_timeout_seconds) == (10.0, 60.0)
    assert config.state.postgres is not None
    tuned = config.state.postgres
    assert (tuned.connect_timeout_seconds, tuned.command_timeout_seconds) == (3.0, 15.5)


def test_expand() -> None:
    """变量在字符串、映射、列表里都展开；非字符串原样保留。"""
    value = {"a": "${X}-${Y}", "b": ["${X}", 1, None, True], "c": {"d": "plain"}}
    assert expand(value, {"X": "1", "Y": "2"}) == {
        "a": "1-2",
        "b": ["1", 1, None, True],
        "c": {"d": "plain"},
    }


def test_undefined_variable_is_an_error() -> None:
    """引用了没提供的变量要报错，不能悄悄当成空串。"""
    with pytest.raises(ConfigError, match="GATEWAY_KEY"):
        parse_config(edited(), variables={})


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"data_dir": "relative/dir"}, "绝对路径"),
        ({"root_key_hash": "not-a-hash"}, "root_key_hash"),
        ({"root_key_hash": "A" * 64}, "root_key_hash"),
        ({"master_keys": {}}, "master_keys"),
        ({"master_keys": {"k1": "not base64!!"}}, "base64"),
        ({"master_keys": {"k1": base64.b64encode(b"short").decode()}}, "32 字节"),
        ({"primary_master_key": "k9"}, "primary_master_key"),
        ({"models__api_key": ...}, "只能给一个"),
        ({"models__api_key_secret": "also"}, "只能给一个"),
        ({"models__tiers__deep": ...}, "deep"),
        ({"models__tiers__huge": {"model": "x"}}, "tiers"),
        ({"listen": {"port": 0}}, "port"),
        ({"control_plane": {"backend": "postgres"}}, "postgres 段"),
        ({"control_plane": {"backend": "sqlite"}}, "backend"),
        ({"state": {"backend": "postgres"}}, "postgres 段"),
        (
            {
                "state": {
                    "backend": "postgres",
                    "postgres": {"dsn": "x", "command_timeout_seconds": 0},
                }
            },
            "command_timeout_seconds",
        ),
        (
            {
                "state": {
                    "backend": "postgres",
                    "postgres": {"dsn": "x", "connect_timeout_seconds": -1},
                }
            },
            "connect_timeout_seconds",
        ),
        ({"sandbox": {"backend": "none"}}, "backend"),
        ({"sandbox": {"memory_mb": 1}}, "memory_mb"),
        ({"sandbox": {"connector_readable_roots": ["venv"]}}, "connector_readable_roots"),
        ({"sandbox": {"connector_image": ""}}, "connector_image"),
        ({"limits": {"max_iterations": 0}}, "max_iterations"),
        ({"lease_seconds": 1}, "lease_seconds"),
        ({"unknown_section": {}}, "unknown_section"),
        ({"audit": {"capture_request": True}}, "capture_request"),
    ],
)
def test_invalid_config(changes: dict[str, Any], fragment: str) -> None:
    """不合法的取值被拒绝，错误信息指到具体字段；拼错的键不被忽略。"""
    with pytest.raises(ConfigError) as caught:
        parse_config(edited(**changes), variables=VARIABLES)
    assert fragment in str(caught.value)


@pytest.mark.parametrize("text", ["", "- a\n", "a: [unclosed"])
def test_malformed_document(text: str) -> None:
    """不是映射、不是合法 YAML。"""
    with pytest.raises(ConfigError):
        parse_config(text, variables=VARIABLES)


def test_load_from_file(tmp_path: Path) -> None:
    """从文件读取；文件不存在时报配置错误。"""
    path = tmp_path / "xuanpu.yaml"
    path.write_text(edited())
    assert load_config(path, variables=VARIABLES).root_key_hash == "a" * 64
    with pytest.raises(ConfigError, match="读不到"):
        load_config(tmp_path / "missing.yaml", variables=VARIABLES)


def test_example_config_is_valid() -> None:
    """仓库里的配置示例要能通过解析，免得示例与代码走偏。"""
    from pathlib import Path

    example = Path(__file__).resolve().parents[3] / "examples" / "xuanpu.yaml"
    config = load_config(
        example,
        variables={
            "XUANPU_ROOT_KEY_HASH": "a" * 64,
            "XUANPU_MASTER_KEY": KEY,
            "MODEL_GATEWAY_KEY": "sk-x",
            "XUANPU_PG_DSN": "postgresql://u:p@db/x",
        },
    )
    assert config.control_plane.backend == "postgres"
    assert config.state.backend == "postgres"
    assert set(config.models.tiers) == {"fast", "balanced", "deep"}


S3_SECTION: dict[str, Any] = {
    "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
    "bucket": "b",
    "region": "oss-cn-hangzhou",
    "access_key_id": "id",
    "secret_access_key": "${OSS_KEY}",
}


def test_object_store_defaults_to_local() -> None:
    """对象存储默认是本机目录，目录未指定。"""
    config = parse_config(edited(), variables=VARIABLES)
    assert config.object_store.backend == "local"
    assert config.object_store.local.root is None
    assert config.object_store.s3 is None


def test_object_store_s3_expands_secret() -> None:
    """s3 后端的密钥经 ${VAR} 展开，寻址风格默认 virtual。"""
    text = edited(object_store={"backend": "s3", "s3": S3_SECTION})
    config = parse_config(text, variables={**VARIABLES, "OSS_KEY": "oss-secret"})
    assert config.object_store.s3 is not None
    assert config.object_store.s3.secret_access_key == "oss-secret"
    assert config.object_store.s3.addressing_style == "virtual"


def test_object_store_s3_requires_section() -> None:
    """backend 为 s3 却没给 s3 段，报错。"""
    with pytest.raises(ConfigError, match="s3"):
        parse_config(edited(object_store={"backend": "s3"}), variables=VARIABLES)


def test_object_store_local_root_must_be_absolute() -> None:
    """local.root 给相对路径，报错。"""
    with pytest.raises(ConfigError, match="绝对路径"):
        parse_config(edited(object_store={"local": {"root": "objects"}}), variables=VARIABLES)


async def test_local_backend_defaults_under_data_dir(tmp_path: Path) -> None:
    """local 后端缺省把对象放在 <data_dir>/objects 下。"""
    config = parse_config(edited(data_dir=str(tmp_path)), variables=VARIABLES)
    store = object_store(config)
    assert isinstance(store, LocalObjectStore)
    await store.put("t/a", b"x")
    assert (tmp_path / "objects" / "t" / "a").read_bytes() == b"x"


def test_s3_backend_builds_without_network() -> None:
    """s3 后端构造时不发请求，用假 endpoint 也能建出来。"""
    text = edited(object_store={"backend": "s3", "s3": S3_SECTION})
    config = parse_config(text, variables={**VARIABLES, "OSS_KEY": "k"})
    assert isinstance(object_store(config), S3ObjectStore)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "oss-cn-hangzhou.aliyuncs.com"),
        ("prefix", "/xuanpu/"),
        ("prefix", "xuanpu"),
    ],
)
def test_object_store_s3_rejects_bad_shapes(field: str, value: str) -> None:
    """endpoint 必须以 http(s):// 开头；prefix 为空或形如 ``a/b/``。"""
    section = {**S3_SECTION, field: value}
    text = edited(object_store={"backend": "s3", "s3": section})
    with pytest.raises(ConfigError):
        parse_config(text, variables={**VARIABLES, "OSS_KEY": "k"})


def test_connector_sandbox_settings_reach_the_provider() -> None:
    """连接器沙盒的两个配置项：缺省为空、同脚本镜像；给了就交到沙盒供给方手里。"""
    from pathlib import Path

    config = parse_config(edited(), variables=VARIABLES)
    assert config.sandbox.connector_readable_roots == ()
    assert config.sandbox.connector_image is None

    roots = ["/srv/connectors/.venv", "/opt/python"]
    local = _sandboxes(
        parse_config(edited(sandbox={"connector_readable_roots": roots}), variables=VARIABLES)
    )
    assert isinstance(local, LocalSandboxProvider)
    assert local._connector_roots == tuple(Path(root) for root in roots)  # noqa: SLF001

    docker = {"backend": "docker", "image": "scripts:1", "docker_env": {"PATH": "/usr/bin"}}
    default = _sandboxes(parse_config(edited(sandbox=docker), variables=VARIABLES))
    assert isinstance(default, DockerSandboxProvider)
    assert default._connector_image == "scripts:1"  # noqa: SLF001
    chosen = _sandboxes(
        parse_config(
            edited(sandbox={**docker, "connector_image": "connectors:2"}), variables=VARIABLES
        )
    )
    assert isinstance(chosen, DockerSandboxProvider)
    assert chosen._connector_image == "connectors:2"  # noqa: SLF001
