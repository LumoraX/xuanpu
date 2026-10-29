// 原生财务提醒与运行概览；通知只恢复原事项，不代表业务决定。
"use strict";
const financeNoticeStatuses={awaiting_invoice:"到票或余款待核对",awaiting_confirmation:"等待本方确认",expired_pending_check:"本地期限已过，等待权威核对",pending:"等待审批",overdue:"审批已逾期",outcome_unknown:"结果待核对",attention_required:"需要核对",open:"等待补充材料",investigating:"核对中",recorded:"已记录",succeeded:"已完成",failed:"未完成",accepted:"已受理",cancelled:"已取消"};
const financeIntegrationStates={configured_internal:"内部审批已配置",configured_simulation:"隔离通道已配置",formal_unverified:"正式能力尚未验收",unconfigured:"尚未配置",disabled:"已关闭",ready:"已配置",degraded:"需要核对"};
function financeOperationsToolbar(toolbar){toolbar.append(wsButton("财务提醒",()=>financeNotifications()),wsButton("运行概览",()=>financeOverview()));if(financeInternalRecords())toolbar.append(wsButton("内部试用检查",financeInternalReadiness));}
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
  const handlers={funding_case:()=>financeFundingDetail(item.target_id,review),action_plan:()=>financeSettlementPlan(item.target_id,review),expense_claim:()=>financeDetail(item.target_id,review),payable:()=>financePayableDetail(item.target_id,review),operation:()=>financeInternalRecords()?financeNoticeOperation(item.target_id):financeSettlementOperation(item.target_id),red_letter_case:()=>financeComplianceCase(item.target_id),archive:()=>financeComplianceArchive(item.target_id),exception:()=>financeNoticeException(item.target_id)};
  if(!handlers[item.target_type])throw new Error("提醒目标类型无法识别，请联系财务核对。");await handlers[item.target_type]();
}
async function financeNoticeReview(item){
  const token=financeToken(),revision=financeState.revision;let cursor=null;do{const page=await financeCall("listReviewQueue",{legal_entity_id:financeState.entity,limit:100,...(cursor?{cursor}:{})});if(!financeCurrent(token)||revision!==financeState.revision)throw new DOMException("事项已变化","AbortError");const review=page.items.find(x=>x.target_id===item.target_id&&x.target_type===item.target_type);if(review)return review;cursor=page.next_cursor;}while(cursor);return null;
}

async function financeNoticeException(id){
  if(financeInternalRecords())return financeNoticeMaterialException(id);
  const token=financeToken(),revision=financeState.revision;let cursor=null,item=null;do{const page=await financeCall("listExceptions",{legal_entity_id:financeState.entity,limit:100,...(cursor?{cursor}:{})});if(!financeCurrent(token)||revision!==financeState.revision)return;item=page.items.find(x=>x.id===id);cursor=page.next_cursor;}while(!item&&cursor);
  const box=$("workspace-content");box.replaceChildren();if(!item){box.append(wsText("p","此事项已变化或当前无权查看，请刷新提醒。"));return;}box.append(wsText("h3","财务材料需要核对"),wsText("p","事项编号："+item.id),wsText("p",item.message||item.rule_id),wsText("p","当前状态："+(financeNoticeStatuses[item.status]||item.status)),wsText("p","请在对话中引用此事项并提交相应原件。系统会核对真实补件，不会因提醒已读而自动解除异常。"));
}
// 内部异常仅在本人已授权收件中定位；每次最多核对五批，继续历史须本人翻页。
async function financeNoticeMaterialMatch(id,batches,current){
  for(const batch of batches){
    if(!current())return null;
    if(typeof batch?.id!=="string"||!batch.id)throw new Error("收件目录响应不完整，请重新读取。");
    let detail;try{detail=await financeCall("getSupportingMaterialImport",{id:batch.id});}catch(error){if(!current())return null;if([403,404].includes(error.status))continue;throw error;}
    if(!current())return null;
    const operation=detail?.operation,materials=detail?.materials;
    if(operation?.id!==batch.id||operation.purpose!=="supporting_material"||!Array.isArray(operation.items)||!Array.isArray(materials)||materials.length!==operation.items.length)throw new Error("收件回执响应不完整，请重新读取。");
    const indexes=operation.items.map((item,index)=>item.exception_id===id?index:-1).filter(index=>index>=0);
    if(indexes.length>1)throw new Error("收件异常关联不唯一，请联系财务核对。");
    if(!indexes.length)continue;
    const index=indexes[0],item=operation.items[index],material=materials[index];
    if(material.state!==item.state||typeof item.state!=="string"||materials.some((entry,i)=>!Number.isSafeInteger(entry.position)||entry.position<0||(i>0&&entry.position<=materials[i-1].position)))throw new Error("收件原件关联不完整，请联系财务核对。");
    return {operation,item,material};
  }
  return null;
}
async function financeNoticeMaterialException(id,cursor=""){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision,entity=financeState.entity;
  const current=()=>financeCurrent(token)&&revision===financeState.revision&&entity===financeState.entity&&!financeState.pending;
  let page;try{page=await financeCall("listSupportingMaterialImports",{legal_entity_id:entity,limit:5,...(cursor?{cursor}:{})});}catch(error){if(!current())return;throw error;}if(!current())return;
  if(!Array.isArray(page?.items)||page.items.length>5||!Number.isSafeInteger(page.total)||page.total<page.items.length||!(page.next_cursor===null||(typeof page.next_cursor==="string"&&!!page.next_cursor)))throw new Error("收件目录响应不完整，请重新读取。");
  const match=await financeNoticeMaterialMatch(id,page.items,current);if(!current())return;
  const box=$("workspace-content");box.replaceChildren(wsText("h3","财务材料需要核对"),wsText("p","事项编号："+id));
  if(match){
    const {operation,item,material}=match,states={received:"等待接收",pending:"等待接收",material_received:"原件已保全",awaiting_original:"等待原件",quarantined:"已隔离，待核对",failed:"处理失败",cancelled:"已取消"};
    box.append(wsText("p","原收件批次："+operation.id),wsText("p",material.filename||"原件 "+(material.position+1)),wsText("p","原件接收状态："+(states[item.state]||item.state)),wsText("p",typeof item.error_code==="string"&&item.error_code?"接收原因代码："+item.error_code:"服务尚未返回失败代码，请核对原收件批次。"),wsText("p","重新上传会形成新的收件记录，不会自动解除此异常。请联系财务核对原事项。"));
  }else{
    box.append(wsText("p","当前这页本人的收件记录中不能定位此事项，或当前无权查看。请核对原收件批次，不能以其他批次代替。"),wsText("p","已核对本页 "+page.items.length+" 批收件，目录共 "+page.total+" 批。"));
    if(page.next_cursor){let pageRevision=revision;const pageCurrent=()=>financeCurrent(token)&&pageRevision===financeState.revision&&entity===financeState.entity&&!financeState.pending;const more=wsButton("继续查找较早收件",async()=>{if(more.disabled||!pageCurrent())return;more.disabled=true;try{const loading=financeNoticeMaterialException(id,page.next_cursor);pageRevision=financeState.revision;await loading;}catch(error){if(pageCurrent())more.disabled=false;throw error;}});box.append(more);}
  }
  if(typeof financeMaterialsInbox==="function")box.append(wsButton("查看我的收件记录",financeMaterialsInbox));
}
async function financeNoticeOperation(id){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision,operation=await financeCall("getOperation",{id});
  if(!financeCurrent(token)||revision!==financeState.revision)return;
  if(operation.id!==id||typeof operation.status!=="string")throw new Error("操作回执响应不完整，请重新核对。");
  const box=$("workspace-content");box.replaceChildren(wsText("h3","财务记录操作"),wsText("p","操作编号："+operation.id),wsText("p","当前状态："+(financeNoticeStatuses[operation.status]||operation.status)),wsText("p","请读取当前单据核对业务结论。"));
  for(const resource of operation.resources||[])if(resource.kind==="expense_claim")box.append(wsButton("查看报销记录",()=>financeDetail(resource.id)));
  if(operation.purpose==="supporting_material"&&typeof financeMaterialsInbox==="function")box.append(wsButton("查看我的收件记录",financeMaterialsInbox));
  box.append(wsButton("刷新操作结果",()=>financeNoticeOperation(id)));
}
async function financeOverview(){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision,data=await financeCall("getFinanceOverview",{legal_entity_id:financeState.entity});if(!financeCurrent(token)||revision!==financeState.revision)return;
  const internal=financeInternalRecords(),box=$("workspace-content");box.replaceChildren();financeFeedback("运行概览");box.append(wsText("h3","财务运行概览"),wsText("p","统计于 "+data.generated_at+(internal?"；内部记录与审批不表示已经付款、入账或完成票据验真。":"；状态来自独立财务记录，不能代替实际税务、支付或归档回执。")));const labels={imports:"原件处理",settlement:"付款与记账",compliance:"税务与归档"};
  for(const [kind,queue] of Object.entries(data.queues)){if(internal&&kind!=="imports")continue;const card=wsText("article","","workspace-record");card.append(wsText("h4",labels[kind]||kind),wsText("p","记录总数 "+financeCount(queue.total)),wsText("p",Object.entries(queue.status_counts).map(([status,count])=>(financeNoticeStatuses[status]||(typeof financeSettlementStatuses!=="undefined"?financeSettlementStatuses[status]:null)||status)+" "+financeCount(count)).join("；")||"暂无记录"),wsText("p",queue.oldest_created_at?"最早在途："+queue.oldest_created_at:"当前没有在途积压"));if(queue.oldest_unknown_created_at)card.append(wsText("p","最早结果未知："+queue.oldest_unknown_created_at+"，请恢复原任务核对。"));box.append(card);}
  box.append(wsText("p","待审批 "+financeCount(data.approvals.pending)+" · 已逾期 "+financeCount(data.approvals.overdue)),wsText("p","站内未读 "+financeCount(data.notifications.unread)+" · 通知待处理 "+financeCount(data.notifications.outbox_pending)+" · 通知诊断 "+financeCount(data.notifications.outbox_diagnostic)),wsText("p","模式："+({enabled:"启用",draining:"排空",read_only:"只读",disabled:"停用"}[data.runtime.mode]||data.runtime.mode)+" · 原件 "+financeRuntimeStatus(data.runtime.worker)+(internal?"":" · 结算 "+financeRuntimeStatus(data.runtime.settlement)+" · 合规 "+financeRuntimeStatus(data.runtime.compliance))+" · 站内提醒 "+financeRuntimeStatus(data.runtime.notification)));
  if(internal)return;
  const integrations=await financeCall("listIntegrations",{legal_entity_id:financeState.entity});if(!financeCurrent(token)||revision!==financeState.revision)return;for(const entry of integrations.items)box.append(wsText("p",({payment:"付款",erp:"记账",tax:"税务",archive:"归档",approval:"审批"}[entry.kind]||entry.kind)+"："+(financeIntegrationStates[entry.status]||entry.status)));box.append(wsText("p","正式税务、正式归档及外部消息送达须另行接入与验收。"));
}

function financeRuntimeStatus(value){return {running:"运行中",not_started:"未启动",failed:"故障"}[value]||value;}

// 内部报告仅使用固定白名单；不从缺失或畸形字段推断已通过。
const financeInternalCheckLabels={company:"公司资料",expense_policy:"内部报销制度",expense_categories:"费用分类",approval_roles:"审批及异常岗位",collector_separation:"收件岗位分离",grant_epochs:"当前授权代际",external_configuration:"外部配置关闭",simulation_disabled:"模拟关闭",runtime_mode:"运行模式",runtime_instance_lock:"实例独占锁",runtime_workers:"内部任务",runtime_external_workers:"外部执行器"};
const financeInternalExternalLabels={formal_policy_approval:"正式制度",production_identity:"生产身份",database_and_original_isolation:"数据库与原件隔离",offsite_disaster_recovery:"异机恢复",real_model:"真实模型",production_load:"生产负载"};
// 顶层和每个子对象都须精确匹配允许字段，避免泄漏附带原始配置。
function financeInternalObject(value,keys){return value!==null&&typeof value==="object"&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));}
// 长度按Unicode字符计算，保持与接口契约一致。
function financeInternalString(value,max){return typeof value==="string"&&[...value].length>0&&[...value].length<=max;}
// 配置与制度摘要只接受服务约定的完整小写SHA-256。
function financeInternalDigest(value){return typeof value==="string"&&/^[a-f0-9]{64}$/.test(value);}
// 禁止Date.parse把年份或纯日期宽松当作报告生成时间。
function financeInternalTimestamp(value){return typeof value==="string"&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)&&Number.isFinite(Date.parse(value));}
// 版本列表仅允许当前报销制度，代际属于制度而非人员授权。
function financeInternalVersion(value){return financeInternalObject(value,["kind","version","generation","enabled","digest"])&&value.kind==="expense"&&Number.isSafeInteger(value.version)&&value.version>0&&Number.isSafeInteger(value.generation)&&value.generation>0&&typeof value.enabled==="boolean"&&financeInternalDigest(value.digest);}
// 运行态显示真实枚举，未知或缺失状态不能降级为健康。
function financeInternalRuntime(value){
  return financeInternalObject(value,["mode","instance_lock_lost","receipt_worker","notification_worker","external_workers"])&&["enabled","draining","read_only","disabled"].includes(value.mode)&&typeof value.instance_lock_lost==="boolean"&&["running","failed","not_started"].includes(value.receipt_worker)&&["running","failed","not_started"].includes(value.notification_worker)&&["absent","present"].includes(value.external_workers);
}
// 严格验证完整报告后才渲染，全部通过也只允许not_verified。
function financeValidateInternalReadiness(value,entity){
  const keys=["legal_entity_id","profile","generated_at","status","production_status","checks","versions","configuration_digest","external_verifications","runtime"],codes=Object.keys(financeInternalCheckLabels),external=Object.keys(financeInternalExternalLabels);
  const invalid=()=>{throw new Error("内部试用检查响应格式无效，请重试或联系财务核对。");};
  if(!financeInternalObject(value,keys)||value.legal_entity_id!==entity||!financeInternalString(value.legal_entity_id,128)||value.profile!=="internal_records"||!financeInternalTimestamp(value.generated_at)||value.production_status!=="not_verified"||!financeInternalDigest(value.configuration_digest))invalid();
  if(!Array.isArray(value.checks)||value.checks.length!==codes.length)invalid();
  const seen=new Set();
  for(const item of value.checks){
    if(!financeInternalObject(item,["code","status","detail"])||!codes.includes(item.code)||seen.has(item.code)||!["passed","blocked"].includes(item.status)||!financeInternalString(item.detail,2000))invalid();
    seen.add(item.code);
  }
  if(value.status!==(value.checks.some(item=>item.status==="blocked")?"blocked":"not_verified"))invalid();
  if(!Array.isArray(value.versions)||value.versions.length>1||!value.versions.every(financeInternalVersion)||!financeInternalRuntime(value.runtime))invalid();
  if(!Array.isArray(value.external_verifications)||value.external_verifications.length!==external.length||new Set(value.external_verifications).size!==external.length||!value.external_verifications.every(item=>external.includes(item)))invalid();
  return value;
}
// 所有服务文字经wsText写入textContent，固定未验边界始终显示。
function financeRenderInternalReadiness(box,data,current){
  const mode={enabled:"启用",draining:"排空",read_only:"只读",disabled:"停用"},runtime=data.runtime;
  box.replaceChildren(wsText("h3","内部试用检查"),wsText("p","检查时间："+data.generated_at),wsText("p",data.status==="blocked"?"内部试用检查存在阻断，请逐项核对。":"当前配置检查通过，仍需核对以下未验项目。"),wsText("p","正式环境尚未验证（not_verified）。内部检查不授予业务执行许可。"));
  box.append(wsText("p","模式："+mode[runtime.mode]+"（"+runtime.mode+"）"),wsText("p","实例独占锁："+(runtime.instance_lock_lost?"已丢失":"保持有效")),wsText("p","收件任务："+financeRuntimeStatus(runtime.receipt_worker)+" · 站内提醒任务："+financeRuntimeStatus(runtime.notification_worker)),wsText("p","外部执行器："+(runtime.external_workers==="absent"?"未装配":"已装配")));
  for(const item of data.checks)box.append(wsText("p",financeInternalCheckLabels[item.code]+"："+(item.status==="passed"?"通过":"阻断")+" · "+item.detail));
  box.append(wsText("h4","当前制度与配置摘要"),wsText("p","公司配置摘要："+data.configuration_digest));
  if(!data.versions.length)box.append(wsText("p","尚无当前报销制度版本。"));
  for(const item of data.versions)box.append(wsText("p","报销制度版本："+item.version+" · 制度代际："+item.generation+" · "+(item.enabled?"启用":"关闭")),wsText("p","制度摘要："+item.digest));
  box.append(wsText("h4","固定未验项目"));
  for(const label of Object.values(financeInternalExternalLabels))box.append(wsText("p",label+"：尚未验证"));
  box.append(wsButton("重新检查",()=>{if(current())return financeInternalReadiness();}));
  financeFeedback(data.status==="blocked"?"内部试用检查存在阻断；正式环境尚未验证。":"内部试用检查已读取；正式环境尚未验证。");
}
// 只读查询仍保护未决写入；成功、失败和重试均绑定当前公司与页面。
async function financeInternalReadiness(){
  if(financeState.pending){financeShowPending();return;}
  if(!financeInternalRecords())return;
  financeInvalidate();const token=financeToken(),revision=financeState.revision,entity=financeState.entity;
  const current=()=>financeCurrent(token)&&revision===financeState.revision&&entity===financeState.entity&&!financeState.pending&&financeInternalRecords();
  const box=$("workspace-content");box.replaceChildren(wsText("h3","内部试用检查"),wsText("p","正在读取当前公司的只读检查报告。"));financeFeedback("正在读取内部试用检查。");
  try{
    const response=await financeCall("getInternalRecordsReadiness",{legal_entity_id:entity});
    if(!current())return;
    financeRenderInternalReadiness(box,financeValidateInternalReadiness(response,entity),current);
  }catch(error){
    // 过期失败也必须静默退出，防止旧公司的错误污染新页面。
    if(!current())return;
    const message=error.status===403?"内部试用检查需要当前公司的财务复核岗位权限，请联系财务核对授权后重试。":error.message;
    box.replaceChildren(wsText("h3","内部试用检查读取失败"),wsText("p",message),wsButton("重试内部试用检查",()=>{if(current())return financeInternalReadiness();}));financeFeedback(message);
  }
}
