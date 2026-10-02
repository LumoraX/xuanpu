"""配置的解析与校验。"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from xuanpu_server.bootstrap import _sandboxes, object_store, open_platform
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
    """最小配置可用；缺省值偏保守：只监听本机、内存控制面、本机沙盒、不留提示词、不开指标端点。"""
    config = parse_config(edited(), variables=VARIABLES)
    assert config.models.api_key == "sk-gateway"
    assert config.models.tiers["deep"].timeout_seconds == 600
    assert (config.listen.host, config.listen.port) == ("127.0.0.1", 8420)
    assert config.control_plane.backend == "memory"
    assert config.state.backend == "local"
    assert config.sandbox.backend == "local"
    assert config.audit.capture_requests is False
    assert config.metrics.token_hash is None
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
        ({"sandbox": {"backend": "docker", "connector_image": ""}}, "connector_image"),
        ({"sandbox": {"backend": "docker", "connector_image": "  \t"}}, "空白"),
        ({"sandbox": {"backend": "docker", "connector_readable_roots": ["/x"]}}, "只用于 local"),
        ({"sandbox": {"connector_image": "connectors:1"}}, "只用于 docker"),
        ({"limits": {"max_iterations": 0}}, "max_iterations"),
        ({"lease_seconds": 1}, "lease_seconds"),
        ({"unknown_section": {}}, "unknown_section"),
        ({"audit": {"capture_request": True}}, "capture_request"),
        ({"metrics": {"token_hash": "not-a-hash"}}, "token_hash"),
        ({"metrics": {"token_hash": "B" * 64}}, "token_hash"),
        ({"metrics": {"token": "x"}}, "token"),
        ({"public_url": "xuanpu.example.com"}, "public_url"),
        ({"public_url": "ftp://xuanpu.example.com"}, "public_url"),
        ({"public_url": "https://xuanpu.example.com/xuanpu"}, "不带路径"),
        ({"public_url": "https://xuanpu.example.com/?a=1"}, "不带路径"),
        ({"public_url": "https://xuanpu.example.com/#x"}, "不带路径"),
        ({"public_url": "https://user:pw@xuanpu.example.com"}, "用户名或密码"),
        ({"public_url": "https://xuanpu.example.com:99999"}, "public_url"),
        ({"public_url": "https://玄圃.example.com"}, "punycode"),
        ({"identity": {"session_ttl_seconds": 10}}, "session_ttl_seconds"),
        ({"identity": {"reconcile_interval_seconds": 0}}, "reconcile_interval_seconds"),
        ({"identity": {"default_tenant": "Acme Corp"}}, "租户标识非法"),
        ({"identity": {"login_landing_path": "console/"}}, "login_landing_path"),
        ({"identity": {"login_landing_path": "//evil.example.com/"}}, "login_landing_path"),
        ({"identity": {"login_landing_path": "/\\evil.example.com/"}}, "login_landing_path"),
        ({"identity": {"login_landing_path": "https://evil.example.com/"}}, "login_landing_path"),
        ({"identity": {"feishu_open_base": "https://open.feishu.cn/open-apis"}}, "不带路径"),
        ({"identity": {"feishu_accounts_base": "accounts.feishu.cn"}}, "feishu_accounts_base"),
        ({"identity": {"cookie_secured": False}}, "cookie_secured"),
    ],
)
def test_invalid_config(changes: dict[str, Any], fragment: str) -> None:
    """不合法的取值被拒绝，错误信息指到具体字段；拼错的键不被忽略。"""
    with pytest.raises(ConfigError) as caught:
        parse_config(edited(**changes), variables=VARIABLES)
    assert fragment in str(caught.value)


def test_identity_defaults() -> None:
    """不配 public_url 与 identity 段：没有对外地址，身份设置取设计里的默认值。"""
    config = parse_config(edited(), variables=VARIABLES)
    assert config.public_url is None
    identity = config.identity
    assert (identity.session_ttl_seconds, identity.cookie_secure) == (604800, True)
    assert identity.reconcile_interval_seconds == 86400
    assert (identity.default_tenant, identity.login_landing_path) == (None, "/console/")
    assert identity.feishu_open_base == "https://open.feishu.cn"
    assert identity.feishu_accounts_base == "https://accounts.feishu.cn"


@pytest.mark.parametrize(
    ("written", "origin"),
    [
        ("https://xuanpu.example.com", "https://xuanpu.example.com"),
        ("https://xuanpu.example.com/", "https://xuanpu.example.com"),
        ("HTTPS://Xuanpu.Example.COM:443", "https://xuanpu.example.com"),
        ("http://127.0.0.1:80/", "http://127.0.0.1"),
        ("http://127.0.0.1:8420", "http://127.0.0.1:8420"),
        ("https://xuanpu.example.com:8443", "https://xuanpu.example.com:8443"),
        ("http://[::1]:8420", "http://[::1]:8420"),
    ],
)
def test_public_url_is_normalized_to_an_origin(written: str, origin: str) -> None:
    """public_url 规整成浏览器 Origin 头的写法：小写、去掉默认端口与末尾的 /。飞书基地址同理。"""
    text = edited(public_url=written, identity={"feishu_open_base": written})
    config = parse_config(text, variables=VARIABLES)
    assert config.public_url == origin
    assert config.identity.feishu_open_base == origin


def test_identity_section_accepts_site_paths_and_tenant() -> None:
    """落地页是站内路径（可带查询串），默认租户按租户标识的规则校验。"""
    text = edited(
        identity={
            "login_landing_path": "/console/?from=login",
            "default_tenant": "acme",
            "cookie_secure": False,
        }
    )
    identity = parse_config(text, variables=VARIABLES).identity
    assert identity.login_landing_path == "/console/?from=login"
    assert identity.default_tenant == "acme"
    assert identity.cookie_secure is False
    assert parse_config(edited(identity={"login_landing_path": "/"}), variables=VARIABLES)


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
            "XUANPU_METRICS_TOKEN_HASH": "b" * 64,
            "XUANPU_OSS_KEY_ID": "oss-id",
            "XUANPU_OSS_KEY_SECRET": "oss-secret",
        },
    )
    assert config.metrics.token_hash == "b" * 64
    # 正式部署的形态：控制面与状态面在库里、归档在 S3 兼容存储、沙盒用容器
    assert config.control_plane.backend == "postgres"
    assert config.state.backend == "postgres"
    assert config.object_store.backend == "s3"
    assert config.object_store.s3 is not None
    assert config.object_store.s3.secret_access_key == "oss-secret"
    assert config.sandbox.backend == "docker"
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


def test_connector_sandbox_settings_reach_the_provider(tmp_path: Path) -> None:
    """连接器沙盒的两个配置项：缺省为空、同脚本镜像；给了就交到沙盒供给方手里。

    可读目录解析成真实路径之后交出去；镜像名去掉首尾空白。
    """
    config = parse_config(edited(), variables=VARIABLES)
    assert config.sandbox.connector_readable_roots == ()
    assert config.sandbox.connector_image is None

    (tmp_path / "venv").mkdir()
    (tmp_path / "python").mkdir()
    (tmp_path / "python-link").symlink_to(tmp_path / "python")
    roots = [str(tmp_path / "venv"), str(tmp_path / "python-link")]
    text = edited(data_dir=str(tmp_path / "data"), sandbox={"connector_readable_roots": roots})
    local = _sandboxes(parse_config(text, variables=VARIABLES))
    assert isinstance(local, LocalSandboxProvider)
    assert local._connector_roots == (  # noqa: SLF001
        (tmp_path / "venv").resolve(),
        (tmp_path / "python").resolve(),
    )

    docker = {"backend": "docker", "image": "scripts:1", "docker_env": {"PATH": "/usr/bin"}}
    default = _sandboxes(parse_config(edited(sandbox=docker), variables=VARIABLES))
    assert isinstance(default, DockerSandboxProvider)
    assert default._connector_image == "scripts:1"  # noqa: SLF001
    chosen = _sandboxes(
        parse_config(
            edited(sandbox={**docker, "connector_image": " connectors:2 "}), variables=VARIABLES
        )
    )
    assert isinstance(chosen, DockerSandboxProvider)
    assert chosen._connector_image == "connectors:2"  # noqa: SLF001


@pytest.mark.parametrize(
    ("root", "fragment"),
    [
        ("/", "根目录"),
        ("{data}", "data_dir"),
        ("{workdir}", "data_dir"),
        ("{data}/cache", "data_dir"),
        ("{tmp}/link-to-data", "data_dir"),
        ("{tmp}/objects", "object_store.local.root"),
        ("{tmp}/objects/sub", "object_store.local.root"),
        ("{tmp}/missing", "不存在"),
    ],
)
def test_connector_readable_roots_are_checked_after_resolving(
    tmp_path: Path, root: str, fragment: str
) -> None:
    """本机连接器沙盒的可读目录：解析符号链接之后，拒绝根目录、与数据目录或本机对象存储
    目录相同或互为上下级的目录，以及不存在的目录。错误指出原因。"""
    data = tmp_path / "workdir" / "data"
    data.mkdir(parents=True)
    (tmp_path / "objects" / "sub").mkdir(parents=True)
    (tmp_path / "link-to-data").symlink_to(data)
    value = root.format(data=data, workdir=data.parent, tmp=tmp_path)
    text = edited(
        data_dir=str(data),
        object_store={"local": {"root": str(tmp_path / "objects")}},
        sandbox={"connector_readable_roots": [value]},
    )
    config = parse_config(text, variables=VARIABLES)
    with pytest.raises(ConfigError, match="connector_readable_roots") as caught:
        _sandboxes(config)
    assert fragment in str(caught.value)


async def test_connector_startup_limit_reaches_the_runtime(tmp_path: Path) -> None:
    """limits.connector_startup_seconds：缺省 30 秒；给了就交到运行时的默认参数里。"""
    assert parse_config(edited(), variables=VARIABLES).limits.connector_startup_seconds == 30
    text = edited(data_dir=str(tmp_path / "data"), limits={"connector_startup_seconds": 7.5})
    async with open_platform(parse_config(text, variables=VARIABLES)) as platform:
        assert platform.runtimes.defaults.connector_startup_seconds == 7.5
    with pytest.raises(ConfigError, match="connector_startup_seconds"):
        parse_config(edited(limits={"connector_startup_seconds": 0}), variables=VARIABLES)


async def test_metrics_token_reaches_the_platform(tmp_path: Path) -> None:
    """metrics.token_hash：缺省不开指标端点；给了就由平台按它校验抓取令牌。"""
    from xuanpu_server.tenancy import generate_metrics_token

    token, digest = generate_metrics_token()
    plain = edited(data_dir=str(tmp_path / "plain"))
    async with open_platform(parse_config(plain, variables=VARIABLES)) as platform:
        assert not platform.metrics_enabled
        assert not platform.verify_metrics_token(token)
    text = edited(data_dir=str(tmp_path / "data"), metrics={"token_hash": digest})
    async with open_platform(parse_config(text, variables=VARIABLES)) as platform:
        assert platform.metrics_enabled
        assert platform.verify_metrics_token(token)
        assert not platform.verify_metrics_token(token + "x")
