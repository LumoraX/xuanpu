// 复用统一通知中心，来源权限摘要变化时清理旧共享投影和未提交确认卡。
"use strict";
const sharedInbox={cursor:0,permission:null};
function resetSharedNotifications(){sharedInbox.cursor=0;sharedInbox.permission=null;$("shared-notification-list").replaceChildren();}
function sharedPermissionChanged(revision){
 if(!revision)return;
 const changed=sharedInbox.permission!==null&&sharedInbox.permission!==revision;sharedInbox.permission=revision;
 if(changed&&!$("shared-panel").classList.contains("hidden")){
  sharedInvalidate("共享资料或权限已变化，请重新核对当前可见范围。关闭的阅读不会重新启用。");
  $("shared-list").append(sharedButton("重新查看共享事项",()=>openShared()),sharedButton("查看我的共享阅读",()=>sharedReadingList()));
 }
}
function sharedNotificationEvent(name,data){
 if(name==="shared_notification")sharedInbox.cursor=Math.max(sharedInbox.cursor,data.sequence);
 else sharedPermissionChanged(data.permission_revision);
}
function renderSharedNotifications(page,appendPage=false){
 if(!Number.isInteger(page.total)||!Number.isInteger(page.unread_total))throw new Error("共享通知统计格式无效，请刷新。");
 sharedPermissionChanged(page.permission_revision);const host=$("shared-notification-list");
 if(!appendPage){host.replaceChildren();host.append(sharedNode("h3","共享动态（"+page.total+"）"),sharedNode("p","未读（"+page.unread_total+"）"));}
 const epoch=conn.epoch,generation=reminderState.generation;
 for(const item of page.items.slice().reverse()){
  sharedInbox.cursor=Math.max(sharedInbox.cursor,item.sequence);const row=sharedNode("article");row.className="reminder-item";
  row.append(sharedNode("strong",item.title),sharedNode("p",item.body),sharedButton("查看当前资料",()=>{if(epoch===conn.epoch&&generation===reminderState.generation){setNotificationsOpen(false);return openSharedMatter(item.shared_id);}}));host.append(row);
  if(!item.read_at){const request={request_id:crypto.randomUUID(),confirmed:true,expected_version:item.version};let pending=false;
   const button=sharedButton("标为已读",async()=>{if(pending||epoch!==conn.epoch||generation!==reminderState.generation)return;pending=true;button.disabled=true;
    try{await api("POST","/v1/shared-notifications/"+encodeURIComponent(item.id)+"/read",{json:request});if(epoch===conn.epoch&&generation===reminderState.generation)await refreshNotifications();}
    catch(error){if(epoch===conn.epoch&&generation===reminderState.generation){if(error.status===403||error.status===404)await refreshNotifications();else{button.disabled=false;button.textContent="重试标为已读";fail(error);}}}finally{pending=false;}
   });row.append(button);
  }
 }
 if(page.next_before_id)host.append(sharedButton("查看更多共享动态",async()=>{
  try{const next=await api("GET","/v1/shared-notifications?latest=true&limit=100&before_id="+page.next_before_id);if(epoch===conn.epoch&&generation===reminderState.generation)renderSharedNotifications(next,true);}catch(error){if(epoch===conn.epoch&&generation===reminderState.generation)fail(error);}
 }));
}
