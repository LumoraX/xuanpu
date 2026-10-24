// 实际原生脚本验证确认、重试、身份代次、固定材料与降权清屏。
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {harness,find}=require('./work_task_behavior.cjs');
function setup(){const h=harness();h.context.TextEncoder=TextEncoder;h.context.document.addEventListener=()=>{};h.context.document.querySelector=selector=>h.node(selector);h.context.reminderState={generation:1};h.context.setNotificationsOpen=()=>{};
 for(const name of ['connections','shared-matters','shared-materials','shared-conversation','shared-notifications'])vm.runInContext(fs.readFileSync(path.join(process.argv[2],name+'.js'),'utf8'),h.context);
 return h;}
(async()=>{
 const h=setup();let calls=[];
 h.context.api=async(method,url,options)=>{calls.push({method,url,options});return{items:[{id:'visible',title:'同事共同核对'}],total:42,enabled:true,next_cursor:null};};
 await h.run('openShared()');assert(calls.every(v=>v.method==='GET'));assert(find(h.node('shared-list'),'共享事项（42）'),'总数不能用当前页长度替代');
 h.context.host=h.node('review');h.run('sharedReview(host,"新建","POST","/v1/shared-matters",{request_id:"fixed",confirmed:true,title:"明确名称"},["明确名称"])');
 assert.equal(calls.length,1,'查看确认卡不能产生写入');const apply=find(h.node('review'),'确认新建');assert(apply);
 let sent=[];h.context.api=async(method,url,options)=>{sent.push(JSON.stringify(options.json));throw new Error('断线');};
 await apply.listeners.click();await apply.listeners.click();assert.equal(sent[0],sent[1]);
 const conflict=Object.assign(new Error('冲突'),{status:409});h.context.api=async()=>{throw conflict;};await apply.listeners.click();assert.equal(apply.disabled,true,'冲突须显式重选，不能自动换新版本');
 let release;h.context.api=async()=>new Promise(resolve=>release=resolve);const pending=h.run('openShared()');h.run('conn.epoch++;resetShared()');release({items:[{id:'old',title:'上一员工私密'}],total:1,enabled:true});await pending;assert(!find(h.node('shared-list'),'上一员工私密'));
 h.context.matter={id:'shared',title:'资料',version:19,allowed_actions:['read_materials','start_context']};
 h.context.api=async()=>({items:[{id:'material',name:'固定.txt',revisions:[{id:'rev1',revision:1,size:10,sha256:'oldsha'},{id:'rev2',revision:2,size:11,sha256:'newsha'}]}],total:1,next_cursor:null});
 h.run('sharedState.generation++;sharedState.matter=matter');await h.run('sharedMaterials($("materials"),matter,conn.epoch,sharedState.generation)');
 const checks=h.node('materials').querySelectorAll().filter(n=>n.type==='checkbox');checks[0].checked=true;
 const choose=find(h.node('materials'),'核对并开始阅读');await choose.listeners.click();
 const confirm=find(h.node('materials'),'确认开始阅读');assert(confirm);let selected;
 h.context.api=async(method,url,options)=>{selected=options.json;throw new Error('断线');};await confirm.listeners.click();assert.deepEqual(Array.from(selected.material_revision_ids),['rev1']);assert.equal(selected.expected_version,19);
 h.run('sharedInbox.permission="before";$("shared-panel").classList.remove("hidden");$("shared-list").append(sharedNode("p","旧全量正文"));sharedPermissionChanged("after")');
 assert(!find(h.node('shared-list'),'旧全量正文'),'降权提示必须清理已渲染的旧投影与确认卡');assert.equal(h.run('sharedState.matter'),null);
 h.context.api=async()=>{throw Object.assign(new Error('无权读取'),{status:404});};await h.run('openSharedMatter("revoked")');assert(!find(h.node('shared-list'),'旧全量正文'));
 h.run('resetSharedNotifications()');
 const stable1=await h.run('sharedDraftRequest("proposal",{title:"固定",content:"正文"})');
 const stable2=await h.run('sharedDraftRequest("proposal",{title:"固定",content:"正文"})');
 assert.equal(stable1,stable2);assert.notEqual(stable1,await h.run('sharedDraftRequest("proposal",{title:"修改",content:"正文"})'));
 assert.equal(h.run('sharedInbox.cursor'),0);
 // 拒绝已清屏时，同代际另一个迟到补读也不能重新填回材料。
 h.run('sharedState.generation++;sharedState.matter=matter');let late;
 h.context.api=async()=>new Promise(resolve=>late=resolve);
 const delayed=h.run('sharedMaterials($("shared-list"),matter,conn.epoch,sharedState.generation)');
 h.context.api=async()=>{throw Object.assign(new Error('已撤权'),{status:404});};
 h.run('sharedReview($("denied"),"保存","PATCH","/denied",{request_id:"deny"},[])');
 await find(h.node('denied'),'确认保存').listeners.click();
 late({items:[{id:'late',name:'迟到私密材料',revisions:[]}],total:1,next_cursor:null});await delayed;
 assert(!find(h.node('shared-list'),'迟到私密材料'),'失权必须递增代际，不能只清屏');
 vm.runInContext(fs.readFileSync(path.join(process.argv[2],'workspace-chat.js'),'utf8'),h.context);
 let queried=[],generic=0;h.context.restoreWorkspaceProposal=async()=>{generic++;};
 h.context.api=async(method,url)=>{queried.push(url);return url.includes('/messages?')?{items:[
  {id:1,role:'assistant',text:'应隔离的共享正文',source_context_id:'ctx',sources:[{shared_id:'shared'}]},
  {id:2,role:'assistant',text:'共享草稿建议',metadata:{route:{shared_draft:{title:'建议',content:''},proposal_id:'stable-proposal'}}}
 ]}:{items:[]};};
 await h.run('loadMainConversation()');assert(queried[0].includes('private_only=true'));
 assert(!find(h.node('transcript'),'应隔离的共享正文'));assert.equal(generic,0,'共享草稿不能走私人proposal恢复接口');
 h.context.api=async()=>({items:[]});await h.run('sharedPrivatePublish($("private-files"),matter,conn.epoch,sharedState.generation)');
 console.log('shared matter behavior passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
