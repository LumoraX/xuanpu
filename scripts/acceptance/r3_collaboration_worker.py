"""独占合成库的协调提交故障点；真实子进程在提交前后等待SIGKILL。"""
from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
from r3_shared_worker import config_file
from taifeng_stores import SqlNamespace
from xuanpu_server.approvals.decisions import decide
from xuanpu_server.approvals.schemas import ApprovalDecisionInput
from xuanpu_server.approvals.service import ApprovalService
from xuanpu_server.collaboration.models import CollaborationConfig
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.coordination.postgres import PostgresCoordinationStore
from xuanpu_server.handoffs.actions import mutate
from xuanpu_server.handoffs.schemas import DeliverHandoff, SignOffHandoff
from xuanpu_server.handoffs.service import HandoffService
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.organization.service import OrganizationService
from xuanpu_server.shared_matters.materials import SharedMaterials
from xuanpu_server.shared_matters.models import SharedMatterConfig
from xuanpu_server.shared_matters.schemas import SharedChange
from xuanpu_server.shared_matters.service import SharedMatterService
from xuanpu_server.tenancy import Principal
from xuanpu_server.workspace.postgres import PostgresWorkspaceStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.coordination.models import CoordinationTransaction


class PausedCoordination(PostgresCoordinationStore):
    """只在本次动作实际新增来源事件的写事务停住，不把预读当提交。"""
    def configure_fault(self, config: dict[str,Any]) -> None:
        """私有配置仅控制合成子进程，不装入正式服务配置。"""
        self.fault=config

    async def pause(self) -> None:
        """标记只含阶段名称，不写业务正文、凭据或数据库地址。"""
        await asyncio.to_thread(Path(self.fault['marker']).write_text,json.dumps({'phase':self.fault['phase']}))
        await asyncio.Future()

    @asynccontextmanager
    async def transaction(self,tenant: str,*,participants: tuple[str,...]=(),allow_inactive: tuple[str,...]=()) -> AsyncIterator[CoordinationTransaction]:
        """提交前停顿证明回滚；提交后停顿证明回执和通知已原子持久。"""
        changed=False
        async with super().transaction(tenant,participants=participants,allow_inactive=allow_inactive) as tx:
            before=set(tx.state.events)
            yield tx
            changed=any(e.id not in before and e.kind==self.fault['event_kind'] for e in tx.state.events.values())
            if changed and self.fault['phase']=='before_commit':
                await self.pause()
        if changed and self.fault['phase']=='after_commit':
            await self.pause()


async def run(config: dict[str,Any]) -> None:
    """复用实际PG仓储、领域服务、固定身份和原确认，不启动模型或外部连接。"""
    pool=await asyncpg.create_pool(config['dsn'],min_size=1,max_size=4)
    namespace=SqlNamespace(schema=config['schema'],prefix=config['prefix'])
    store=PostgresControlPlane(pool,namespace=namespace);identity=PostgresIdentityStore(pool,namespace=namespace)
    coordination=PausedCoordination(pool,namespace);coordination.configure_fault(config)
    organization=OrganizationService(coordination,identity,store)
    service=SharedMatterService(organization,config=SharedMatterConfig(enabled=True,tenants=('acme',)))
    materials=SharedMaterials(service,PostgresWorkspaceStore(pool,namespace=namespace),LocalObjectStore(Path(config['objects_dir'])))
    handoffs=HandoffService(materials,config=CollaborationConfig(enabled=True,tenants=('acme',)))
    approvals=ApprovalService(handoffs,config=CollaborationConfig(enabled=True,tenants=('acme',)))
    who=Principal(role='tenant_member',tenant_id=config['tenant'],user_id=config['user_id'],key_id=None)
    try:
        if config['action']=='approve':
            await decide(approvals,who,config['identifier'],'approve',ApprovalDecisionInput.model_validate(config['body']))
        else:
            models={'accept':SharedChange,'deliver':DeliverHandoff,'sign_off':SignOffHandoff}
            await mutate(handoffs,who,config['identifier'],config['action'],models[config['action']].model_validate(config['body']))
    finally:
        await pool.close()


def main() -> None:
    """命令行只接收私有配置路径；合成schema及企业约束复用共享故障夹具。"""
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args();asyncio.run(run(config_file(args.config)))


if __name__=='__main__':
    main()
