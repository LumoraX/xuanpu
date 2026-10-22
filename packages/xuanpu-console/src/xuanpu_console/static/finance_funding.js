// 专用资金辅助入口：声明、真实材料、当前审批和渠道结果分别展示。
"use strict";
const financeFundingNames={prepayment:"预付",employee_advance:"员工垫付",entrusted_collection:"委托收款",prepayment_clearance:"预付核销",prepayment_return:"预付退回"};
Object.assign(financeActions,{submit_funding_case:"提交资金单据"});
function financeFundingClosing(record){return ["prepayment_clearance","prepayment_return"].includes(record.content?.scenario);}
function financeFundingToolbar(toolbar){toolbar.append(wsButton("专用资金记录",()=>financeFundingList()),wsButton("新建专用资金",()=>financeFundingChoose()));}
async function financeFundingChoose(){if(financeState.pending){financeShowPending();return;}financeInvalidate();const box=$("workspace-content");box.replaceChildren(wsText("h3","选择业务类型"));for(const [scenario,name] of Object.entries(financeFundingNames))box.append(wsButton(name,()=>financeFundingEdit(null,scenario)));}
async function financeFundingList(cursor="",append=false){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),page=await financeCall("listFundingCases",{legal_entity_id:financeState.entity,limit:50,...(cursor?{cursor}:{})});if(!financeCurrent(token))return;const box=$("workspace-content");if(!append)box.replaceChildren();financeFeedback("专用资金记录（"+page.total+"）");
  for(const item of page.items){const card=wsText("article","","workspace-record");card.append(wsText("h3",financeFundingNames[item.content.scenario]+" · "+item.content.reason),wsText("p","¥"+item.total+" · "+(financeStatuses[item.status]||item.status)),wsButton("核对资金单据",()=>financeFundingDetail(item.id)));box.append(card);}if(!page.items.length)box.append(wsText("p","尚无专用资金记录。"));if(page.next_cursor)box.append(wsButton("加载更多",()=>financeFundingList(page.next_cursor,true)));
}
async function financeFundingDetail(id,review=null){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision,record=await financeCall("getFundingCase",{id});if(!financeCurrent(token)||revision!==financeState.revision)return;financeState.current=record;const box=$("workspace-content"),funding=record.funding;box.replaceChildren(wsText("h3",financeFundingNames[record.content.scenario]+" · "+record.content.reason),wsText("p","¥"+record.total+" · "+(financeStatuses[record.status]||record.status)+" · 版本 "+record.version));financeSettlementMode(box,record);
  box.append(wsText("p","供应商："+record.content.supplier_ref.id+" · 到期："+record.content.due_date));if(funding.source_case_id)box.append(wsButton("查看原预付",()=>financeFundingDetail(funding.source_case_id)));
  if(record.content.scenario==="prepayment")box.append(wsText("p","已付 ¥"+funding.paid_amount+" · 已核销 ¥"+funding.cleared_amount+" · 已退回 ¥"+funding.returned_amount+" · 未结 ¥"+funding.outstanding_amount),wsText("p","核销在途 ¥"+funding.clearance_pending_amount+" · 退回在途 ¥"+funding.return_pending_amount));
  if(funding.authority)box.append(wsText("p","原凭据/授权："+funding.authority.reference+" · 限额 ¥"+funding.authority.amount_limit+" · "+(funding.authority.revoked?"已撤销":"未撤销")));
  box.append(wsText("p",funding.internal_attestation?"内部人工核对："+funding.internal_attestation.actor_id+" · 决定 "+funding.internal_attestation.decision_id+"。该核对不代表银行确认。":"材料待内部人工核对。"));financeEvidence(box,record.evidence);for(const evidence of record.evidence)box.append(wsButton("读取原件 "+evidence.id,()=>financeFundingReadMaterial(record,evidence,box)));
  if(funding.authority&&!funding.authority.revoked&&record.owner_id!==conn.me.user_id)box.append(wsButton("财务复核人撤销授权",()=>financeFundingRevoke(record)));
  for(const step of record.workflow?.steps||[])box.append(wsText("p",step.name+" · "+step.approver_user_id+" · "+({approved:"已批准",pending:"待审批",waiting:"等待前一步",rejected:"已驳回"}[step.status]||step.status)));
  const allocations=await Promise.all(record.allocation_ids.map(id=>financeCall("getInvoiceAllocation",{id},false,"catalog")));if(!financeCurrent(token)||revision!==financeState.revision)return;
  for(const item of allocations){box.append(wsText("p","票据 "+item.invoice_id+" · ¥"+item.amount+" · "+item.state),wsButton("核对票据 "+item.invoice_id,()=>financeInvoice(item.invoice_id)));if(record.status==="draft"&&record.owner_id===conn.me.user_id&&item.state==="proposed")box.append(wsButton("移除此分配",()=>financeConfirmWrite("releaseInvoiceAllocation",{id:item.id,body:{expected_version:item.version}},"确认移除此票据分配？",()=>financeFundingDetail(id))));}
  if(record.owner_id===conn.me.user_id){if(["draft","rejected","withdrawn"].includes(record.status))box.append(wsButton("编辑资金草稿",()=>financeFundingEdit(record)));if(record.status==="draft"){if(["employee_advance","entrusted_collection","prepayment_clearance"].includes(record.content.scenario))box.append(wsButton("分配发票",()=>financeAllocate(record)));box.append(wsButton("提交资金预检",()=>financeSettlementPreview("submit_funding_case",record,{})));}if(["under_review","submitted","approved"].includes(record.status)&&!(financeFundingClosing(record)&&record.status==="approved"))box.append(wsButton("撤回资金单据",()=>financeSettlementReason("withdraw",record)));}
  if(review&&record.status==="under_review"){const step=record.workflow?.steps.find(x=>x.id===review.workflow_step_id);if(review.target_version===record.version&&step?.id===record.workflow.current_step_id&&step.approver_user_id===conn.me.user_id&&record.owner_id!==conn.me.user_id){const button=wsButton("批准本步骤",()=>financeSettlementPreview("approve",record,{workflow_step_id:step.id}));button.disabled=!!review.blockers?.length;box.append(button,wsButton("驳回资金单据",()=>financeSettlementReason("reject",record,step.id)));}}
  await financeSettlementSection(box,record);if(typeof financeComplianceDocumentArchive==="function")await financeComplianceDocumentArchive(box,record);
}
async function financeFundingEdit(record=null,scenario=record?.content.scenario||"prepayment"){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const token=financeToken(),revision=financeState.revision;const [options,supplierRows,basisRows,parents]=await Promise.all([financeCall("getFundingOptions",{legal_entity_id:financeState.entity}),financeSettlementCatalog("listBusinessDocuments",{legal_entity_id:financeState.entity,kind:"supplier"},token),Promise.all(["contract","order","acceptance"].map(kind=>financeSettlementCatalog("listBusinessDocuments",{legal_entity_id:financeState.entity,kind},token))),["prepayment_clearance","prepayment_return"].includes(scenario)?financeFundingParents(token):Promise.resolve([])]);if(!financeCurrent(token)||revision!==financeState.revision)return;
  const content=record?.content,suppliers=financeFundingOriginalOptions(supplierRows,content?[content.supplier_ref]:[]),basis=financeFundingOriginalOptions(basisRows.flat(),content?.business_refs||[]),box=$("workspace-content");box.replaceChildren();if(!options.enabled||!suppliers.length||!basis.some(x=>x.ref.kind==="contract")){box.append(wsText("p","当前公司缺少启用的专用资金制度、供应商或合同，请联系财务负责人。"));return;}
  const form=wsText("form","","workspace-form");box.append(wsText("h3",financeFundingNames[scenario]+"草稿"),form);const select=(label,items,ref)=>financeSelect(form,label,items.map((x,i)=>({id:String(i),name:x.name})),String(ref?items.findIndex(x=>JSON.stringify(x.ref)===JSON.stringify(ref)):0));
  const supplier=select("供应商",suppliers,content?.supplier_ref),amount=financeField(form,"本次金额（元）","text",content?.amount||""),due=financeField(form,"到期日期","date",content?.due_date||""),reason=financeField(form,"款项用途","text",content?.reason||"");amount.inputMode="decimal";const selections=[];
  function addBasis(ref=basis.find(x=>x.ref.kind==="contract").ref){const field=select("合同、订单或验收依据 "+(selections.length+1),basis,ref),item={field,removed:false};selections.push(item);form.append(wsButton("移除依据 "+selections.length,()=>{item.removed=true;field.disabled=true;financeInvalidate(true);}));}for(const ref of content?.business_refs||[basis.find(x=>x.ref.kind==="contract").ref])addBasis(ref);form.append(wsButton("增加采购依据",()=>{addBasis();financeInvalidate(true);}));
  const parent=["prepayment_clearance","prepayment_return"].includes(scenario)?financeSelect(form,"原预付",parents.map(x=>({id:x.id,name:x.content.reason+" · 已付 ¥"+x.funding.paid_amount+" · 未结 ¥"+x.funding.outstanding_amount})),content?.source_case_id||parents[0]?.id||""):null;
  const authority=financeFundingAuthorityFields(form,scenario,content?.authority,options.accounts),materials={ids:[...(content?.evidence_ids||[])],pending:null},save=wsButton("核对资金草稿",()=>{});save.type="submit";save.disabled=!materials.ids.length;
  financeFundingMaterials(form,materials,save,token);form.append(save);form.addEventListener("input",()=>financeInvalidate(true));form.addEventListener("change",()=>financeInvalidate(true));
  financeSubmit(form,async()=>{if(!financeCurrent(token)||!materials.ids.length||materials.pending||materials.uploading||materials.confirming)return;const draft={legal_entity_id:financeState.entity,scenario,reason:reason.value.trim(),amount:financeMoney(amount.value),due_date:due.value,supplier_ref:suppliers[Number(supplier.value)].ref,business_refs:selections.filter(x=>!x.removed).map(x=>basis[Number(x.field.value)].ref),evidence_ids:[...materials.ids],...(parent?{source_case_id:parent.value}:{}),...(authority?{authority:authority(materials.ids)}:{})};await financeConfirmWrite(record?"updateFundingCase":"createFundingCase",{...(record?{id:record.id}:{}),body:record?{expected_version:record.version,draft}:draft},"确认保存"+financeFundingNames[scenario]+"草稿 · ¥"+draft.amount+" · "+draft.reason,result=>financeFundingDetail(result.id),form);});
}
function financeFundingOriginalOptions(items,refs){const result=[...items];for(const ref of refs)if(!result.some(x=>JSON.stringify(x.ref)===JSON.stringify(ref)))result.push({ref,name:"原资料 "+ref.id+" · "+ref.version+"（当前目录未提供）",account_name:"原绑定账户（当前不可用）",masked_account:ref.id});return result;}

async function financeFundingParents(token){const rows=[];let cursor;do{const page=await financeCall("listFundingCases",{legal_entity_id:financeState.entity,scenario:"prepayment",limit:100,...(cursor?{cursor}:{})});if(!financeCurrent(token))return [];rows.push(...page.items.filter(x=>x.owner_id===conn.me.user_id&&financeSettlementUnits(x.funding.paid_amount)>0n));cursor=page.next_cursor;}while(cursor);return rows;}
function financeFundingAuthorityFields(form,scenario,initial,accounts){
  if(scenario==="prepayment_clearance")return null;const kind={prepayment:"prepayment_terms",employee_advance:"employee_payment",entrusted_collection:"collection_mandate",prepayment_return:"return_evidence"}[scenario],reference=financeField(form,scenario==="employee_advance"||scenario==="prepayment_return"?"原交易编号":"条款或授权编号","text",initial?.reference||""),limit=financeField(form,"原凭据总额或授权限额（元）","text",initial?.amount_limit||"");
  const occurred=["employee_advance","prepayment_return"].includes(scenario)?financeField(form,"原交易日期","date",initial?.occurred_on||""):null,start=scenario==="entrusted_collection"?financeField(form,"授权生效日期","date",initial?.valid_from||""):null,end=scenario==="entrusted_collection"?financeField(form,"授权截止日期","date",initial?.valid_until||""):null;
  const allowed=financeFundingOriginalOptions(accounts,initial?.account_ref?[initial.account_ref]:[]).filter(x=>x.ref.kind===(scenario==="employee_advance"?"employee_account":"supplier_account")),account=["employee_advance","entrusted_collection"].includes(scenario)?financeSelect(form,"受控收款账户",allowed.map((x,i)=>({id:String(i),name:x.account_name+" · "+x.masked_account})),String(Math.max(0,allowed.findIndex(x=>JSON.stringify(x.ref)===JSON.stringify(initial?.account_ref))))):null;
  return ids=>({kind,reference:reference.value.trim(),amount_limit:financeMoney(limit.value),evidence_ids:[...(initial?.evidence_ids||ids)],...(occurred?{occurred_on:occurred.value}:{}),...(start?{valid_from:start.value,valid_until:end.value}:{}),...(account?{account_ref:allowed[Number(account.value)]?.ref}: {})});
}
function financeFundingMaterials(form,state,save,token,{required=true,onReceived=null}={}) {
  if(typeof financeMaterialsUpload==="function")return financeMaterialsUpload(form,state,save,token,{required,onReceived});
  financeState.materialForm=form;
  const input=financeField(form,"支持材料文件","file","",false),status=wsText("p",state.ids.length?"已有材料回执 "+state.ids.length+" 份":"尚无材料接收回执。上传并确认导入后，可读取接收结果。"),progress=wsText("div","");
  input.accept=".pdf,.png,.jpg,.jpeg,.xml";
  const update=()=>{save.disabled=(required&&!state.ids.length)||!!(state.pending||state.uploading||state.confirming);};
  const cancel=()=>{if(!financeState.pending&&!state.pending){state.confirming=false;update();}};
  form.append(status,progress,wsButton("上传支持材料",async()=>{
    const file=input.files?.[0];if(!file||state.pending||state.uploading||state.confirming||financeState.pending)return;
    financeInvalidate(true);state.uploading=true;update();const revision=financeState.revision,current=()=>financeCurrent(token)&&revision===financeState.revision;
    try {
      const bytes=await file.arrayBuffer(),sha=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)),n=>n.toString(16).padStart(2,"0")).join("");
      if(!current())return;
      const params=new URLSearchParams({name:file.name,request_id:crypto.randomUUID()}),item=await api("POST",workspaceBase+"/files?"+params,{body:file,headers:{"Content-Type":file.type||"application/octet-stream"},signal:financeState.controller.signal});
      if(!current())return;
      const args={body:{legal_entity_id:financeState.entity,files:[{file_id:item.id,revision:item.data.current_revision,sha256:sha}]}};
      state.confirming=true;
      await financeConfirmWrite("importSupportingMaterials",args,"确认将“"+file.name+"”固定版本导入当前公司独立财务存储？",op=>{
        // 在途编辑不能丢失原操作；独立表单身份仍拒绝切页或身份切换后的旧回执。
        if(!financeCurrent(token)||financeState.materialForm!==form)return;
        state.confirming=false;state.pending=op.id;status.textContent="材料已受理，尚未取得接收回执。";
        if(form.isConnected===false)$("workspace-content").replaceChildren(form);
        progress.replaceChildren(wsButton("读取材料接收结果",async()=>{
          const queryRevision=financeState.revision,operationId=state.pending;
          const op=await financeCall("getOperation",{id:operationId});
          if(!financeCurrent(token)||queryRevision!==financeState.revision||state.pending!==operationId)return;
          if(op.status!=="succeeded"){status.textContent="材料状态："+op.status+"，请保留原操作继续查询。";return;}
          const ids=op.result_refs?.filter(x=>x.kind==="source").map(x=>x.id);
          if(op.id!==operationId||op.purpose!=="supporting_material"||op.business_result!=="succeeded"||!Number.isSafeInteger(op.summary?.materials)||op.summary.materials<1||!Array.isArray(op.items)||!op.items.length||!op.items.every(x=>x.state==="material_received")||ids?.length!==op.summary.materials||ids.some(x=>typeof x!=="string"||!x))throw new Error("材料回执不完整，请核对原操作。");
          state.ids=[...new Set([...state.ids,...ids])];if(onReceived)onReceived(ids);state.pending=null;update();status.textContent="已取得真实材料接收回执 "+state.ids.length+" 份，待人工核对。";progress.replaceChildren();
        }));update();
      },form);
      for(const event of ["input","change"])form.addEventListener(event,cancel);
    } catch(error) {if(current()){status.textContent=error.message;state.confirming=false;fail(error);}}
    finally {state.uploading=false;if(financeCurrent(token))update();}
  }));update();
}
const financeFundingDownloads=new Set(),financeFundingDownloadLinks=new Map();
function financeFundingReleaseDownloads(){
  // 详情可能继续显示驳回表单；撤销字节URL时同步取消旧锚的可点击状态。
  for(const url of financeFundingDownloads){const link=financeFundingDownloadLinks.get(url);if(link){link.removeAttribute("href");link.removeAttribute("download");link.setAttribute("aria-disabled","true");link.textContent="此原件下载已失效，请重新读取";}URL.revokeObjectURL(url);}
  financeFundingDownloads.clear();financeFundingDownloadLinks.clear();
}
async function financeFundingReadMaterial(record,evidence,box){
  if(financeState.pending){financeShowPending();return;}const token=financeToken(),revision=financeState.revision,parts=[];let offset=0,initial=null;
  do{const part=await financeCall(record.kind==="expense_claim"?"getExpenseMaterial":"getFundingMaterial",{id:record.id,receipt_id:evidence.id,...(record.kind==="expense_claim"?{document_version:record.version}:{}),offset,limit:131072});if(!financeCurrent(token)||revision!==financeState.revision)return;
    if(part.document_id!==record.id||part.document_version!==record.version||part.receipt_id!==evidence.id||part.sha256!==evidence.sha256||part.offset!==offset||!Number.isSafeInteger(part.size)||part.size<1||part.size>50*1024*1024)throw new Error("原件或单据版本已变化，请刷新后核对。");
    if(initial&&(part.size!==initial.size||part.media_type!==initial.media_type))throw new Error("原件分块资料不一致。");initial=part;const decoded=atob(part.content_base64),bytes=Uint8Array.from(decoded,c=>c.charCodeAt(0));parts.push(bytes);const end=offset+bytes.length;if(!bytes.length||end>part.size||(part.next_offset!==null&&part.next_offset!==end)||(part.next_offset===null&&end!==part.size))throw new Error("原件分块不完整，请按原材料重新读取。");offset=part.next_offset;
  }while(offset!==null);
  const full=new Uint8Array(initial.size);let position=0;for(const part of parts){full.set(part,position);position+=part.length;}const sha=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",full)),x=>x.toString(16).padStart(2,"0")).join("");if(!financeCurrent(token)||revision!==financeState.revision)return;if(sha!==evidence.sha256)throw new Error("原件整体摘要校验失败。");
  const safeType=["application/pdf","image/png","image/jpeg"].includes(initial.media_type)?initial.media_type:"application/octet-stream";
  const url=URL.createObjectURL(new Blob([full],{type:safeType}));financeFundingDownloads.add(url);const link=wsText("a","下载已核对摘要的原件");link.href=url;link.download="财务材料-"+evidence.id+({"application/pdf":".pdf","image/png":".png","image/jpeg":".jpg","application/xml":".xml"}[initial.media_type]||".bin");financeFundingDownloadLinks.set(url,link);box.append(link,wsText("p","已读取独立财务原件；这不代表材料内容或银行事实已核实。"));
}
function financeFundingRevoke(record){
  if(financeState.pending){financeShowPending();return;}financeInvalidate();const form=wsText("form","","workspace-form"),box=$("workspace-content"),authority=record.funding.authority;box.append(form);form.append(wsText("p","仅当前公司财务复核人且非申请人可撤销。撤销阻止尚未发送的资格，不会撤回已付资金或删除历史。"));const reason=financeField(form,"撤销授权的原因"),button=wsButton("核对撤销授权",()=>{});button.type="submit";form.append(button);form.addEventListener("input",financeInvalidate);financeSubmit(form,()=>financeConfirmWrite("revokeFundingAuthority",{id:authority.id,body:{expected_version:authority.version,reason:reason.value.trim()}},"确认撤销原凭据/授权“"+authority.reference+"”？原因："+reason.value.trim(),()=>financeFundingDetail(record.id),form));
}

// 关闭辅助窗口或转到其它功能后，立即回收仍留在隐藏页面上的下载链接。
if(typeof MutationObserver!=="undefined")new MutationObserver(()=>{if(workspace.kind!=="finance")financeFundingReleaseDownloads();}).observe($("workspace-window"),{attributes:true,childList:true,subtree:true});
