'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
function element(tag='div') {
  const classes=new Set();return {tag,children:[],listeners:{},dataset:{},value:'',textContent:'',disabled:false,checked:false,isConnected:true,
    classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,v)=>v?classes.add(x):classes.delete(x)},
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];},remove(){this.isConnected=false;},
    setAttribute(){},addEventListener(k,fn){this.listeners[k]=fn;},focus(){},querySelectorAll(){return this.children.flatMap(n=>[n,...(n.querySelectorAll?.()||[])]);}};
}
function harness() {
  const nodes=new Map(),timers=new Map();let next=0;
  const node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  const context=vm.createContext({console,crypto,Date,JSON,Map,Set,encodeURIComponent,URLSearchParams,
    document:{createElement:element},$:node,conn:{epoch:1,ready:true,me:{user_id:'alice'}},
    chat:{generation:1,routeContext:[]},workspace:{kind:'task',settings:{timezone:'Asia/Shanghai'}},
    workspaceChat:{attachments:[],matter:null},workspaceBase:'/v1/workspace',
    workspaceCurrent:()=>true,workspaceTime:value=>String(value),reminderWallTime:()=>'',reminderTimestamp:()=>2000,
    wsText:(tag,text,kind='')=>{const n=element(tag);n.textContent=text;n.className=kind;return n;},
    wsButton:(text,fn)=>{const n=element('button');n.textContent=text;n.listeners.click=fn;return n;},
    api:async()=>({items:[]}),fail(){},toast(){},refreshWorkspaceSummary:async()=>{},downloadWorkspaceFile:async()=>{},
    append(){},renderMessageFileCards(){},workspaceRouteAccepted(){},
    setTimeout:(callback)=>{timers.set(++next,callback);return next;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(fs.readFileSync(path.join(process.argv[2],'work-tasks.js'),'utf8'),context);
  return {context,node,timers,run:code=>vm.runInContext(code,context)};
}
function find(node,test){for(const child of node.children){if(typeof test==='string'?child.textContent===test:test(child))return child;const result=find(child,test);if(result)return result;}return null;}
async function lateResponseAfterIdentityChangeIsDiscarded() {
  const h=harness();let release,writes=0;
  h.context.api=async(method)=>{if(method==='POST')writes++;return await new Promise(resolve=>release=resolve);};
  const pending=h.run('refreshWorkTask("task1")');
  h.run('conn.epoch++;resetWorkTasks()');
  release({task_id:'task1',state_version:1,status:'running',text:'PRIVATE ALICE',artifacts:[]});
  await pending;
  assert.equal(h.run('workTaskState.records.size'),0);
  assert.equal(writes,0,'切页与身份清理不能请求取消后台任务');
}
async function uncertainDecisionRetriesExactPayload() {
  const h=harness(),writes=[];
  h.context.api=async(method,url,options)=>{if(method==='POST'){writes.push(JSON.parse(JSON.stringify(options.json)));if(writes.length===1)throw new Error('断线');return {...task,status:'succeeded',state_version:4};}return task;};
  const task={task_id:'task1',status:'waiting_user',state_version:3,text:'会议',artifacts:[],pending:[],
    action_candidates:[{action_id:'a1',candidate:{kind:'todo',title:'<img src=x onerror=bad()>',owner:'self',source:{quote:'原文'}}}]};
  h.context.task=task;h.run('workTaskState.records.set(task.task_id,task);const card=workTaskCard(task);$("host").append(card)');
  const form=find(h.node('host'),n=>n.tag==='form');
  const checkbox=find(form,n=>n.type==='checkbox');checkbox.checked=true;
  const title=find(form,n=>n.type==='text');title.value='用户编辑的标题';
  await form.listeners.submit({preventDefault(){}});
  assert.equal(writes.length,1);assert.equal(title.disabled,true);assert.equal(checkbox.disabled,true);
  title.value='迟到修改不能改变请求';
  await form.listeners.submit({preventDefault(){}});
  assert.deepEqual(writes[0],writes[1]);
  assert.equal(writes[0].choices[0].title,'用户编辑的标题');
  assert(!find(h.node('host'),n=>n.tag==='img'),'任务内容只能作为文本展示');
}
module.exports={harness,find};
if(require.main===module)(async()=>{await lateResponseAfterIdentityChangeIsDiscarded();await uncertainDecisionRetriesExactPayload();console.log('work task behavior passed');})().catch(error=>{console.error(error);process.exitCode=1;});
