// 统一通知中心只显示通用协作提示；来源或职责变化先清除旧确认与正文。
"use strict";
const collabInbox={cursor:0,permission:null};
function resetCollaborationNotifications(){collabInbox.cursor=0;collabInbox.permission=null;$("collaboration-notification-list").replaceChildren();}
function collaborationPermission(revision){
 if(!revision)return;const changed=collabInbox.permission!==revision;collabInbox.permission=revision;
 if(changed&&!$("collaboration-panel").classList.contains("hidden")){
  const id=collabState.current?.id,kind=collabState.kind;collabInvalidate("正在重新核对当前协作责任与资料范围…");
  if(id){if(kind==="approval")void openApproval(id);else if(kind==="handoff")void openHandoff(id);}
 }
}
function collaborationNotificationEvent(name,data){if(name==="collaboration_notification")collabInbox.cursor=Math.max(collabInbox.cursor,data.sequence);else collaborationPermission(data.permission_revision);}
function renderCollaborationNotifications(page,append=false){
 if(!Number.isInteger(page.total)||!Number.isInteger(page.unread_total))throw new Error("协作通知统计格式无效，请刷新。");
 collaborationPermission(page.permission_revision);const host=$("collaboration-notification-list");
 if(!append){host.replaceChildren();host.append(cNode("h3","协作动态（"+page.total+"）"),cNode("p","未读（"+page.unread_total+"）"));}
 const epoch=conn.epoch,generation=reminderState.generation;
 for(const item of page.items.slice().reverse()){
  collabInbox.cursor=Math.max(collabInbox.cursor,item.sequence);const card=cNode("article");card.className="reminder-item";
  card.append(cNode("strong",item.title),cNode("p",item.body),cButton("查看当前工作",()=>{if(epoch!==conn.epoch||generation!==reminderState.generation)return;setNotificationsOpen(false);return item.approval_id?openApproval(item.approval_id):openHandoff(item.handoff_id);}));host.append(card);
  if(!item.read_at){const body={request_id:crypto.randomUUID(),confirmed:true,expected_version:item.version};let pending=false;
   const button=cButton("标为已读",async()=>{if(pending||epoch!==conn.epoch||generation!==reminderState.generation)return;pending=true;button.disabled=true;
    try{await api("POST","/v1/collaboration-notifications/"+encodeURIComponent(item.id)+"/read",{json:body});if(epoch===conn.epoch&&generation===reminderState.generation)await refreshNotifications();}
    catch(error){if(epoch===conn.epoch&&generation===reminderState.generation){if([403,404].includes(error.status))await refreshNotifications();else{button.disabled=false;button.textContent="重试标为已读";fail(error);}}}finally{pending=false;}
   });card.append(button);
  }
 }
 if(page.next_before_id)host.append(cButton("查看更多协作动态",async()=>{try{const next=await api("GET","/v1/collaboration-notifications?latest=true&limit=100&before_id="+page.next_before_id);if(epoch===conn.epoch&&generation===reminderState.generation)renderCollaborationNotifications(next,true);}catch(error){if(epoch===conn.epoch&&generation===reminderState.generation)fail(error);}}));
}
