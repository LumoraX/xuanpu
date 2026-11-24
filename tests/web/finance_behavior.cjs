'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
function element(tag='div') {return {tag,children:[],listeners:{},dataset:{},value:'',disabled:false,textContent:'',classList:{add(){},remove(){}},append(...x){this.children.push(...x);},replaceChildren(...x){this.children=[...x];},setAttribute(){},addEventListener(k,f){this.listeners[k]=f;}};}
function harness(){const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
 const ctx=vm.createContext({console,crypto,Date,JSON,Map,Set,URLSearchParams,encodeURIComponent,AbortController,AbortSignal,DOMException,
  document:{createElement:element},$:node,conn:{epoch:1,ready:true,me:{user_id:'alice'},apiKey:'',controller:new AbortController()},
  workspace:{kind:'finance',generation:1},workspaceCurrent:()=>true,refreshWorkspace:async()=>{},
  wsText:(tag,text)=>Object.assign(element(tag),{textContent:text}),wsButton:(text,action)=>Object.assign(element('button'),{textContent:text,listeners:{click:action}}),
  fail:()=>{},api:async()=>({}),toast:()=>{}});
 vm.runInContext(fs.readFileSync(path.join(process.argv[2],'finance.js'),'utf8'),ctx);return {ctx,node,run:s=>vm.runInContext(s,ctx)};}
function find(n,text){for(const c of n.children){if(c.textContent===text)return c;const r=find(c,text);if(r)return r;}return null;}
const claim={id:'claim1',version:3,status:'draft',owner_id:'alice',legal_entity_id:'le',total:'100.00',allocation_ids:[],evidence:[],content:{reason:'出差',lines:[]}};
const plan={id:'plan1',digest:'sha256:plan',action:'submit_claim',target_id:'claim1',target_version:3,total:'100.00',executable:true,expires_at:'2099-01-01T00:00:00Z',effects:['保留票据额度，进入第一审批步骤'],blockers:[],required_approvals:['财务复核']};
async function previewNeedsSeparateClickAndRetryKeepsKey(){const h=harness(),writes=[];let execute=0;
 h.ctx.api=async(_m,p,o)=>{const op=p.split('/').pop();writes.push({op,...JSON.parse(JSON.stringify(o.json))});if(op==='getExpenseClaim')return {status:200,body:claim};if(op==='previewFinanceAction')return {status:201,body:plan};if(op==='executeFinanceAction'){if(++execute===1)throw new Error('timeout');return {status:202,body:{id:'op1',status:'succeeded',business_result:'succeeded'}};}return {status:200,body:claim};};
 h.run('financeState.current=JSON.parse('+JSON.stringify(JSON.stringify(claim))+')');
 await h.run('financePreview("submit_claim",{})');
 assert.equal(execute,0,'预检绝不能自动执行');
 assert(h.node('workspace-feedback').textContent.includes('尚未执行'),'预检状态必须明确尚未执行，不能被请求已受理覆盖');
 assert(find(h.node('workspace-content'),'保留票据额度，进入第一审批步骤'));
 const button=find(h.node('workspace-content'),'本人确认提交');assert(button);
 await button.listeners.click();await button.listeners.click();
 const requests=writes.filter(w=>w.op==='executeFinanceAction');assert.equal(requests.length,2);assert.deepEqual(requests[0],requests[1]);
 assert(!find(h.node('workspace-content'),'已批准'),'操作成功不能显示为已批准');
}
async function stalePreviewAndConflictAreDiscarded(){const h=harness();let resolve;h.ctx.api=async()=>new Promise(r=>resolve=r);h.run('financeState.current='+JSON.stringify(claim));
 const p=h.run('financePreview("submit_claim",{})');h.run('conn.epoch++;resetFinance()');resolve({status:200,body:claim});await assert.rejects(p,{name:"AbortError"});
 assert.equal(h.run('financeState.plan'),null);assert.equal(h.node('workspace-content').children.length,0);
 const j=harness();j.run('financeState.current='+JSON.stringify(claim)+';financeState.plan='+JSON.stringify(plan));j.ctx.api=async()=>({status:409,is_error:true,body:{error:{message:'版本冲突'}}});
 await j.run('financeExecute()');assert.equal(j.run('financeState.plan'),null);
}
async function changedInputDiscardsLatePreview(){const h=harness();let resolve;h.run('financeState.current='+JSON.stringify(claim));
 h.ctx.api=async(_m,p)=>p.endsWith('/getExpenseClaim')?{status:200,body:claim}:await new Promise(r=>resolve=r);
 const pending=h.run('financePreview("submit_claim",{})');await new Promise(r=>setImmediate(r));h.run('financeInvalidate()');resolve({status:201,body:plan});await pending;
 assert.equal(h.run('financeState.plan'),null,'预检中修改输入后，迟到预检不能恢复旧确认');
}
async function workflowDisplayUsesCurrentAssignedStep(){const h=harness(),record={...claim,status:'under_review',owner_id:'other',due_at:'2026-11-01T00:00:00Z',workflow:{id:'wf1',status:'active',current_step_id:'step2',steps:[{id:'step2',name:'部门负责人',approver_user_id:'alice',status:'pending'}],due_at:'2026-11-01T00:00:00Z'}};
 h.ctx.api=async()=>({status:200,body:record});h.ctx.review={workflow_step_id:'step1',target_version:3,blockers:[]};await h.run('financeDetail("claim1",review)');
 assert(!find(h.node('workspace-content'),'批准本步骤'),'旧队列步骤不能继续显示批准按钮');
 h.ctx.review={workflow_step_id:'step2',target_version:3,approver_user_id:'alice',due_at:record.due_at,blockers:[]};await h.run('financeDetail("claim1",review)');
 assert(find(h.node('workspace-content'),'批准本步骤'));assert(find(h.node('workspace-content'),'审批期限：2026-11-01T00:00:00Z'));
}
async function approvedOwnerCanWithdraw(){const h=harness();h.ctx.api=async()=>({status:200,body:{...claim,status:'approved'}});await h.run('financeDetail("claim1")');assert(find(h.node('workspace-content'),'撤回'),'审批后尚未核销的owner仍需无模型撤回入口');}
async function financeErrorsPreserveBusinessReason(){const h=harness();h.ctx.api=async()=>({status:422,is_error:true,body:{code:'POLICY_UNAVAILABLE',message:'公司尚未启用报销记录'}});await assert.rejects(h.run('financeCall("createExpenseClaim",{})'),{message:'公司尚未启用报销记录'});}
async function approvalBlockersStillAllowRejection(){const h=harness(),record={...claim,status:'under_review',owner_id:'other',workflow:{current_step_id:'step1',steps:[{id:'step1',name:'负责人',approver_user_id:'alice',status:'pending'}]}};
 h.ctx.api=async()=>({status:200,body:record});h.ctx.review={workflow_step_id:'step1',target_version:3,blockers:['审批期限已过']};await h.run('financeDetail("claim1",review)');
 assert(find(h.node('workspace-content'),'阻断：审批期限已过'));const approve=find(h.node('workspace-content'),'批准本步骤');assert(approve?.disabled,'阻断只禁用批准');
 const reject=find(h.node('workspace-content'),'驳回');assert(reject && !reject.disabled,'超时或票据阻断时仍应允许有原因的驳回预检');
}
(async()=>{await previewNeedsSeparateClickAndRetryKeepsKey();await approvalBlockersStillAllowRejection();await financeErrorsPreserveBusinessReason();await approvedOwnerCanWithdraw();await workflowDisplayUsesCurrentAssignedStep();await changedInputDiscardsLatePreview();await stalePreviewAndConflictAreDiscarded();console.log('finance behavior passed');})().catch(e=>{console.error(e);process.exitCode=1;});
