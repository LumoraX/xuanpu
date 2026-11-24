// 协作原生确认共用本人身份；任何撤权、切换或晚响应都不能填回旧正文。
"use strict";
const collabState={generation:0,current:null,kind:null,view:"received",draft:null};
const collabLabels={submitted:"等待接收",accepted:"已接收",in_progress:"处理中",needs_information:"等待补充",delivered:"待签收",signed_off:"已签收",returned:"已退回",cancelled:"已取消",disputed:"待重办",pending:"审批中",approved:"审批通过",rejected:"已驳回",withdrawn:"已撤销",blocked:"待重新安排",superseded:"原输入已变化"};
const cNode=(tag,text="")=>connectionText(tag,text),cButton=(text,action)=>connectionButton(text,action);
function collabLive(epoch,generation){return epoch===conn.epoch&&generation===collabState.generation;}
function collabNotice(text){$("collaboration-feedback").textContent=text;}
function collabClear(message="",hide=false){
 collabState.generation++;collabState.current=null;collabState.kind=null;collabState.draft=null;
 $("collaboration-list").replaceChildren();collabNotice(message);if(hide)$("collaboration-panel").classList.add("hidden");
}
function resetCollaboration(){collabClear("",true);}
function collabInvalidate(message){collabClear(message);$("collaboration-list").append(cButton("重新查看我的协作",()=>openCollaboration()));}
function collabOpen(){ $("collaboration-panel").classList.remove("hidden");$("collaboration-panel").scrollIntoView?.({block:"start"}); }
async function collabPages(path,epoch,generation){
 const records=[];let cursor=null;
 do{const page=await api("GET",path+(path.includes("?")?"&":"?")+"limit=100"+(cursor?"&cursor="+encodeURIComponent(cursor):""));
  if(!collabLive(epoch,generation))return null;if(!Array.isArray(page.items))throw new Error("协作记录格式无效，请刷新。");records.push(...page.items);cursor=page.next_cursor;
 }while(cursor);return records;
}
function collabReview(host,label,path,body,lines,done,send=null){
 const epoch=conn.epoch,generation=collabState.generation,request=JSON.parse(JSON.stringify(body));
 const card=cNode("section");card.className="reminder-preview";card.append(cNode("h4","请核对本次操作"));
 for(const line of lines)card.append(cNode("p",line));let pending=false;
 const button=cButton("确认"+label,async()=>{
  if(pending||button.disabled||!collabLive(epoch,generation))return;pending=true;button.disabled=true;
  try{const result=send?await send(request):await api("POST",path,{json:request});if(collabLive(epoch,generation))await done(result);}
  catch(error){if(!collabLive(epoch,generation))return;
   if([403,404,409].includes(error.status))collabInvalidate(error.message+" 请重新核对当前记录。");
   else{collabNotice(error.message);button.disabled=false;button.textContent="重试本次提交";}
  }finally{pending=false;}
 });card.append(button,cButton("暂不提交",()=>card.remove()));host.append(card);return card;
}
function collabReason(host,label,action){sharedForm(host,label,(form,card)=>{
 const reason=sharedField(form,"说明原因");reason.required=true;reason.maxLength=4000;
 sharedSubmit(form,"核对"+label,()=>action(card,reason.value));
});}
function collabAction(host,record,action,label,extra={},reason=null){
 const body={request_id:crypto.randomUUID(),confirmed:true,expected_version:record.version,...extra};if(reason!==null)body.reason=reason;
 return collabReview(host,label,"/v1/handoffs/"+encodeURIComponent(record.id)+"/"+action,body,
  ["工作："+record.title,label,...(reason?["说明："+reason]:[])],()=>openHandoff(record.id));
}
async function collabDownload(record,ref,name){
 const epoch=conn.epoch,generation=collabState.generation;
 try{const response=await sharedBinary(sharedPath(record.shared_id)+"/materials/"+encodeURIComponent(ref.material_id)+"/content?revision="+ref.revision),data=await response.arrayBuffer();
  if(!collabLive(epoch,generation))return;const sha=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",data)),n=>n.toString(16).padStart(2,"0")).join("");
  if(sha!==ref.sha256)throw new Error("成果版本校验不一致，请重新查看。");if(!collabLive(epoch,generation))return;
  const blob=URL.createObjectURL(new Blob([data])),link=cNode("a");link.href=blob;link.download=name;link.click();URL.revokeObjectURL(blob);
 }catch(error){if(collabLive(epoch,generation)){if([403,404].includes(error.status))collabInvalidate(error.message);else collabNotice(error.message);}}
}
async function handleCollaborationRoute(route,text){
 if(!route.open_collaboration&&!route.handoff_draft)return false;
 if(typeof addMessage==="function")addMessage("user",text);
 await openCollaboration("received",route.handoff_draft||null,route.proposal_id||null);return true;
}
$("open-collaboration").addEventListener("click",()=>openCollaboration());
$("close-collaboration").addEventListener("click",resetCollaboration);
