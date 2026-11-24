// 真实页面脚本的原生确认、身份代际、首次权限快照及协作未读回归。
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {harness,find}=require('./work_task_behavior.cjs');
function setup(){const h=harness();Object.assign(h.context,{TextEncoder,isAdmin:()=>false,reminderState:{generation:1},setNotificationsOpen:()=>{},addMessage:()=>{}});
 for(const name of ['connections','shared-matters','shared-materials','collaboration-core','handoffs','handoff-results','organizational-approvals','collaboration-notifications','shared-notifications'])vm.runInContext(fs.readFileSync(path.join(process.argv[2],name+'.js'),'utf8'),h.context);
 return h;}
(async()=>{
 const h=setup();let calls=[];
 h.context.api=async(method,url,options)=>{calls.push({method,url,options});return {items:[{id:'h1',title:'收到工作',status:'submitted'}],total:42,enabled:true,next_cursor:null};};
 await h.run('openCollaboration()');assert(find(h.node('collaboration-list'),'工作交接（42）'));assert(calls.every(c=>c.method==='GET'));
 h.context.api=async()=>({items:[{id:'ended',title:'已结束工作的历史记录',status:'signed_off',blocked_reason:'source_access_revoked'}],total:1,enabled:true});
 await h.run('openCollaboration()');assert(find(h.node('collaboration-list'),'已签收'));assert(!find(h.node('collaboration-list'),'需要重新安排'));
 h.context.host=h.node('confirm');h.run('collabReview(host,"接收工作","/v1/handoffs/h/accept",{request_id:"fixed",confirmed:true,expected_version:1},["接收本人"],()=>{})');
 assert.equal(calls.length,1,'查看确认不产生写入');const confirm=find(h.node('confirm'),'确认接收工作');let sent=[];
 h.context.api=async(method,url,options)=>{sent.push(JSON.stringify(options.json));throw new Error('断线');};await confirm.listeners.click();await confirm.listeners.click();assert.equal(sent[0],sent[1]);
 h.context.api=async()=>{throw Object.assign(new Error('来源失效'),{status:409});};await confirm.listeners.click();assert.equal(confirm.disabled,true);assert.equal(h.run('collabState.current'),null);
 let release;h.context.api=async()=>new Promise(resolve=>release=resolve);const pending=h.run('openCollaboration()');h.run('conn.epoch++;resetCollaboration()');release({items:[{id:'old',title:'上一员工私密'}],total:1,enabled:true});await pending;assert(!find(h.node('collaboration-list'),'上一员工私密'));
 h.run('collabInbox.permission=null;collabState.current=null;$("collaboration-panel").classList.remove("hidden");$("collaboration-list").append(cNode("p","首次快照前的旧正文"));collaborationPermission("already-revoked")');
 assert(!find(h.node('collaboration-list'),'首次快照前的旧正文'),'首次基线不能留下撤权正文');
 h.context.host=h.node('draft');h.run('collabState.kind="handoff";collabState.current={id:"h1",shared_id:"s1",version:3,allowed_actions:["publish_result"]};handoffTextPublication(host,{id:"s1"},"ctx1",{id:"message1",text:"固定草稿"})');
 assert(find(h.node('draft'),'将此草稿发布为本次交接成果'));assert.equal(sent.length,2,'展示草稿按钮不自动发布');
 h.run('sharedInbox.permission=null;$("shared-panel").classList.remove("hidden");$("shared-list").append(sharedNode("p","首次共享旧正文"));sharedPermissionChanged("revoked")');assert(!find(h.node('shared-list'),'首次共享旧正文'));
 // 刷新统一通知，只有协作未读也必须显示入口提示。
 vm.runInContext(fs.readFileSync(path.join(process.argv[2],'reminders.js'),'utf8'),h.context);
 h.context.observeNotificationAlerts=()=>{};h.context.notificationCenterOpen=()=>false;
 h.context.api=async()=>({items:[],collaboration:{items:[],total:1,unread_total:1,permission_revision:'same'}});
 h.node('notification-list').appendChild=function(item){this.append(item);};
 await h.run('refreshNotifications()');assert.equal(h.node('notification-toggle').textContent,'通知 · 有未读');
 console.log('collaboration native behavior passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
