"""F11只记录构造参数的边界替身，不创建网络、数据库或原件目录。"""
from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from xuanpu_finance import runtime

if TYPE_CHECKING:
    import pytest
    from xuanpu_finance.config import FinanceConfig


class RuntimeSpies:
    """替代不可安全连接的外部边界并在首次连接时替换固定文件。"""

    def __init__(self, config: FinanceConfig) -> None:
        """保留受测配置及捕获值，只用假锁和假池。"""
        self.config = config
        self.seen: dict[str, Any] = {}

    async def pool(self, dsn: str, **kwargs: Any) -> RuntimeSpies:
        """门禁之后立即替换全部文件，模拟轮换和TOCTOU攻击。"""
        self.seen["pool_dsn"] = dsn
        paths = [self.config.database_dsn_file, self.config.service_token_file,
                 self.config.objects.key_file, *self.config.public_key_files.values(),
                 *self.config.gateway.tenant_token_files.values(),
                 self.config.objects.access_key_file, self.config.objects.secret_key_file]
        for path in paths:
            if path is not None:
                path.write_bytes(b"replacement-private-secret")
        return self

    async def connect(self, dsn: str) -> RuntimeSpies:
        """记录持锁地址并复用无需数据库的连接替身。"""
        self.seen["lock_dsn"] = dsn
        return self

    async def close(self) -> None:
        """假连接没有需要释放的外部资源。"""

    async def fetchval(self, query: str, *args: Any) -> int:
        """模拟独占成功与会话标识。"""
        return 1

    def add_termination_listener(self, callback: Any) -> None:
        """假连接不会主动丢失锁。"""

    def remove_termination_listener(self, callback: Any) -> None:
        """假连接没有注册外部监听器。"""

    def is_closed(self) -> bool:
        """允许运行时完整执行清理分支。"""
        return False

    async def configure(self, *args: Any) -> object:
        """跳过数据库业务配置，仅检查资源装配的秘密来源。"""
        return object()

    def gateway(self, client: Any, *, tenant_tokens: Any) -> object:
        """记录正式网关实际使用的令牌。"""
        self.seen["gateway_tokens"] = tenant_tokens
        return object()

    def auth(self, **kwargs: Any) -> object:
        """记录认证器实际使用的服务令牌及公钥。"""
        self.seen["auth"] = kwargs
        return object()

    def local_objects(self, root: Any, key: bytes) -> object:
        """记录AES字节且不创建原件目录。"""
        self.seen["aes"] = key
        return object()

    def s3_client(self, *args: Any, **kwargs: Any) -> object:
        """记录S3显式凭据，关闭方法只为资源生命周期提供接口。"""
        self.seen["s3"] = kwargs
        return SimpleNamespace(close=lambda: None)

    async def app(self, *args: Any) -> FastAPI:
        """返回不含业务后台的空应用，保持完整上下文生命周期。"""
        return FastAPI()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """仅替换有副作用的边界，受测门禁和组装代码全部真实执行。"""
        monkeypatch.setattr(runtime.asyncpg, "create_pool", self.pool)
        monkeypatch.setattr(runtime.asyncpg, "connect", self.connect)
        monkeypatch.setattr(runtime, "configure_store", self.configure)
        monkeypatch.setattr(runtime, "PlatformGateway", self.gateway)
        monkeypatch.setattr(runtime, "FinanceAuthenticator", self.auth)
        monkeypatch.setattr(runtime, "EncryptedLocalObjects", self.local_objects)
        monkeypatch.setattr(runtime, "FinanceWorker", lambda *a, **k: object())
        monkeypatch.setattr(runtime, "runtime_app", self.app)
        monkeypatch.setattr("boto3.client", self.s3_client)
