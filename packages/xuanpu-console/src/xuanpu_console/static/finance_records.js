// 普通报销的申报原文、人工材料核对与只读流转历史。
"use strict";
function financeRecordsInvoiceField(form,claim) {
  if(claim && claim.record_mode!=="internal_record")return null;
  const label="发票信息（内部记录可填，未经查验）",wrapper=wsText("label",label),input=document.createElement("textarea");
  input.value=claim?.content.invoice_information||"";input.rows=7;input.maxLength=20000;input.setAttribute("aria-label",label);wrapper.append(input);form.append(wrapper);
  form.append(wsText("p","可选：粘贴发票信息原文，仅内部记录制度允许保存。申报文本未经查验，不是正式发票，不表示付款或记账。"));return input;
}
function financeRecordsInvoice(box,record) {
  if(record.record_mode!=="internal_record" || !record.content.invoice_information)return;
  const original=wsText("pre",record.content.invoice_information);original.setAttribute("style","white-space: pre-wrap; overflow-wrap: anywhere");
  box.append(wsText("h4","发票信息申报（未经查验）"),original,wsText("p","以上为申请人提供的原文，非正式发票；未作票据验真、付款或记账。"));
}
function financeRecordsMaterials(box,record) {
  for(const evidence of record.evidence||[])box.append(wsButton("读取原件 "+evidence.id,()=>financeFundingReadMaterial(record,evidence,box)));
}
function financeRecordsDetail(box,record) {
  financeRecordsInvoice(box,record);financeRecordsMaterials(box,record);
  const history=wsText("section",""),button=wsButton("查看流转历史",async()=>{button.disabled=true;try{await financeRecordsHistory(history,record);}catch(error){if(error.name!=="AbortError"){financeFeedback(error.message);fail(error);}}finally{button.disabled=false;}});
  box.append(button,history);
}
async function financeRecordsHistory(box,record,cursor="",append=false) {
  const token=financeToken(),revision=financeState.revision,page=await financeCall("listExpenseClaimHistory",{id:record.id,limit:1,...(cursor?{cursor}:{})});
  if(!financeCurrent(token)||revision!==financeState.revision)return;
  if(!Number.isSafeInteger(page.total)||page.total<0||!Array.isArray(page.items))throw new Error("流转历史返回不完整，请重新读取。");
  if(!append)box.replaceChildren(wsText("h4","流转历史（"+page.total+"）"));
  for(const item of page.items) {
    const row=wsText("article","","workspace-record"),data=item.data;
    row.append(wsText("p",item.created_at));
    if(item.kind==="revision") {
      row.append(wsText("h5","原单据版本 "+data.version+" · ¥"+data.total+" · "+(financeStatuses[data.status]||data.status)),wsText("p",data.content.reason));
      for(const line of data.content.lines||[])row.append(wsText("p",[line.reason,line.occurred_on,"¥"+line.amount].filter(Boolean).join(" · ")));
      financeRecordsInvoice(row,data);financeEvidence(row,data.evidence);financeRecordsMaterials(row,{...data,id:record.id,kind:"expense_claim"});
    } else if(item.kind==="decision") {
      row.append(wsText("h5",(financeActions[data.action]||data.action)+" · 单据版本 "+data.target_version),wsText("p",data.actor_id?"实际操作人："+data.actor_id:"实际操作人未记录"));
      if(data.parameters?.reason)row.append(wsText("p","意见："+data.parameters.reason));
      if(data.workflow_id||data.step_id)row.append(wsText("p",[data.workflow_id,data.step_id].filter(Boolean).join(" · ")));
    } else throw new Error("无法识别的历史记录类型，请重新读取。");
    box.append(row);
  }
  if(!page.items.length&&!append)box.append(wsText("p","尚无流转历史。"));
  if(page.next_cursor){const more=wsButton("加载更多历史",async()=>{if(more.disabled)return;more.disabled=true;try{await financeRecordsHistory(box,record,page.next_cursor,true);}catch(error){more.disabled=false;financeFeedback(error.message);fail(error);}});box.append(more);}
}
