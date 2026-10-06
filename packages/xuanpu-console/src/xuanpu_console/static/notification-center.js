// 非模态通知中心：保持当前页面与滚动位置，三个入口共用同一浮层。
"use strict";
const notificationCenter={opener:null};
function positionNotificationCenter() {
  const panel=$("notifications-panel");
  if(panel.classList.contains("hidden"))return;
  const anchor=$("notification-toggle").getBoundingClientRect();
  const top=Math.max(12,Math.min(anchor.bottom+8,window.innerHeight-120));
  const left=Math.max(12,Math.min(anchor.right-panel.offsetWidth,window.innerWidth-panel.offsetWidth-12));
  panel.style.top=top+"px";panel.style.left=left+"px";panel.style.right="auto";
  panel.style.maxHeight=Math.min(560,Math.max(0,window.innerHeight-top-12))+"px";
}
function setNotificationsOpen(open,{restoreFocus=false}={}) {
  const panel=$("notifications-panel"),toggle=$("notification-toggle");
  const wasOpen=!panel.classList.contains("hidden");
  if(open) {
    if(!conn.me?.user_id)return;
    if(!wasOpen)notificationCenter.opener=document.activeElement;
    clearVisibleNotificationAlerts();
    panel.classList.remove("hidden");toggle.setAttribute("aria-expanded","true");
    positionNotificationCenter();panel.focus({preventScroll:true});
  } else {
    panel.classList.add("hidden");toggle.setAttribute("aria-expanded","false");
    const opener=notificationCenter.opener;
    notificationCenter.opener=null;
    if(wasOpen && restoreFocus) {
      const target=opener?.isConnected && !opener.disabled ? opener : toggle;
      target.focus({preventScroll:true});
    }
  }
}
async function openNotificationCenter() {
  setNotificationsOpen(true);
  await refreshNotifications().catch(fail);
}
$("notification-toggle").addEventListener("click",()=>{
  if($("notifications-panel").classList.contains("hidden"))return openNotificationCenter();
  setNotificationsOpen(false,{restoreFocus:true});
});
$("open-notification-module").addEventListener("click",()=>openNotificationCenter());
$("close-notifications").addEventListener("click",()=>setNotificationsOpen(false,{restoreFocus:true}));
for(const name of ["pointerdown","focusin"])document.addEventListener(name,event=>{
  if(!$("notifications-panel").contains(event.target) && !$("notification-toggle").contains(event.target))setNotificationsOpen(false);
},true);
document.addEventListener("keydown",event=>{
  if(event.key!=="Escape" || $("notifications-panel").classList.contains("hidden"))return;
  event.preventDefault();event.stopImmediatePropagation();
  setNotificationsOpen(false,{restoreFocus:true});
},true);
window.addEventListener("resize",positionNotificationCenter);
document.addEventListener("scroll",positionNotificationCenter,true);
