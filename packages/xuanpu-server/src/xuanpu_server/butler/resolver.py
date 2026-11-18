"""匠人解析：本人此刻可用的业务包入口。

``ArtisanResolver`` 是接缝：现状实现 ``CatalogArtisanResolver`` 按租户当前生效版本解析
（与 ``/v1/assistant/route`` 的目录同口径）；R4-B 的到人能力决策合入后换成按决策解析，
派工记录里的 ``decision_ref`` 随之改为决策引用。两者谁后合入，谁负责适配并补反例测试。

承载方式：租户开启持久执行且包不出网时由 R2 持久任务承载（与 ``/v1/work-tasks/from-capability``
同口径），否则由既有会话承载。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import taifeng

from xuanpu_server.butler.models import Artisan, ArtisanRef
from xuanpu_server.errors import NotFoundError, PermissionDeniedError, RuntimeUnavailableError

if TYPE_CHECKING:
    from xuanpu_server.butler.models import HostKind
    from xuanpu_server.capabilities.admission import CapabilityAdmission
    from xuanpu_server.packages import PackageRegistry, ResolvedPackage
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.config import DurableExecutionConfig
    from xuanpu_server.workspace.service import WorkspaceService

CATALOG_MAX = 100


class ArtisanResolver(Protocol):
    """按认证员工解析可用匠人。"""

    async def available(self, who: Principal) -> tuple[Artisan, ...]:
        """本人此刻可用的全部匠人。"""
        ...

    async def resolve(self, who: Principal, package: str, entry: str) -> Artisan:
        """解析一个匠人；不可用一律 ``NotFoundError``，不区分原因。"""
        ...


class CatalogArtisanResolver:
    """现状实现：租户生效版本的全部入口，员工须在职、租户须启用。"""

    def __init__(
        self, packages: PackageRegistry, workspace: WorkspaceService,
        durable: DurableExecutionConfig,
    ) -> None:
        """依赖由平台装配注入。"""
        self._packages = packages
        self._workspace = workspace
        self._durable = durable
        # 能力受理（R4-B1）：纳管包只列本人有决策的入口、按决策版本；平台装配后注入
        self.admission: CapabilityAdmission | None = None

    async def available(self, who: Principal) -> tuple[Artisan, ...]:
        """逐个生效包读入口说明；不启动运行时或连接器。"""
        tenant, _ = await self._workspace.require_employee(who)
        records = await self._packages.list_versions(tenant)
        found: list[Artisan] = []
        for name in sorted({r.name for r in records}):
            if self.admission is not None and await self.admission.managed(tenant, name):
                found.extend(await self._managed(who, tenant, name))
                continue
            if await self._packages.active_version(tenant, name) is None:
                continue
            package = await self._packages.resolve(tenant, name)
            found.extend(await self._artisans(tenant, package))
            if len(found) > CATALOG_MAX:
                raise RuntimeUnavailableError("可用匠人过多，请联系管理员整理业务入口")
        return tuple(found)

    async def resolve(self, who: Principal, package: str, entry: str) -> Artisan:
        """未生效、不存在、入口未发布都是 404。"""
        tenant, _ = await self._workspace.require_employee(who)
        version = None
        if self.admission is not None and await self.admission.managed(tenant, package):
            decision = await self.admission.admit(who, package, entry, None)
            assert decision is not None
            version = decision.package_version
        elif await self._packages.active_version(tenant, package) is None:
            raise NotFoundError("匠人不可用")
        resolved = await self._packages.resolve(tenant, package, version)
        for artisan in await self._artisans(tenant, resolved):
            if artisan.ref.entry == entry:
                return artisan
        raise NotFoundError("匠人不可用")

    async def _managed(self, who: Principal, tenant: str, name: str) -> list[Artisan]:
        """纳管包：只列本人有能力决策的入口，按决策版本读说明。"""
        assert self.admission is not None
        found: list[Artisan] = []
        for entry in await self._entries(tenant, name):
            try:
                decision = await self.admission.admit(who, name, entry, None)
            except (NotFoundError, PermissionDeniedError):
                continue
            assert decision is not None
            package = await self._packages.resolve(tenant, name, decision.package_version)
            found.extend(a for a in await self._artisans(tenant, package) if a.ref.entry == entry)
        return found

    async def _entries(self, tenant: str, name: str) -> list[str]:
        """包的全部入口（纳管包可能从未激活：读生效版本，没有则读最新已发布版本）。"""
        version = await self._packages.active_version(tenant, name)
        if version is None:
            versions = await self._packages.list_versions(tenant, name)
            if not versions:
                return []
            version = versions[-1].version
        package = await self._packages.resolve(tenant, name, version)
        return list(package.manifest.spec.skills.entries)

    async def _artisans(self, tenant: str, package: ResolvedPackage) -> list[Artisan]:
        """一个包的全部已发布入口。"""
        skills = (await taifeng.FilesystemSkillRegistry.load(package.skills_dir)).snapshot()
        record = package.record
        host = self._host(tenant, package)
        found = []
        for entry in package.manifest.spec.skills.entries:
            skill = skills.get(entry)
            if skill is None:
                raise RuntimeUnavailableError("匠人入口缺少技能说明")
            ref = ArtisanRef(
                package=record.name, version=record.version,
                digest=record.digest.removeprefix("sha256:"), entry=entry,
                decision_ref=f"catalog:{record.name}@{record.version}",
            )
            found.append(Artisan(
                ref=ref, description=package.manifest.metadata.description[:2000],
                entry_description=skill.description[:2000], host=host,
            ))
        return found

    def _host(self, tenant: str, package: ResolvedPackage) -> HostKind:
        """与 from-capability 同口径：持久执行开启且断网的包走任务，其余走会话。"""
        if self._durable.allows(tenant) and not package.manifest.spec.policy.sandbox.network:
            return "task"
        return "session"
