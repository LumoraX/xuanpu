// 网页内悬浮通知：显示状态与服务端已读状态分开，关闭卡片不等于标读。
"use strict";
const notificationAlerts = {watermark:0, snapshotReady:false, summaryShown:false, cards:new Map(), delayed:new Map()};

function clearVisibleNotificationAlerts() {
  notificationAlerts.cards.clear();
  $("notification-alert-list").innerHTML="";
  $("notification-alert-more").classList.add("hidden");
  $("notification-alerts").classList.add("hidden");
}
function resetNotificationAlerts() {
  for(const pending of notificationAlerts.delayed.values())clearTimeout(pending.timer);
  notificationAlerts.delayed.clear();
  notificationAlerts.watermark=0;notificationAlerts.snapshotReady=false;notificationAlerts.summaryShown=false;
  $("notification-announcer").textContent="";
  clearVisibleNotificationAlerts();
}
function dismissNotificationAlert(key) {
  notificationAlerts.cards.get(key)?.node.remove();
  notificationAlerts.cards.delete(key);
  if(!notificationAlerts.cards.size) {
    $("notification-alerts").classList.add("hidden");
    $("notification-alert-more").classList.add("hidden");
  }
}
// 到点前重新核实服务端状态；队列对象本身也是并发失效令牌。
function scheduleDelayedNotificationAlert(item) {
  const id=item.notification_id,previous=notificationAlerts.delayed.get(id);
  if(previous)clearTimeout(previous.timer);
  const pending={item,timer:null,generation:reminderState.generation,epoch:conn.epoch};
  notificationAlerts.delayed.set(id,pending);
  pending.timer=setTimeout(()=>verifyDelayedNotificationAlert(pending),Math.min(2147483647,Math.max(0,Math.ceil((item.alert_after-Date.now()/1000)*1000))));
}
async function verifyDelayedNotificationAlert(pending) {
  const id=pending.item.notification_id;
  const current=()=>pending.generation===reminderState.generation && pending.epoch===conn.epoch && notificationAlerts.delayed.get(id)===pending;
  if(!current())return;
  pending.timer=null;
  try {
    // 按原通知ID定位，避免它滑出最近100条列表后无法核实。
    const body=await api("GET",`/v1/notifications?after_id=${id-1}&limit=1`);
    if(!current())return;
    const fresh=body.items.find(item=>item.notification_id===id);
    if(!fresh || fresh.read_at || fresh.attention_active===false) {
      notificationAlerts.delayed.delete(id);dismissNotificationAlert(id);return;
    }
    if(fresh.alert_after>Date.now()/1000) {scheduleDelayedNotificationAlert(fresh);return;}
    notificationAlerts.delayed.delete(id);addNotificationAlert(fresh);
  } catch(error) {
    if(!current())return;
    $("notification-status").textContent="稍后自动核实延迟通知。";
    pending.timer=setTimeout(()=>verifyDelayedNotificationAlert(pending),15000);
  }
}
function addNotificationAlert(item, unreadCount = null) {
  if(item?.attention_active===false)return;
  // 正在查看通知中心时仅更新列表与水位，不再叠加提示卡遮挡操作。
  if(!$("notifications-panel").classList.contains("hidden")) return;
  // 汇总显示过即记住；关闭后，迟到的首次快照或历史SSE不能让它复活。
  if(!item && notificationAlerts.summaryShown) return;
  if(!item) notificationAlerts.summaryShown=true;
  const key=item ? item.notification_id : "summary";
  if(notificationAlerts.cards.has(key)) return;
  const generation=reminderState.generation;
  const node=reminderNode("article","","notification-alert");
  node.dataset.notificationId=String(key);
  const heading=reminderNode("div","","notification-alert-heading");
  heading.appendChild(reminderNode("strong",item ? (item.source==="work_task"?"交办工作有新进展":["proactive_check","proactive_followup"].includes(item.source)?"有一项业务需要跟进":"提醒时间到了") : "你有未读通知"));
  const close=reminderNode("button","×","notification-alert-close");close.type="button";
  close.setAttribute("aria-label","关闭通知提示");
  close.addEventListener("click",()=>{if(generation===reminderState.generation) dismissNotificationAlert(key);});
  heading.appendChild(close);node.appendChild(heading);
  const summaryText=unreadCount===null ? "正在同步通知，可先打开通知中心查看。" : `最近100条通知中有 ${unreadCount} 条未读。`;
  const content=reminderNode("p",item ? item.title : summaryText,"notification-alert-content");
  node.appendChild(content);
  if(item?.due_at!=null)node.appendChild(reminderNode("p",`原定 ${when(item.due_at)}`,"meta"));
  if(item?.body)node.appendChild(reminderNode("p",item.body,"notification-reason"));
  const actions=reminderNode("div","","notification-alert-actions");
  const view=reminderNode("button","查看通知");view.type="button";
  view.addEventListener("click",()=>{if(generation===reminderState.generation) return openNotificationCenter();});
  actions.appendChild(view);
  if(item) {
    const read=reminderNode("button","标为已读","ghost");read.type="button";
    read.addEventListener("click",async()=>{
      if(generation!==reminderState.generation || read.disabled) return;
      read.disabled=true;
      try {
        await api("POST",`/v1/notifications/${item.notification_id}/read`);
        if(generation!==reminderState.generation) return;
        dismissNotificationAlert(key);await refreshNotifications();
      } catch(error) {if(generation===reminderState.generation) {read.disabled=false;fail(error);}}
    });actions.appendChild(read);
  }
  node.appendChild(actions);
  // 超出上限时保留最新三张；其余通知仍持久保存在中心。
  if(notificationAlerts.cards.size>=3) {
    dismissNotificationAlert(notificationAlerts.cards.keys().next().value);
    $("notification-alert-more").classList.remove("hidden");
  }
  notificationAlerts.cards.set(key,{node,content});
  $("notification-alert-list").appendChild(node);
  if(item && typeof appendProactiveNotificationAction==="function")appendProactiveNotificationAction(item,actions,
    ()=>generation===reminderState.generation && notificationAlerts.cards.get(key)?.node===node,()=>dismissNotificationAlert(key));
  $("notification-alerts").classList.remove("hidden");
  $("notification-announcer").textContent=item ? `提醒时间到了：${item.title}。可在新通知提示中查看或标为已读。` : `你有未读通知。${summaryText}`;
}
function observeNotificationAlerts(items, {snapshot = false} = {}) {
  if(!conn.me?.user_id) return;
  const initial=!notificationAlerts.snapshotReady;
  const previous=notificationAlerts.watermark || 0;
  const unread=items.filter(item=>!item.read_at && item.attention_active!==false);
  const now=Date.now()/1000;
  // 免打扰通知保存在独立队列；SSE游标向前后仍会在到点补发。
  for(const item of items) {
    const existing=notificationAlerts.delayed.get(item.notification_id);
    if(item.read_at || item.attention_active===false) {
      if(existing)clearTimeout(existing.timer);
      notificationAlerts.delayed.delete(item.notification_id);
    } else if(item.alert_after>now && (!existing || existing.item.alert_after!==item.alert_after)) {
      scheduleDelayedNotificationAlert(item);
    }
  }
  const ready=unread.filter(item=>!item.alert_after || item.alert_after<=now);
  notificationAlerts.watermark=Math.max(previous,...items.map(item=>item.notification_id));
  for(const item of items) if(item.read_at || item.attention_active===false) dismissNotificationAlert(item.notification_id);
  const summary=notificationAlerts.cards.get("summary");
  if(snapshot && summary) {
    if(!ready.length) dismissNotificationAlert("summary");
    else summary.content.textContent=`最近100条通知中有 ${ready.length} 条未读。`;
  }
  // 初次快照与SSE显示水位分开：弱网时历史事件先回来也只给一张汇总。
  if(initial) {
    if(snapshot) notificationAlerts.snapshotReady=true;
    if(ready.length) addNotificationAlert(null,snapshot ? ready.length : null);
    return;
  }
  for(const item of ready.filter(item=>item.notification_id>previous).sort((a,b)=>a.notification_id-b.notification_id)) addNotificationAlert(item);
}
$("notification-alert-more").addEventListener("click",()=>openNotificationCenter());
