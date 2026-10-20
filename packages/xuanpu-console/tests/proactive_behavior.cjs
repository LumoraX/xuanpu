// 运行真实前端脚本，验证业务跟进的确认、恢复、隔离与通知延迟。
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
function element(tag='div') {
  const classes=new Set();return {tag,children:[],listeners:{},attributes:{},dataset:{},style:{},value:'',textContent:'',disabled:false,checked:false,
    classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,v)=>v?classes.add(x):classes.delete(x)},
    set innerHTML(value){assert.equal(value,'');this.replaceChildren();},
    append(...items){items.forEach(x=>this.appendChild(x));},appendChild(x){x.remove?.();this.children.push(x);x.parentNode=this;return x;},
    replaceChildren(...items){this.children.forEach(x=>x.parentNode=null);this.children=[];this.append(...items);},
    prepend(x){this.children.unshift(x);x.parentNode=this;},remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(x=>x!==this);this.parentNode=null;},
    setAttribute(k,v){this.attributes[k]=v;},addEventListener(k,fn){this.listeners[k]=fn;},focus(){},scrollIntoView(){},
    getBoundingClientRect(){return {bottom:40,right:900};}};
}
function harness() {
  const nodes=new Map(),timers=new Map(),messages=[],errors=[];let next=0;
  const node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  const context=vm.createContext({console,crypto,AbortController,DOMException,Date,Intl,Number,
    document:{createElement:element,querySelector:()=>({click(){}}),addEventListener(){}},window:{addEventListener(){}},$:node,
    conn:{epoch:1,ready:true,me:{user_id:'a'}},chat:{generation:1,sessionId:null,routing:false,streaming:null,routeContext:[]},
    workspace:{settings:{timezone:'Asia/Shanghai'},kind:null,generation:1},workspaceChat:{loading:1,attachments:[]},workspaceBase:'/v1/workspace',
    workspaceTime:stamp=>"时间 "+stamp,workspaceCurrent:(g,e)=>g===1 && e===1,workspaceRouteAccepted(){},renderMessageFileCards(){},closeWorkspace(){},openWorkspace(){},
    wsText:(tag,text,kind='')=>{const n=element(tag);n.textContent=text;n.className=kind;return n;},
    wsButton:(text,action,kind='ghost')=>{const n=element('button');n.textContent=text;n.className=kind;n.listeners.click=action;return n;},
    append:(role,text)=>messages.push({role,text}),toast(){},fail:e=>errors.push(e),when:x=>String(x),url:x=>x,
    api:async()=>({items:[]}),setTimeout:(callback,delay)=>{const id=++next;timers.set(id,{callback,delay});return id;},clearTimeout:id=>timers.delete(id),
  });node('notifications-panel').classList.add('hidden');
  for(const name of ['notification-alerts.js','notification-center.js','reminders.js','proactive.js'])
    vm.runInContext(fs.readFileSync(path.join(process.argv[2],name),'utf8'),context);
  return {context,node,timers,messages,errors,run:code=>vm.runInContext(code,context)};
}
function find(node,text){for(const c of node.children){if(typeof text==='function'?text(c):c.textContent===text)return c;const sub=find(c,text);if(sub)return sub;}return null;}
const record={id:'followup:1',version:2,item_version:7,title:'<img src=x onerror=alert(1)>',reason:'可能需要跟进，待核实',body:'来源 <script>bad()</script>',status:'awaiting_user',certainty:'inferred',evidence_refs:[{id:'m1'}]};
async function decisionsRequireSpecificTimeAndRetry() {
  const h=harness(),writes=[];h.context.item=record;
  h.context.api=async(method,url,options)=>{if(method==='POST'){writes.push(JSON.parse(JSON.stringify(options.json)));if(writes.length===1)throw new Error('断线');return {...record,status:'waiting',version:3};}return record;};
  h.run('showProactiveCard(item)');
  const box=h.node('proactive-draft');assert(find(box,record.title));assert(!find(box,n=>n.tag==='img'));
  await find(box,'设置提醒').listeners.click();assert.equal(writes.length,0);
  const form=find(box,n=>n.tag==='form'),due=find(form,n=>n.type==='datetime-local');
  await form.listeners.submit({preventDefault(){}});assert.equal(writes.length,0,'没有具体时间不能提交');
  due.value='2033-05-18T10:00';await form.listeners.submit({preventDefault(){}});
  assert.equal(writes.length,1);assert.equal(writes[0].due_at,1999994400);assert.equal(writes[0].timezone,'Asia/Shanghai');assert.equal(writes[0].version,2);assert.equal(writes[0].item_version,7,'确认须携带工作区版本以免覆盖新的进展');
  assert.equal(due.disabled,true,'不确定的提交冻结正文');due.value='2034-01-01T10:00';
  await form.listeners.submit({preventDefault(){}});assert.deepEqual(writes[0],writes[1]);
  assert(box.classList.contains('hidden'));assert.equal(h.messages.length,1);
}
async function completionAndHistoryRecovery() {
  const h=harness();h.context.item=record;let writes=0;
  h.context.api=async method=>method==='POST'?(writes++,{...record,status:'resolved',version:3}):record;
  await h.run('restoreProactiveDraft({...item,action:"complete"})');
  assert.equal(writes,0);await find(h.node('proactive-draft'),'确认完成').listeners.click();assert.equal(writes,1);
  assert(h.node('proactive-draft').classList.contains('hidden'));
  h.context.api=async()=>({...record,status:'resolved',version:3});
  await h.run('restoreProactiveDraft({...item,action:"complete"})');assert(h.node('proactive-draft').classList.contains('hidden'),'已处理历史不能恢复操作');
  h.context.api=async()=>({...record,version:4});await h.run('restoreProactiveDraft({...item,action:"complete"})');
  assert(!find(h.node('proactive-draft'),'确认完成'),'版本已改变不能恢复旧确认');
}
async function identityAndConversationIsolation() {
  const h=harness();let finish;h.context.api=()=>new Promise(r=>finish=r);
  const pending=h.run('openProactive("followup:1")');h.run('conn.epoch++; resetProactive()');finish(record);await pending;
  assert(h.node('proactive-draft').classList.contains('hidden'));
  h.context.item=record;h.run('showProactiveCard(item)');
  const old=find(h.node('proactive-draft'),'完成了');h.run('chat.generation++; dismissProactiveContext()');
  let writes=0;h.context.api=async()=>{writes++;return record;};await old.listeners.click();assert.equal(writes,0,'旧对话按钮失效');
  h.run('showProactiveCard(item)');await find(h.node('proactive-draft'),'收起').listeners.click();assert(h.node('proactive-draft').classList.contains('hidden'));
}
async function notificationsStayUnifiedAndDelayed() {
  const h=harness();let opened;h.context.openProactive=async id=>{opened=id;};
  const notice={notification_id:1,title:'跟进',source:'proactive_followup',proactive_followup_id:'followup:1',body:'请核实最新进展',due_at:null,read_at:null,alert_after:Date.now()/1000+3600};
  h.context.notices=[notice];h.context.api=async()=>({items:h.context.notices});
  await h.run('refreshNotifications()');assert.equal(h.node('notification-alert-list').children.length,0,'免打扰期间不显示汇总');
  const list=h.node('notification-list');assert(find(list,notice.body));assert(!find(list,n=>n.textContent==='原定 null'));
  await find(list,'处理').listeners.click();assert.equal(opened,'followup:1');
  const [timerId,timer]=[...h.timers.entries()].find(([,t])=>t.delay>3500000);assert(timer,'延迟通知必须独立保存定时器，不能依赖SSE游标');
  h.context.notices=[{...notice,alert_after:Date.now()/1000-1,attention_active:true}];h.run('notificationAlerts.watermark=999');h.timers.delete(timerId);await timer.callback();
  assert(find(h.node('notification-alert-list'),'跟进'),'推进游标后到点仍显示');
  h.run('resetReminders()');assert.equal(h.timers.size,0,'退出取消延迟提示');
}
async function conflictsAndLateWritesStayScoped() {
  const h=harness();h.context.item=record;let finish;
  h.context.api=async method=>method==='POST'?new Promise(resolve=>finish=resolve):record;
  h.run('showProactiveCard(item)');const pending=find(h.node('proactive-draft'),'完成了').listeners.click();
  h.run('chat.generation++;dismissProactiveContext();showProactiveCard({...item,title:"新的跟进"})');
  finish({...record,status:'resolved'});await pending;
  assert(find(h.node('proactive-draft'),'新的跟进'));assert.equal(h.messages.length,0,'旧提交结果不能写入新对话');
  h.context.api=async method=>{if(method==='POST')throw Object.assign(new Error('冲突'),{status:409});return {...record,version:3,reason:'最新依据'};};
  h.run('showProactiveCard(item)');await find(h.node('proactive-draft'),'完成了').listeners.click();
  assert(find(h.node('proactive-draft'),'最新依据'));assert.equal(h.errors.length,0);
  assert(find(h.node('proactive-draft'),'记录已改变，已读取最新状态。请重新选择操作。'));
}
async function settingsUseAvailableChannelsAndPreserveExpiry() {
  const h=harness();h.run('reminderState.channels=["inbox","feishu"]');
  h.context.settings={proactive_checks:{enabled:true,quiet_hours:{enabled:true,start:'22:00',end:'08:00'},ignored_types:[{type:'stale',until:2000000000}]},proactive_channels:['inbox','feishu']};
  h.context.form=element('form');h.run('readSettings=renderProactiveSettings(form,settings)');
  assert(find(h.context.form,'飞书通知'));assert(!find(h.context.form,'邮箱通知'));
  const value=JSON.parse(JSON.stringify(h.run('readSettings()')));assert.deepEqual(value.proactive_checks.ignored_types,[{type:'stale',until:2000000000}]);
  find(h.context.form,n=>n.attributes['aria-label']==='主动关注我的业务进展').checked=false;
  find(h.context.form,n=>n.attributes['aria-label']==='暂停承诺跟进').checked=true;
  find(h.context.form,n=>n.attributes['aria-label']==='暂停长期未处理待办').checked=false;
  const changed=JSON.parse(JSON.stringify(h.run('readSettings()')));assert.equal(changed.proactive_checks.enabled,false);
  assert.deepEqual(changed.proactive_checks.ignored_types,[{type:'commitment',until:null}]);
  find(h.context.form,n=>n.attributes['aria-label']==='免打扰结束').value='22:00';
  assert.throws(()=>h.run('readSettings()'),/免打扰/);
}
async function evidenceUsesReadableQuotesAndRetainsLegacyRefs() {
  const h=harness();h.context.item={...record,evidence_refs:['message:1'],evidence_details:[{ref:'message:1',text:'今天下午发报价 <img src=x>',at:1234}]};
  h.run('showProactiveCard(item)');const box=h.node('proactive-draft');
  assert(find(box,'今天下午发报价 <img src=x>'),'依据优先显示用户原话');assert(find(box,'时间 1234'),'依据显示来源时间');
  assert(!find(box,'message:1'),'有原文时不向用户暴露技术引用ID');assert(!find(box,n=>n.tag==='img'));
  h.run('showProactiveCard({...item,evidence_details:[]})');assert(find(box,'message:1'),'旧接口保留依据兼容');
}
async function nativeReminderLinksBackWithoutChangingOrdinaryReminders() {
  const h=harness();let reads=0,opened;h.context.openProactive=async id=>{opened=id;};
  h.context.notices=[{notification_id:1,source:'reminder',reminder_id:'linked',title:'跟进报价',due_at:123,read_at:null},
    {notification_id:2,source:'reminder',reminder_id:'ordinary',title:'喝水',due_at:123,read_at:null}];
  h.context.api=async(method,url)=>url==='/v1/proactive'?(reads++,{items:[{...record,reminder_id:'linked'}]}):{items:h.context.notices};
  await h.run('refreshNotifications()');const list=h.node('notification-list');
  const linked=list.children.find(row=>find(row,'跟进报价')),ordinary=list.children.find(row=>find(row,'喝水'));
  assert(find(linked,'处理'),'原生到点通知应能继续业务处理');assert(!find(ordinary,'处理'),'普通原生提醒不增加业务动作');
  await find(linked,'处理').listeners.click();assert.equal(opened,record.id);assert.equal(reads,1,'多条原生通知共用一次关联查询');
  h.run('notificationAlerts.snapshotReady=true;notificationAlerts.watermark=2');
  h.context.extra={...h.context.notices[0],notification_id:3};h.run('observeNotificationAlerts([extra])');
  await new Promise(resolve=>setImmediate(resolve));const alert=find(h.node('notification-alert-list'),n=>n.dataset.notificationId==='3');
  assert(find(alert,'处理'),'悬浮原生提醒同样保留业务处理入口');assert(find(alert,'查看通知'));
  assert.equal(reads,1,'通知中心与浮层共用关联缓存');
  let finish;h.run('resetProactive()');h.context.api=()=>new Promise(resolve=>finish=resolve);
  const late=h.run('refreshNotifications()');finish({items:h.context.notices});await new Promise(resolve=>setImmediate(resolve));
  const oldRow=h.node('notification-list').children.find(row=>find(row,'跟进报价'));
  h.run('conn.epoch++;resetProactive();resetReminders()');finish({items:[{...record,reminder_id:'linked'}]});await late;
  assert(!find(oldRow,'处理'),'身份切换后的旧关联查询不能回填按钮');
}
async function handledDelayedNoticesNeverResurfaceAndFailuresWait() {
  const h=harness();let reads=0;
  h.context.notice={notification_id:8,title:'已完成的报价',source:'proactive_followup',proactive_followup_id:'followup:1',read_at:null,alert_after:Date.now()/1000+100};
  h.run('observeNotificationAlerts([notice],{snapshot:true})');
  assert.equal(h.run('notificationAlerts.delayed.size'),1);
  h.context.api=async(method,url)=>{reads++;assert.equal(url,'/v1/notifications?after_id=7&limit=1');return {items:[{...h.context.notice,attention_active:false}]};};
  const [timerId,timer]=[...h.timers.entries()][0];h.timers.delete(timerId);await timer.callback();
  assert.equal(reads,1,'到点展示前须查询最新关注状态');assert.equal(h.run('notificationAlerts.delayed.size'),0);
  assert.equal(h.node('notification-alert-list').children.length,0,'用户完成后不能再弹旧跟进');
  h.run('observeNotificationAlerts([{...notice,notification_id:9}])');
  h.run('observeNotificationAlerts([{...notice,notification_id:9,attention_active:false}])');
  assert.equal(h.run('notificationAlerts.delayed.size'),0,'补查失效状态立即取消等待队列');assert.equal(h.timers.size,0);
  h.run('observeNotificationAlerts([{...notice,notification_id:10,alert_after:null}])');assert.equal(h.node('notification-alert-list').children.length,1);
  h.run('observeNotificationAlerts([{...notice,notification_id:10,attention_active:false}])');assert.equal(h.node('notification-alert-list').children.length,0,'已显示旧卡也要撤回');
  h.run('observeNotificationAlerts([{...notice,notification_id:11,source:"reminder",proactive_followup_id:null}])');
  h.context.api=async()=>{throw new Error('网络断开');};
  const [failedId,failedTimer]=[...h.timers.entries()][0];h.timers.delete(failedId);await failedTimer.callback();
  assert.equal(h.node('notification-alert-list').children.length,0,'核验失败不能信任旧快照弹出');assert.equal(h.run('notificationAlerts.delayed.size'),1);
  const [retryId,retry]=[...h.timers.entries()][0];assert.equal(retry.delay,15000,'读取失败保留等待并安排重试');h.timers.delete(retryId);
  h.context.api=async()=>({items:[{...h.context.notice,notification_id:11,source:'reminder',proactive_followup_id:null,alert_after:null}]});
  await retry.callback();assert(find(h.node('notification-alert-list'),'已完成的报价'),'普通提醒未提供attention_active时仍兼容有效');
  h.run('resetReminders()');
  h.run('observeNotificationAlerts([{...notice,notification_id:12}],{snapshot:true})');
  let finish;h.context.api=()=>new Promise(resolve=>finish=resolve);
  const [flightId,flightTimer]=[...h.timers.entries()][0];h.timers.delete(flightId);const inFlight=flightTimer.callback();
  h.run('observeNotificationAlerts([{...notice,notification_id:12,attention_active:false}])');
  finish({items:[{...h.context.notice,notification_id:12,alert_after:null,attention_active:true}]});await inFlight;
  assert.equal(h.node('notification-alert-list').children.length,0,'核验请求飞行中收到失效状态后，迟到有效快照不能复活旧通知');
  assert.equal(h.run('notificationAlerts.delayed.size'),0);
}
(async()=>{await handledDelayedNoticesNeverResurfaceAndFailuresWait();await evidenceUsesReadableQuotesAndRetainsLegacyRefs();await nativeReminderLinksBackWithoutChangingOrdinaryReminders();await settingsUseAvailableChannelsAndPreserveExpiry();await conflictsAndLateWritesStayScoped();await decisionsRequireSpecificTimeAndRetry();await completionAndHistoryRecovery();await identityAndConversationIsolation();await notificationsStayUnifiedAndDelayed();console.log('proactive behavior passed');})().catch(e=>{console.error(e);process.exitCode=1;});
