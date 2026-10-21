// 原生财务提醒与运行概览；通知只恢复原事项，不代表业务决定。
"use strict";
const financeNoticeStatuses={awaiting_invoice:"到票或余款待核对",awaiting_confirmation:"等待本方确认",expired_pending_check:"本地期限已过，等待权威核对",pending:"等待审批",overdue:"审批已逾期",outcome_unknown:"结果待核对",attention_required:"需要核对",open:"等待补充材料",investigating:"核对中",recorded:"已记录",succeeded:"已完成",failed:"未完成",accepted:"已受理",cancelled:"已取消"};
const financeIntegrationStates={configured_internal:"内部审批已配置",configured_simulation:"隔离通道已配置",formal_unverified:"正式能力尚未验收",unconfigured:"尚未配置",disabled:"已关闭",ready:"已配置",degraded:"需要核对"};
function financeOperationsToolbar(toolbar){toolbar.append(wsButton("财务提醒",()=>financeNotifications()),wsButton("运行概览",()=>financeOverview()));}
function financeCount(value){if(!Number.isSafeInteger(value)||value<0)throw new Error("财务计数响应无效，请刷新核对。");return value;}
async function financeNotifications(cursor="",append=false){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision,page=await financeCall("listFinanceNotifications",{legal_entity_id:financeState.entity,limit:50,...(cursor?{cursor}:{})});if(!financeCurrent(token)||revision!==financeState.revision)return;
  const total=financeCount(page.total),unread=financeCount(page.unread_total);if(!Array.isArray(page.items))throw new Error("财务提醒响应无效，请刷新核对。");const box=$("workspace-content");if(!append)box.replaceChildren();financeFeedback("财务提醒（"+total+"）");box.append(wsText("p","未读 "+unread+" · 当前可见 "+total),wsText("p","这里保存财务站内提醒，不代表飞书、企业微信或邮件已送达。审批和执行仍须核对原事项。"));
  for(const item of page.items){const card=wsText("article","","workspace-record");card.append(wsText("h3",item.title),wsText("p",(item.read_at?"已读":"未读")+" · "+(financeNoticeStatuses[item.source_status]||item.source_status)),wsButton("核对原事项",()=>financeOpenNotice(item)));if(!item.read_at)card.append(wsButton("标记已读",()=>financeConfirmWrite("readFinanceNotification",{id:item.id,body:{expected_version:item.version}},"仅标记此财务提醒已读，不代表批准、补件完成或渠道成功。",()=>financeNotifications())));box.append(card);}
  if(!page.items.length)box.append(wsText("p","当前没有可见提醒。"));if(page.next_cursor)box.append(wsButton("加载更多",()=>financeNotifications(page.next_cursor,true)));
}
async function financeOpenNotice(item){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();
  const review=["approval_pending","approval_overdue"].includes(item.kind)?await financeNoticeReview(item):null;
  const handlers={funding_case:()=>financeFundingDetail(item.target_id,review),action_plan:()=>financeSettlementPlan(item.target_id,review),expense_claim:()=>financeDetail(item.target_id,review),payable:()=>financePayableDetail(item.target_id,review),operation:()=>financeSettlementOperation(item.target_id),red_letter_case:()=>financeComplianceCase(item.target_id),archive:()=>financeComplianceArchive(item.target_id),exception:()=>financeNoticeException(item.target_id)};
  if(!handlers[item.target_type])throw new Error("提醒目标类型无法识别，请联系财务核对。");await handlers[item.target_type]();
}
async function financeNoticeReview(item){
  const token=financeToken(),revision=financeState.revision;let cursor=null;do{const page=await financeCall("listReviewQueue",{legal_entity_id:financeState.entity,limit:100,...(cursor?{cursor}:{})});if(!financeCurrent(token)||revision!==financeState.revision)throw new DOMException("事项已变化","AbortError");const review=page.items.find(x=>x.target_id===item.target_id&&x.target_type===item.target_type);if(review)return review;cursor=page.next_cursor;}while(cursor);return null;
}

async function financeNoticeException(id){
  const token=financeToken(),revision=financeState.revision;let cursor=null,item=null;do{const page=await financeCall("listExceptions",{legal_entity_id:financeState.entity,limit:100,...(cursor?{cursor}:{})});if(!financeCurrent(token)||revision!==financeState.revision)return;item=page.items.find(x=>x.id===id);cursor=page.next_cursor;}while(!item&&cursor);
  const box=$("workspace-content");box.replaceChildren();if(!item){box.append(wsText("p","此事项已变化或当前无权查看，请刷新提醒。"));return;}box.append(wsText("h3","财务材料需要核对"),wsText("p","事项编号："+item.id),wsText("p",item.message||item.rule_id),wsText("p","当前状态："+(financeNoticeStatuses[item.status]||item.status)),wsText("p","请在对话中引用此事项并提交相应原件。系统会核对真实补件，不会因提醒已读而自动解除异常。"));
}
async function financeOverview(){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision,data=await financeCall("getFinanceOverview",{legal_entity_id:financeState.entity});if(!financeCurrent(token)||revision!==financeState.revision)return;
  const box=$("workspace-content");box.replaceChildren();financeFeedback("运行概览");box.append(wsText("h3","财务运行概览"),wsText("p","统计于 "+data.generated_at+"；状态来自独立财务记录，不能代替实际税务、支付或归档回执。"));const labels={imports:"原件处理",settlement:"付款与记账",compliance:"税务与归档"};
  for(const [kind,queue] of Object.entries(data.queues)){const card=wsText("article","","workspace-record");card.append(wsText("h4",labels[kind]||kind),wsText("p","记录总数 "+financeCount(queue.total)),wsText("p",Object.entries(queue.status_counts).map(([status,count])=>(financeNoticeStatuses[status]||financeSettlementStatuses[status]||status)+" "+financeCount(count)).join("；")||"暂无记录"),wsText("p",queue.oldest_created_at?"最早在途："+queue.oldest_created_at:"当前没有在途积压"));if(queue.oldest_unknown_created_at)card.append(wsText("p","最早结果未知："+queue.oldest_unknown_created_at+"，请恢复原任务核对。"));box.append(card);}
  box.append(wsText("p","待审批 "+financeCount(data.approvals.pending)+" · 已逾期 "+financeCount(data.approvals.overdue)),wsText("p","站内未读 "+financeCount(data.notifications.unread)+" · 通知待处理 "+financeCount(data.notifications.outbox_pending)+" · 通知诊断 "+financeCount(data.notifications.outbox_diagnostic)),wsText("p","模式："+({enabled:"启用",draining:"排空",read_only:"只读",disabled:"停用"}[data.runtime.mode]||data.runtime.mode)+" · 原件 "+financeRuntimeStatus(data.runtime.worker)+" · 结算 "+financeRuntimeStatus(data.runtime.settlement)+" · 合规 "+financeRuntimeStatus(data.runtime.compliance)+" · 站内提醒 "+financeRuntimeStatus(data.runtime.notification)));
  const integrations=await financeCall("listIntegrations",{legal_entity_id:financeState.entity});if(!financeCurrent(token)||revision!==financeState.revision)return;for(const entry of integrations.items)box.append(wsText("p",({payment:"付款",erp:"记账",tax:"税务",archive:"归档",approval:"审批"}[entry.kind]||entry.kind)+"："+(financeIntegrationStates[entry.status]||entry.status)));box.append(wsText("p","正式税务、正式归档及外部消息送达须另行接入与验收。"));
}

function financeRuntimeStatus(value){return {running:"运行中",not_started:"未启动",failed:"故障"}[value]||value;}
