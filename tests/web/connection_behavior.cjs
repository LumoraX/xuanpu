// 员工授权必须原生确认，身份切换后不接受迟到正文。
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {harness}=require('./work_task_behavior.cjs');
function setup(){const h=harness();h.context.isAdmin=()=>false;h.run('function resetConnectionsPlaceholder(){}');vm.runInContext(fs.readFileSync(path.join(process.argv[2],'connections.js'),'utf8'),h.context);return h;}
function find(node,text){if(node.textContent===text)return node;for(const child of node.children){const v=find(child,text);if(v)return v;}return null;}
(async()=>{
 const h=setup();let calls=[];h.context.api=async(method,path,options)=>{calls.push({method,path,options});return {enabled:true,connections:[{id:'c',name:'企业微信',status:'active',operations:['self_profile.read'],consents:[]}]};};
 await h.run('openConnections()');assert.equal(calls.length,1,'显示页面不能自动同意');
 const button=find(h.node('connections-list'),'授权本人资料只读（1小时）');assert(button);await button.listeners.click();assert.equal(calls.length,1,'查看授权范围不能自动同意');
 assert(find(h.node('connections-list'),'同意并允许读取本人资料'));
 let resolve;h.context.api=async()=>new Promise(done=>resolve=done);
 const pending=h.run('openConnections()');h.run('conn.epoch++;resetConnections()');resolve({enabled:true,connections:[{id:'private',name:'上一位员工的连接',status:'active',consents:[]}]});await pending;
 assert(!find(h.node('connections-list'),'上一位员工的连接'));
 h.context.task={task_id:'t',status:'accepted',text:'核对本人',state_version:1,block_reason:'connection_reauthorization_required',artifacts:[]};
 h.run('$("task-host").append(workTaskCard(task))');
 assert(find(h.node('task-host'),'查看本人连接'));
 assert(find(h.node('task-host'),'已重新授权，继续原工作'));
 let resumeCalls=[];h.context.api=async(method,path,options)=>{resumeCalls.push(options.json);throw new Error('断线');};
 const resume=find(h.node('task-host'),'已重新授权，继续原工作');await resume.listeners.click();await resume.listeners.click();
 assert.deepEqual(resumeCalls[0],resumeCalls[1],'继续确认重试必须保持原request_id');
 console.log('connection behavior passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
