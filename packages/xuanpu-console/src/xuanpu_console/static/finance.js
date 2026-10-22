// 财务辅助页：普通员工可无模型完成草稿、分票与逐步审批，真实规则始终由业务服务裁定。
"use strict";
const financeState={generation:0,controller:new AbortController(),entity:null,current:null,plan:null,execution:null,pending:null,queue:[],categories:[],centers:[],claimStatus:"",claimView:"mine",claimQuery:"",listRequest:0,capabilities:null,claimFilters:null,revision:0,materialForm:null};
const financeActions={submit_claim:"提交",approve:"批准本步骤",reject:"驳回",withdraw:"撤回"};
const financeStatuses={draft:"草稿",submitted:"已提交",under_review:"审批中",approved:"审批通过",rejected:"已驳回",withdrawn:"已撤回"};
function resetFinance() {
  if(typeof financeFundingReleaseDownloads==="function")financeFundingReleaseDownloads();
  financeState.controller.abort();financeState.controller=new AbortController();financeState.generation++;
  Object.assign(financeState,{entity:null,current:null,plan:null,execution:null,pending:null,queue:[],categories:[],centers:[],claimStatus:"",claimView:"mine",claimQuery:"",listRequest:0,capabilities:null,claimFilters:null,materialForm:null});
}
function financeToken(){return {epoch:conn.epoch,generation:financeState.generation};}
function financeCurrent(token){return conn.ready && token.epoch===conn.epoch && token.generation===financeState.generation && workspace.kind==="finance";}
function financeInvalidate(preserveMaterialForm=false){if(preserveMaterialForm!==true)financeState.materialForm=null;if(typeof financeFundingReleaseDownloads==="function")financeFundingReleaseDownloads();financeState.revision++;financeState.plan=null;financeState.execution=null;}
async function financeCall(operation,arguments_={},confirmed=false,connector="finance") {
  if(conn.apiKey || !conn.me?.user_id)throw new Error("请使用员工登录后操作财务记录。");
  const epoch=conn.epoch,controller=financeState.controller;
  const signals=[conn.controller.signal,controller.signal];
  if(typeof AbortSignal?.timeout==="function")signals.push(AbortSignal.timeout(30000));
  const result=await api("POST","/v1/integration-actions/finance/"+connector+"/"+operation,{
    signal:AbortSignal.any(signals),json:{arguments:arguments_,confirmed}});
  if(epoch!==conn.epoch || controller!==financeState.controller)throw new DOMException("身份已切换","AbortError");
  if(result.truncated)throw new Error("返回资料超过上限，请缩小查询范围。");
  if(result.is_error || result.status>=400) {
    const error=result.body?.error || result.body || {};
    throw Object.assign(new Error(error.message || "财务服务暂不可用"),{status:result.status,code:error.code});
  }
  return result.body;
}
function financeField(form,label,type="text",value="",required=true) {
  const wrapper=wsText("label",label),input=document.createElement("input");
  input.type=type;input.value=value;input.required=required;input.setAttribute("aria-label",label);wrapper.append(input);form.append(wrapper);return input;
}
function financeSelect(form,label,items,value="",optional=false) {
  const wrapper=wsText("label",label),select=document.createElement("select");select.required=!optional;select.setAttribute("aria-label",label);
  if(optional){const option=wsText("option","不指定");option.value="";select.append(option);}
  for(const item of items){const option=wsText("option",item.name);option.value=item.id;select.append(option);}select.value=value;
  wrapper.append(select);form.append(wrapper);return select;
}
function financeSubmit(form,handler){form.addEventListener("submit",event=>{event.preventDefault();return Promise.resolve().then(handler).catch(error=>{financeFeedback(error.message);fail(error);});});}
function financeMoney(value){if(!/^\d+(\.\d{1,2})?$/.test(value.trim()))throw new Error("金额请填写最多两位小数的非负数。");const [whole,fraction=""]=value.trim().split(".");return BigInt(whole).toString()+"."+fraction.padEnd(2,"0");}
function financeFeedback(text){$("workspace-feedback").textContent=text;}
function financeInternalRecords(){return financeState.capabilities?.profile==="internal_records";}
function financeValidateCapabilities(value) {
  const fields=["profile","mode","service_version","internal_records_only"];
  if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).length!==fields.length||fields.some(key=>!Object.hasOwn(value,key))||!["general","internal_records"].includes(value.profile)||!["enabled","read_only","disabled","draining"].includes(value.mode)||typeof value.service_version!=="string"||!value.service_version.trim()||typeof value.internal_records_only!=="boolean"||value.internal_records_only!==(value.profile==="internal_records"))throw new Error("财务能力响应无效，请重新加载或联系管理员核对。");
  return value;
}
async function renderFinanceList(generation,epoch) {
  financeState.generation++;if(!financeState.pending)financeInvalidate();financeState.current=null;financeState.capabilities=null;
  if(conn.apiKey){financeFeedback("报销与审批需要员工登录；请退出管理凭据模式。");return;}
  const token=financeToken();
  try {
    const capabilities=await financeCall("getFinanceCapabilities");
    if(!financeCurrent(token)||!workspaceCurrent(generation,epoch))return;
    financeState.capabilities=financeValidateCapabilities(capabilities);
    const entities=await financeCall("listLegalEntities",{limit:100});
    if(!financeCurrent(token)||!workspaceCurrent(generation,epoch))return;
    if(!entities.items?.length){financeFeedback("当前没有授权公司，报销功能未启用。");return;}
    if(!entities.items.some(x=>x.id===financeState.entity)){financeState.entity=entities.items[0].id;financeState.claimView="mine";financeState.claimStatus="";financeState.claimQuery="";}
    const toolbar=$("workspace-toolbar"),select=financeSelect(toolbar,"所属公司",entities.items,financeState.entity);
    select.addEventListener("change",()=>{if(financeState.pending){select.value=financeState.entity;financeShowPending();return;}financeState.entity=select.value;financeState.capabilities=null;financeState.claimView="mine";financeState.claimStatus="";financeState.claimQuery="";financeInvalidate();return refreshWorkspace();});
    select.disabled=!!financeState.pending;
    if(typeof financeMaterialsToolbar==="function")financeMaterialsToolbar(toolbar);
    if(typeof financeOperationsToolbar==="function")financeOperationsToolbar(toolbar);
    toolbar.append(wsButton("新建报销",financeEdit),wsButton("本人记录",()=>financeFilterClaims({view:"mine",status:""})),wsButton("本人草稿",()=>financeFilterClaims({view:"mine",status:"draft"})),wsButton("全部可看记录",()=>financeFilterClaims({view:"visible",status:""})),wsButton("待我审批",()=>financeLoadQueue(token)),wsButton("刷新",refreshWorkspace));
    financeClaimsFilters(toolbar);
    if(!financeInternalRecords()){
      const auxiliary=wsText("div",""),more=wsButton("更多财务功能",()=>{auxiliary.hidden=!auxiliary.hidden;more.setAttribute("aria-expanded",String(!auxiliary.hidden));});auxiliary.hidden=true;more.setAttribute("aria-expanded","false");
      if(typeof financeComplianceToolbar==="function")financeComplianceToolbar(auxiliary);
      if(typeof financeFundingToolbar==="function")financeFundingToolbar(auxiliary);
      if(typeof financeSettlementToolbar==="function")financeSettlementToolbar(auxiliary);
      toolbar.append(more,auxiliary);
    }
    if(financeState.pending)financeShowPending();
    else await financeLoadClaims(token);
  } catch(error) {
    if(!financeCurrent(token)||error.name==="AbortError")return;
    financeFeedback(error.status===404?"报销功能未启用，请联系管理员安装财务业务包。":error.message);
  }
}
function financeClaimsFilters(toolbar) {
  const status=financeSelect(toolbar,"记录状态",[{id:"",name:"全部状态"},...Object.entries(financeStatuses).map(([id,name])=>({id,name}))],financeState.claimStatus);
  const query=financeField(toolbar,"单号或事由","search",financeState.claimQuery,false);query.maxLength=256;
  financeState.claimFilters={status,query};
  status.addEventListener("change",()=>financeFilterClaims({status:status.value}).catch(error=>{financeFeedback(error.message);fail(error);}));
  toolbar.append(wsButton("查询记录",()=>financeFilterClaims({q:query.value})));
}
async function financeFilterClaims(filters) {
  const controls=financeState.claimFilters;
  if(financeState.pending){if(controls){controls.status.value=financeState.claimStatus;controls.query.value=financeState.claimQuery;}financeShowPending();return;}
  const q=(filters.q??financeState.claimQuery).trim();
  if([...q].length>128)throw new Error("单号或事由最多填写128个字符。");
  Object.assign(financeState,{claimView:filters.view??financeState.claimView,claimStatus:filters.status??financeState.claimStatus,claimQuery:q});
  if(controls){controls.status.value=financeState.claimStatus;controls.query.value=q;}
  return financeLoadClaims();
}
function financeValidateSummaryPage(page,args) {
  const keys=["id","legal_entity_id","owner_id","version","status","total","reason","created_at","record_mode"],nonempty=value=>typeof value==="string"&&!!value.trim();
  if(!page||!Array.isArray(page.items)||page.items.length>args.limit||!Number.isSafeInteger(page.total)||page.total<page.items.length||!(page.next_cursor===null||nonempty(page.next_cursor)))throw new Error("报销摘要响应不完整，请重新读取。");
  const seen=new Set();
  for(const item of page.items){
    if(!item||typeof item!=="object"||Object.keys(item).length!==keys.length||keys.some(key=>!Object.hasOwn(item,key))||!nonempty(item.id)||seen.has(item.id)||item.legal_entity_id!==args.legal_entity_id||!nonempty(item.owner_id)||(args.view==="mine"&&item.owner_id!==conn.me.user_id)||!Number.isSafeInteger(item.version)||item.version<1||!Object.hasOwn(financeStatuses,item.status)||(args.status&&item.status!==args.status)||typeof item.total!=="string"||!/^\d+\.\d{2}$/.test(item.total)||typeof item.reason!=="string"||[...item.reason].length<1||[...item.reason].length>2000||!nonempty(item.created_at)||!Number.isFinite(Date.parse(item.created_at))||!["internal_record","invoice_backed"].includes(item.record_mode))throw new Error("报销摘要响应无效，请重新读取。");
    seen.add(item.id);
  }
  return page;
}
async function financeLoadClaims(token=financeToken(),cursor="",append=false) {
  if(financeState.pending){financeShowPending();return;}if(!append)financeInvalidate();const revision=financeState.revision;let request=++financeState.listRequest;
  const args={legal_entity_id:financeState.entity,view:financeState.claimView,limit:20};if(cursor)args.cursor=cursor;if(financeState.claimStatus)args.status=financeState.claimStatus;if(financeState.claimQuery)args.q=financeState.claimQuery;
  if(!args.legal_entity_id)throw new Error("请先选择所属公司后查询报销记录。");
  const box=$("workspace-content");if(!append)box.replaceChildren();
  const current=()=>financeCurrent(token)&&revision===financeState.revision&&request===financeState.listRequest&&args.legal_entity_id===financeState.entity;
  let response;try{response=await financeCall("listExpenseClaimSummaries",args);}catch(error){if(!current())return;throw error;}if(!current())return;
  const page=financeValidateSummaryPage(response,args);financeFeedback("可查看的报销（"+page.total+"）");
  if(!page.items.length&&!append)box.append(wsText("p","还没有报销，点击新建即可保存本人草稿。"));
  for(const item of page.items){const card=wsText("article","","workspace-record");
    card.append(wsText("h3",item.reason),wsText("p","¥"+item.total+" · "+financeStatuses[item.status]+" · 版本 "+item.version),wsText("p","单号："+item.id),wsButton("查看详情",()=>financeDetail(item.id)));
    box.append(card);}
  if(page.next_cursor){const more=wsButton("加载更多",async()=>{if(more.disabled||!current())return;more.disabled=true;try{const loading=financeLoadClaims(token,page.next_cursor,true);request=financeState.listRequest;await loading;more.hidden=true;}catch(error){if(current())more.disabled=false;throw error;}});box.append(more);}
}
async function financeLoadQueue(token=financeToken(),cursor="",append=false) {
  if(financeState.pending){financeShowPending();return;}if(!append)financeInvalidate();const revision=financeState.revision;
  const args={legal_entity_id:financeState.entity,limit:50};if(cursor)args.cursor=cursor;
  const page=await financeCall("listReviewQueue",args);if(!financeCurrent(token)||revision!==financeState.revision)return;
  financeState.queue=page.items;const box=$("workspace-content");if(!append)box.replaceChildren();
  financeFeedback("待我审批（"+page.total+"）");if(!page.items.length)box.append(wsText("p","没有分派给你的审批步骤。"));
  for(const item of page.items){const card=wsText("article","","workspace-record");card.append(wsText("h3",item.summary),wsText("p","¥"+item.total+" · 单据版本 "+item.target_version),wsText("p",item.due_at?"审批期限："+item.due_at:"审批期限待服务确认"),wsText("p",(item.blockers||[]).map(text=>"阻断："+text).join("；")),wsButton("核对材料并审批",()=>item.target_type==="action_plan"?financeSettlementPlan(item.target_id,item):item.target_type==="funding_case"?financeFundingDetail(item.target_id,item):item.target_type==="payable"?financePayableDetail(item.target_id,item):financeDetail(item.target_id,item)));box.append(card);}
  if(page.next_cursor)box.append(wsButton("加载更多",()=>financeLoadQueue(token,page.next_cursor,true)));
}
async function financeDetail(id,review=null) {
  if(financeState.pending){financeShowPending();return;}
  financeInvalidate();const revision=financeState.revision,token=financeToken(),claim=await financeCall("getExpenseClaim",{id});
  if(!financeCurrent(token)||revision!==financeState.revision)return;financeState.current=claim;
  const box=$("workspace-content");box.replaceChildren();
  box.append(wsText("h3",claim.content.reason),wsText("p","¥"+claim.total+" · "+(financeStatuses[claim.status]||claim.status)+" · 版本 "+claim.version),wsText("p","单号："+claim.id));
  for(const line of claim.content.lines)box.append(wsText("p",[line.reason,line.occurred_on,"¥"+line.amount,line.category_id,line.department_id,line.project_id].filter(Boolean).join(" · ")));
  if(claim.record_mode==="internal_record")box.append(wsText("p","内部报销记录：记录与内部审批不表示已经付款、入账或完成票据验真。"));
  financeEvidence(box,claim.evidence);
  if(typeof financeRecordsDetail==="function")financeRecordsDetail(box,claim);
  if(claim.workflow){const flow=claim.workflow;box.append(wsText("h4","审批进展"));
    box.append(wsText("p","流程版本："+flow.version+" · 制度版本："+flow.policy_version));
    if(flow.due_at||claim.due_at)box.append(wsText("p","审批期限："+(flow.due_at||claim.due_at)));
    for(const step of flow.steps||[])box.append(wsText("p",[step.name||step.id,{pending:"待审批",approved:"已批准本步骤",rejected:"已驳回",cancelled:"已取消",waiting:"等待前一步"}[step.status]||step.status,step.approver_user_id?"审批人："+step.approver_user_id:"",step.decided_at,step.reason].filter(Boolean).join(" · ")));}
  const allocations=await Promise.all(claim.allocation_ids.map(allocationId=>financeCall("getInvoiceAllocation",{id:allocationId},false,"catalog")));
  if(!financeCurrent(token)||revision!==financeState.revision)return;
  for(const item of allocations){const row=wsText("article","","workspace-record");row.append(wsText("p","票据 "+item.invoice_id+" · 分配 ¥"+item.amount+" · 税额 ¥"+item.tax+" · "+({proposed:"待提交建议",reserved:"已保留额度",consumed:"已使用额度",released:"已释放"}[item.state]||item.state)),wsButton("查看票据与证据",()=>financeInvoice(item.invoice_id)));
    if(claim.status==="draft" && item.state==="proposed" && claim.owner_id===conn.me.user_id)row.append(wsButton("移除此分配",()=>financeConfirmWrite("releaseInvoiceAllocation",{id:item.id,body:{expected_version:item.version}},"确认移除此草稿的分票建议？",()=>financeDetail(claim.id))));box.append(row);}
  if(["draft","rejected","withdrawn"].includes(claim.status) && claim.owner_id===conn.me.user_id)box.append(wsButton("编辑报销记录",()=>financeEdit(claim)));
  if(claim.status==="draft" && claim.owner_id===conn.me.user_id){if(claim.record_mode!=="internal_record")box.append(wsButton("分配发票（可选）",()=>financeAllocate(claim)));box.append(wsButton("提交预检",()=>financePreview("submit_claim",{})));}
  if(claim.owner_id===conn.me.user_id && ["submitted","under_review","approved"].includes(claim.status))box.append(wsButton("撤回",()=>financeReason("withdraw")));
  if(review && claim.status==="under_review"){
    const step=claim.workflow?.steps?.find(item=>item.id===review.workflow_step_id);
    if(review.target_version!==claim.version || claim.workflow?.current_step_id!==review.workflow_step_id || !step || step.approver_user_id!==conn.me.user_id || claim.owner_id===conn.me.user_id){financeFeedback("审批目标、当前步骤或分派人已变化，请刷新待我审批。");return;}
    if(review.due_at && !claim.workflow?.due_at && !claim.due_at)box.append(wsText("p","审批期限："+review.due_at));
    for(const blocker of review.blockers||[])box.append(wsText("p","阻断："+blocker));
    const approve=wsButton("批准本步骤",()=>financePreview("approve",{workflow_step_id:review.workflow_step_id}));approve.disabled=!!review.blockers?.length;
    box.append(approve,wsButton("驳回",()=>financeReason("reject",review.workflow_step_id)));}
  if(claim.record_mode!=="internal_record"){
    if(typeof financeSettlementSection==="function")await financeSettlementSection(box,claim);
    if(typeof financeComplianceDocumentArchive==="function")await financeComplianceDocumentArchive(box,claim);
  }
  financeFeedback("请核对报销明细、票据依据和审批步骤；审批通过不表示已付款或入账。");
}
function financeEvidence(box,evidence=[]) {
  const details=wsText("details",""),title=wsText("summary","证据记录（"+evidence.length+"）");details.append(title);
  for(const item of evidence)details.append(wsText("p",[item.kind,item.id,"核验："+({not_checked:"待核验",verified:"已核验",failed:"未通过",unavailable:"不可用"}[item.verification_state]||item.verification_state),item.source_version?"来源版本 "+item.source_version:"",item.sha256].filter(Boolean).join(" · ")));
  box.append(details);
}
async function financeInvoice(id) {
  const token=financeToken(),[invoice,bundle]=await Promise.all([financeCall("getInvoice",{id}),financeCall("getInvoiceEvidence",{id})]);
  if(!financeCurrent(token))return;const box=$("workspace-content"),card=wsText("article","","workspace-record");
  card.append(wsText("h4","发票 "+invoice.number),wsText("p","票面 ¥"+invoice.gross+" · 税额 ¥"+invoice.tax+" · 可用 ¥"+invoice.available_amount+" · 版本 "+invoice.version),wsText("p","查验状态："+({not_checked:"未查验",passed:"已通过",failed:"未通过",unavailable:"不可用",pending:"处理中",stale:"已过期"}[invoice.verification_status]||invoice.verification_status)));
  if(typeof financeComplianceInvoice==="function")card.append(wsButton("查验、税务与归档",()=>financeComplianceInvoice(id)));
  financeEvidence(card,bundle.evidence);for(const gap of bundle.gaps||[])card.append(wsText("p",gap));box.append(card);
}
async function financeEdit(claim=null,initialMaterials=[]) {
  if(financeState.pending){financeShowPending();return;}
  financeInvalidate();const revision=financeState.revision,token=financeToken(),args={legal_entity_id:financeState.entity,limit:100};
  const [categories,centers]=await Promise.all([financeCall("listExpenseCategories",args,false,"catalog"),financeCall("listCostCenters",args,false,"catalog")]);
  if(!financeCurrent(token)||revision!==financeState.revision)return;financeState.categories=categories.items;financeState.centers=centers.items;
  if(!categories.items.length){financeFeedback("公司尚未配置可用的报销分类，无法保存草稿。");return;}
  const box=$("workspace-content"),form=wsText("form","","workspace-form");box.replaceChildren(form);
  const reason=financeField(form,"报销事由","text",claim?.content.reason||""),lines=[];
  const invoice=typeof financeRecordsInvoiceField==="function"?financeRecordsInvoiceField(form,claim):null;
  const materials={ids:[],pending:null};
  const attachments=typeof financeMaterialsAttachmentField==="function"?financeMaterialsAttachmentField(form,claim,token,initialMaterials):null;
  const evidence=attachments?.evidence||financeField(form,"附件记录编号（可选，多个用逗号分隔）","text",(claim?.content.evidence_ids||[]).join(","),false);
  function addLine(initial={}){const row=wsText("fieldset",""),legend=wsText("legend","费用明细 "+(lines.length+1));row.append(legend);
    const category=financeSelect(row,"费用分类",categories.items,initial.category_id||categories.items[0].id);
    const amount=financeField(row,"报销金额（元）","text",initial.amount||"");amount.inputMode="decimal";
    const date=financeField(row,"发生日期","date",initial.occurred_on||new Date().toISOString().slice(0,10));
    const why=financeField(row,"明细事由","text",initial.reason||"");
    const department=financeSelect(row,"所属部门",centers.items.filter(x=>x.kind==="department"),initial.department_id||"",true);
    const project=financeSelect(row,"所属项目",centers.items.filter(x=>x.kind==="project"),initial.project_id||"",true);
    form.append(row);lines.push({category,amount,date,why,department,project});}
  for(const line of claim?.content.lines||[{}])addLine(line);
  form.append(wsButton("增加一项费用",()=>{addLine();financeInvalidate(true);}));const save=wsButton(claim?"保存修改":"保存本人草稿",()=>{},"");save.type="submit";
  if(typeof financeFundingMaterials==="function")financeFundingMaterials(form,materials,save,token,{required:false,onReceived:(ids,items)=>{if(attachments){attachments.add(ids,items);return;}evidence.value=[...new Set([...evidence.value.split(/[，,\s]+/).filter(Boolean),...ids])].join(",");}});form.append(save);
  form.addEventListener("input",()=>financeInvalidate(true));
  financeSubmit(form,async()=>{if(!financeCurrent(token)||materials.pending||materials.uploading||materials.confirming)return;
    const ids=[...new Set(evidence.value.split(/[，,\s]+/).filter(Boolean))];
    const draft={legal_entity_id:financeState.entity,reason:reason.value.trim(),...(ids.length?{evidence_ids:ids}:{}),...(invoice?.value.trim()?{invoice_information:invoice.value}:{}),lines:lines.map(line=>({category_id:line.category.value,amount:financeMoney(line.amount.value),occurred_on:line.date.value,reason:line.why.value.trim(),...(line.department.value?{department_id:line.department.value}:{}),...(line.project.value?{project_id:line.project.value}:{})}))};
    await financeConfirmWrite(claim?"updateExpenseClaim":"createExpenseClaim",{...(claim?{id:claim.id}:{}),body:claim?{expected_version:claim.version,draft}:draft},"确认保存以下报销草稿？",result=>financeDetail(result.id),form);});
}
function financeAllocate(claim) {
  financeInvalidate();const box=$("workspace-content"),form=wsText("form","","workspace-form");box.replaceChildren(form);
  form.append(wsText("h3","为“"+claim.content.reason+"”分配发票"),wsText("p","填写已有票据的标识和当前版本。在提交单据时才保留额度。"));
  const invoice=financeField(form,"发票标识"),version=financeField(form,"发票当前版本","number"),amount=financeField(form,"本次分配金额（元）"),tax=financeField(form,"本次分配税额（元）","text","0.00");version.min="1";
  const save=wsButton("核对分配建议",()=>{},"");save.type="submit";form.append(save);
  form.addEventListener("input",financeInvalidate);
  financeSubmit(form,async()=>{await financeConfirmWrite("createInvoiceAllocation",{body:{invoice_id:invoice.value.trim(),invoice_version:Number(version.value),document_id:claim.id,document_kind:claim.kind==="funding_case"?"funding_case":claim.kind==="payable"?"payable":"expense_claim",expected_document_version:claim.version,amount:financeMoney(amount.value),tax:financeMoney(tax.value)}},"确认保存该票据版本的分配建议？",()=>claim.kind==="funding_case"?financeFundingDetail(claim.id):claim.kind==="payable"?financePayableDetail(claim.id):financeDetail(claim.id),form);});
}
function financeReason(action,step=null) {
  financeInvalidate();const box=$("workspace-content"),form=wsText("form","","workspace-form");box.append(form);
  const reason=financeField(form,financeActions[action]+"原因");reason.maxLength=2000;const submit=wsButton("查看"+financeActions[action]+"预检",()=>{},"");submit.type="submit";form.append(submit);
  form.addEventListener("input",financeInvalidate);
  financeSubmit(form,()=>{if(!reason.value.trim())return;return financePreview(action,{reason:reason.value.trim(),...(step?{workflow_step_id:step}:{})});});
}
async function financePreview(action,parameters) {
  if(financeState.pending){financeShowPending();return;}
  const token=financeToken(),id=financeState.current?.id;if(!id)return;
  financeInvalidate();const revision=financeState.revision,current=await financeCall("getExpenseClaim",{id});if(!financeCurrent(token)||revision!==financeState.revision)return;
  financeState.current=current;
  const request={body:{action,target_type:"expense_claim",target_id:id,expected_version:current.version,parameters},"Idempotency-Key":crypto.randomUUID()};
  financeState.pending={operation:"previewFinanceAction",arguments:request,onDone:plan=>{if(revision!==financeState.revision-1)return;financeState.plan=plan;financeShowPlan(plan,current);}};
  await financeSendPending(token);
}
function financeShowPlan(plan,claim) {
  const box=$("workspace-content");box.replaceChildren();
  box.append(wsText("h3","请核对服务预检结果"),wsText("p","动作："+(financeActions[plan.action]||plan.action)),wsText("p","金额：¥"+(plan.total||plan.amount||plan.parameters?.amount||claim.total)),wsText("p","目标："+plan.target_id+" · 版本 "+plan.target_version),wsText("p","计划有效期："+plan.expires_at));
  for(const effect of plan.effects)box.append(wsText("p",effect));
  for(const approval of plan.required_approvals)box.append(wsText("p","所需审批："+approval));
  for(const blocker of plan.blockers)box.append(wsText("p","阻断："+blocker));
  if(plan.parameters?.reason)box.append(wsText("p","原因："+plan.parameters.reason));
  const confirm=wsButton("本人确认"+(financeActions[plan.action]||plan.action),financeExecute,"");
  confirm.disabled=!plan.executable||plan.blockers.length>0||Date.parse(plan.expires_at)<=Date.now();box.append(confirm,wsButton("放弃本次确认",()=>{financeInvalidate();return financeDetail(claim.id);}));
  financeFeedback("此处尚未执行。请核对真实影响后再确认。");
}
async function financeExecute() {
  const plan=financeState.plan;if(!plan)return;const token=financeToken();
  if(!financeState.execution){if(!plan.executable||plan.blockers.length||Date.parse(plan.expires_at)<=Date.now()){financeInvalidate();financeFeedback("预检已失效，请重新读取详情并预检。");return;}
    financeState.execution={operation:"executeFinanceAction",arguments:{"Idempotency-Key":crypto.randomUUID(),body:{plan_id:plan.id,plan_digest:plan.digest}},target:plan.target_id};}
  financeState.pending=financeState.execution;
  await financeSendPending(token);
}
async function financeConfirmWrite(operation,args,label,onDone,form=null) {
  if(financeState.pending){financeShowPending();return;}
  const token=financeToken(),revision=financeState.revision,snapshot=JSON.parse(JSON.stringify(args)),box=wsText("article","","workspace-record");
  const beforeSend=operation==="importSupportingMaterials"&&typeof financeMaterialsSendGuard==="function"?financeMaterialsSendGuard(snapshot):null;
  box.append(wsText("p",label));
  if(args.body?.draft||args.body?.lines){const draft=args.body.draft||args.body;box.append(wsText("p",draft.reason));for(const line of draft.lines||[])box.append(wsText("p",[line.reason,line.occurred_on,"¥"+line.amount].join(" · ")));}
  if(args.body?.invoice_id)box.append(wsText("p","票据 "+args.body.invoice_id+" · 版本 "+args.body.invoice_version+" · 金额 ¥"+args.body.amount+" · 税额 ¥"+args.body.tax));
  const confirm=wsButton("本人确认保存",async()=>{if(!financeCurrent(token)||revision!==financeState.revision||confirm.disabled)return;confirm.disabled=true;
    financeState.pending={operation,arguments:{...snapshot,"Idempotency-Key":snapshot["Idempotency-Key"]||crypto.randomUUID()},onDone,beforeSend};
    await financeSendPending(token);},"");box.append(confirm);(form||$("workspace-content")).append(box);
  if(form)for(const event of ["input","change"])form.addEventListener(event,()=>{confirm.disabled=true;box.replaceChildren(wsText("p","内容已改变，请重新核对。"));},{once:true});
}
function financeShowPending() {
  const box=$("workspace-content");box.replaceChildren(wsText("p","上次操作结果尚未核实。请保持原请求，查询记录或重试同一操作。"),wsButton("按原请求核对并重试",()=>financeSendPending(financeToken())));
  const pending=financeState.pending;
  if(pending?.check)box.append(wsButton("查询原行动计划",pending.check));
  else if(pending?.target)box.append(wsButton("查询当前单据",()=>financeCheckPending(pending.target)));
}
async function financeCheckPending(id) {
  const token=financeToken(),claim=await financeCall("getExpenseClaim",{id});if(!financeCurrent(token))return;
  $("workspace-content").append(wsText("p","当前状态："+(financeStatuses[claim.status]||claim.status)+" · 版本 "+claim.version+" · ¥"+claim.total));
  if(financeState.plan){const plan=await financeCall("getActionPlan",{id:financeState.plan.id});if(!financeCurrent(token))return;$("workspace-content").append(wsText("p","原计划有效期："+plan.expires_at));}
}
async function financeSendPending(token) {
  const pending=financeState.pending;if(!pending||pending.sending)return;pending.sending=true;
  try {
    if(pending.beforeSend)pending.beforeSend();
    const result=await financeCall(pending.operation,pending.arguments,true);if(!financeCurrent(token))return;
    financeState.pending=null;financeInvalidate(pending.operation==="importSupportingMaterials");
    if(pending.onDone)await pending.onDone(result);
    else {$("workspace-content").replaceChildren(wsText("p","已取得操作回执："+result.id+"（"+result.status+"）。审批结论以当前单据为准。"),wsButton("读取最新单据",()=>financeDetail(pending.target)));}
    if(pending.operation!=="previewFinanceAction")financeFeedback("请求已受理，请以服务返回的当前业务状态为准。");
  } catch(error) {
    if(!financeCurrent(token))return;
    if(!financeState.pending){financeFeedback("请求已记录，详情暂时加载失败，请刷新核对。");$("workspace-content").append(wsButton("刷新核对",refreshWorkspace));return;}
    if(error.status>=400 && error.status<500){financeState.pending=null;financeInvalidate();financeFeedback(error.status===409?"版本已变化，原确认作废。请刷新详情后重新预检。":error.message);$("workspace-content").replaceChildren(wsButton("重新加载",refreshWorkspace));}
    else {financeShowPending();financeFeedback("结果未核实，请核对当前状态。重试将保留原幂等键和原计划，不产生新业务。");}
  } finally {pending.sending=false;}
}
