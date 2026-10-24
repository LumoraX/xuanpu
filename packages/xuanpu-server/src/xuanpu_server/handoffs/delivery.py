"""固定成果交付、签收与争议分别记录，不用后台任务状态代替业务事实。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError
from xuanpu_server.handoffs.models import Delivery
from xuanpu_server.handoffs.result_checks import results, target_digest
from xuanpu_server.handoffs.schemas import DeliverHandoff, SignOffHandoff
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from typing import Any

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.handoffs.service import HandoffService
    from xuanpu_server.shared_matters.schemas import SharedChange
    from xuanpu_server.tenancy import Principal


def changes(service: HandoffService, tx: CoordinationTransaction, who: Principal, handoff: Handoff,
            action: str, body: SharedChange) -> dict[str, Any]:
    """同事务冻结交付及原生签收引用，错误摘要不能偷偷变成完成。"""
    if action=='deliver':
        assert isinstance(body,DeliverHandoff)
        return _deliver(service,tx,who,handoff,body)
    if action in ('sign_off','dispute'):
        delivery=tx.collaboration.deliveries.get(handoff.delivery_id or '')
        if delivery is None or delivery.delivery_version!=handoff.delivery_version:
            raise ConflictError('当前交付记录不存在')
        if action=='sign_off':
            assert isinstance(body,SignOffHandoff)
            if body.delivery_version!=delivery.delivery_version:
                raise ConflictError('请核对本次固定交付版本后签收')
            results(service,tx,who,handoff,delivery.result_refs)
    if action=='reopen':
        # 旧决定仍保留，后续交付的新目标版本不再匹配旧组织批准。
        for request in list(tx.collaboration.approvals.values()):
            if request.handoff_id==handoff.id and request.status=='pending':
                tx.collaboration.approvals[request.id]=request.model_copy(update={'status':'superseded',
                    'version':request.version+1,'updated_at':service.now()})
    return {}


def _deliver(service: HandoffService, tx: CoordinationTransaction, who: Principal, handoff: Handoff,
             body: DeliverHandoff) -> dict[str, Any]:
    """组织批准必须指向同一责任版本、精确结果摘要及冻结策略修订。"""
    assert who.user_id is not None
    results(service,tx,who,handoff,body.result_refs)
    if handoff.policy_id is not None or body.approval_id is not None:
        request=tx.collaboration.approvals.get(body.approval_id or '')
        if (request is None or request.status!='approved' or request.handoff_id!=handoff.id
                or request.target_version!=handoff.version or request.target_digest!=target_digest(handoff,body.result_refs)
                or request.policy_id!=handoff.policy_id or request.policy_version!=handoff.policy_version
                or request.applicant_id!=who.user_id or request.applicant_epoch!=tx.participant_epochs[who.user_id]):
            raise ConflictError('本次固定成果尚无匹配的有效组织批准')
    now=service.now()
    delivery=Delivery(tenant_id=tx.tenant_id,id=new_id('hdl'),handoff_id=handoff.id,
        assignee_id=who.user_id,input_digest=handoff.input_digest,delivery_version=handoff.delivery_version+1,
        result_refs=body.result_refs,approval_id=body.approval_id,created_at=now,updated_at=now)
    tx.collaboration.deliveries[delivery.id]=delivery
    return {'delivery_id':delivery.id,'delivery_version':delivery.delivery_version}
