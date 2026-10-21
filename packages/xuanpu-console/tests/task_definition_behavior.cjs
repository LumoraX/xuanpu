"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {harness,find}=require('./work_task_behavior.cjs');
function setup(){const h=harness();h.context.refreshWorkspace=async()=>{};h.run('workspace.generation=0');vm.runInContext(fs.readFileSync(path.join(process.argv[2],'task-definitions.js'),'utf8'),h.context);return h;}
const request={request_id:'draft1',confirmed:false,title:'每周材料汇总',template:{kind:'office',text:'按部门汇总',input_files:[{item_id:'f1',revision:2}]},trigger:{kind:'time',repeat:'weekly',at:'2030-01-02T09:00:00+08:00'},timezone:'Asia/Shanghai',input_policy:'fixed_revision',misfire_policy:'skip',overlap_policy:'skip',max_executions:3,max_attempts:3,max_tokens:300000,valid_until:'2030-01-31T09:00:00+08:00'};
const draft={request,display:{files:[{name:'部门材料.csv',revision:2}],capability:'材料处理',next_fire_at:1893546000,expires_at:1896051600}};
async function confirmationAndExactReplay(){
 const h=setup(),writes=[];h.context.draft=draft;
 h.context.api=async(method,url,options)=>{if(url==='/v1/task-definitions/preview')return draft;if(method==='POST'){writes.push(JSON.parse(JSON.stringify(options.json)));if(writes.length===1)throw new Error('断线');return {definition_id:'d1',title:request.title,confirmed:request,state_version:1,enabled:true};}return {items:[]};};
 h.run('showTaskDefinitionDraft(draft,$("host"))');
 assert.equal(writes.length,0,'显示草稿不能创建安排');
 let button=find(h.node('host'),'确认启用安排');assert(button);
 await button.listeners.click();assert.equal(writes.length,1);assert.equal(writes[0].confirmed,true);
 button=find(h.node('host'),'重试同一确认');assert(button);await button.listeners.click();
 assert.deepEqual(writes[0],writes[1]);assert(find(h.node('host'),'已启用安排：每周材料汇总'));
}
async function lateConfirmationDiscarded(){
 const h=setup();h.context.draft=draft;let release;
 h.context.api=async()=>new Promise(resolve=>release=resolve);
 h.run('showTaskDefinitionDraft(draft,$("host"))');const card=h.node('host').children[0];
 const pending=find(card,'确认启用安排').listeners.click();h.run('conn.epoch++;resetTaskDefinitions()');
 release({definition_id:'private',title:'OTHER USER',confirmed:request,state_version:1});await pending;
 assert(!find(card,'已启用安排：OTHER USER'));
}
async function managementDoesNotNestCards(){
 const h=setup();let record={definition_id:'d',title:'合成安排',enabled:true,cancelled:false,state_version:1,confirmed:request,executions:0};
 h.context.record=record;h.run('definitionState.enabled=true;$("host").append(taskDefinitionCard(record))');
 h.context.api=async()=>{record={...record,enabled:false,state_version:2};return record;};
 await find(h.node('host'),'暂停未来执行').listeners.click();
 const cards=h.node('host').querySelectorAll().filter(n=>n.tag==='article');assert.equal(cards.length,1,'管理操作不能反复嵌套卡片');
 assert(find(h.node('host'),'恢复原安排'));
}
async function capabilityShowsVerifiedSummary(){
 const h=setup();h.context.task={task_id:'c',text:'查询客户',status:'succeeded',state_version:2,result_summary:'客户C-R2，verified',artifacts:[]};
 h.run('$("host").append(workTaskCard(task))');assert(find(h.node('host'),'客户C-R2，verified'));
}
async function recoveryAndUnknown(){
 const h=setup();h.context.task={task_id:'t',text:'材料处理',status:'running',state_version:2,execution_stage:'outputs_staged',attempt_no:2,recovering:true,recovery_mode:'save_artifacts',artifacts:[]};
 h.run('$("host").append(workTaskCard(task))');assert(find(h.node('host'),'继续保存成果'));
 h.context.task={task_id:'u',text:'核实写入',status:'outcome_unknown',state_version:3,effects:[{action_id:'e',state:'unknown',can_reconcile:false}],artifacts:[]};
 h.run('$("unknown").append(workTaskCard(task))');assert(find(h.node('unknown'),'结果待核实'));
 assert(!find(h.node('unknown'),n=>n.tag==='button'&&/重试|重新执行/.test(n.textContent)));
}
async function disabledPaginationKeepsManagementReadOnly(){
 const h=setup();h.context.api=async(method,url)=>url.endsWith('/options')?{enabled:false}:{items:[{definition_id:'d',title:'旧安排',enabled:false,state_version:1,confirmed:request}]};
 await h.run('renderTaskDefinitionList(0,1)');
 await h.run('renderTaskDefinitionList(0,1,"old-page")');
 assert(!find(h.node('workspace-content'),'恢复原安排'));
 assert(!find(h.node('workspace-content'),'修改并重新确认'));
 assert(find(h.node('workspace-content'),'取消安排并停止在途'));
}
(async()=>{await disabledPaginationKeepsManagementReadOnly();await confirmationAndExactReplay();await lateConfirmationDiscarded();await recoveryAndUnknown();await capabilityShowsVerifiedSummary();await managementDoesNotNestCards();console.log('task definition behavior passed');})().catch(error=>{console.error(error);process.exitCode=1;});
