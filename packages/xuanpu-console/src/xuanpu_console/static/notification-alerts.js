// 网页内悬浮通知：显示状态与服务端已读状态分开，关闭卡片不等于标读。
"use strict";
const notificationAlerts = {watermark:0, snapshotReady:false, summaryShown:false, cards:new Map()};

function clearVisibleNotificationAlerts() {
  notificationAlerts.cards.clear();
  $("notification-alert-list").innerHTML="";
  $("notification-alert-more").classList.add("hidden");
  $("notification-alerts").classList.add("hidden");
}
function resetNotificationAlerts() {
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
async function openNotificationCenter() {
  clearVisibleNotificationAlerts();
  setNotificationsOpen(true);
  $("notifications-panel").scrollIntoView({block:"start"});
  $("notifications-panel").focus({preventScroll:true});
  await refreshNotifications().catch(fail);
}
function addNotificationAlert(item, unreadCount = null) {
  // 汇总显示过即记住；关闭后，迟到的首次快照或历史SSE不能让它复活。
  if(!item && notificationAlerts.summaryShown) return;
  if(!item) notificationAlerts.summaryShown=true;
  const key=item ? item.notification_id : "summary";
  if(notificationAlerts.cards.has(key)) return;
  const generation=reminderState.generation;
  const node=reminderNode("article","","notification-alert");
  node.dataset.notificationId=String(key);
  const heading=reminderNode("div","","notification-alert-heading");
  heading.appendChild(reminderNode("strong",item ? "提醒时间到了" : "你有未读通知"));
  const close=reminderNode("button","×","notification-alert-close");close.type="button";
  close.setAttribute("aria-label","关闭通知提示");
  close.addEventListener("click",()=>{if(generation===reminderState.generation) dismissNotificationAlert(key);});
  heading.appendChild(close);node.appendChild(heading);
  const summaryText=unreadCount===null ? "正在同步通知，可先打开通知中心查看。" : `最近100条通知中有 ${unreadCount} 条未读。`;
  const content=reminderNode("p",item ? item.title : summaryText,"notification-alert-content");
  node.appendChild(content);
  if(item) node.appendChild(reminderNode("p",`原定 ${when(item.due_at)}`,"meta"));
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
  $("notification-alerts").classList.remove("hidden");
  $("notification-announcer").textContent=item ? `提醒时间到了：${item.title}。可在新通知提示中查看或标为已读。` : `你有未读通知。${summaryText}`;
}
function observeNotificationAlerts(items, {snapshot = false} = {}) {
  if(!conn.me?.user_id) return;
  const initial=!notificationAlerts.snapshotReady;
  const previous=notificationAlerts.watermark || 0;
  const unread=items.filter(item=>!item.read_at);
  notificationAlerts.watermark=Math.max(previous,...items.map(item=>item.notification_id));
  for(const item of items) if(item.read_at) dismissNotificationAlert(item.notification_id);
  const summary=notificationAlerts.cards.get("summary");
  if(snapshot && summary) {
    if(!unread.length) dismissNotificationAlert("summary");
    else summary.content.textContent=`最近100条通知中有 ${unread.length} 条未读。`;
  }
  // 初次快照与SSE显示水位分开：弱网时历史事件先回来也只给一张汇总。
  if(initial) {
    if(snapshot) notificationAlerts.snapshotReady=true;
    if(unread.length) addNotificationAlert(null,snapshot ? unread.length : null);
    return;
  }
  for(const item of unread.filter(item=>item.notification_id>previous).sort((a,b)=>a.notification_id-b.notification_id)) addNotificationAlert(item);
}
$("notification-alert-more").addEventListener("click",()=>openNotificationCenter());
