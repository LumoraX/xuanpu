'use strict';
// 内部试用只读报告：严格契约、未验边界与身份/页面迟到响应隔离。
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const codes=['company','expense_policy','expense_categories','approval_roles','collector_separation','grant_epochs','external_configuration','simulation_disabled','runtime_mode','runtime_instance_lock','runtime_workers','runtime_external_workers'];
const external=['formal_policy_approval','production_identity','database_and_original_isolation','offsite_disaster_recovery','real_model','production_load'];
const labels=['正式制度','生产身份','数据库与原件隔离','异机恢复','真实模型','生产负载'];
function element(tag='div'){
  // 禁止测试夹具容忍HTML插入；服务文本只能成为textContent。
  return {tag,children:[],listeners:{},value:'',disabled:false,textContent:'',append(...x){this.children.push(...x);},replaceChildren(...x){this.children=[...x];},setAttribute(){},addEventListener(k,f){this.listeners[k]=f;},set innerHTML(_value){throw new Error('禁止HTML注入');}};
}
function report(extra={}){
  return {legal_entity_id:'e',profile:'internal_records',generated_at:'2026-10-23T08:00:00Z',status:'not_verified',production_status:'not_verified',checks:codes.map(code=>({code,status:'passed',detail:'核对 '+code})),versions:[{kind:'expense',version:9,generation:4,enabled:true,digest:'a'.repeat(64)}],configuration_digest:'b'.repeat(64),external_verifications:[...external],runtime:{mode:'read_only',instance_lock_lost:false,receipt_worker:'running',notification_worker:'running',external_workers:'absent'},...extra};
}
function harness(profile='internal_records'){
  const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},calls=[];
  const ctx=vm.createContext({console,crypto,Date,JSON,Map,Set,AbortController,AbortSignal,DOMException,document:{createElement:element},$:node,conn:{epoch:1,ready:true,me:{user_id:'reviewer'},apiKey:'',controller:new AbortController()},workspace:{kind:'finance'},workspaceCurrent:()=>true,refreshWorkspace:async()=>{},wsText:(tag,text)=>Object.assign(element(tag),{textContent:text}),wsButton:(text,action)=>Object.assign(element('button'),{textContent:text,listeners:{click:action}}),fail:()=>{throw new Error('只读检查应在当前页显示错误');}});
  for(const name of ['finance.js','finance_operations.js'])vm.runInContext(fs.readFileSync(path.join(process.argv[2],name),'utf8'),ctx);
  const h={ctx,node,calls,run:s=>vm.runInContext(s,ctx),answer:async()=>({status:200,body:report()})};
  ctx.api=async(method,url,options)=>{calls.push({method,op:url.split('/').pop(),...JSON.parse(JSON.stringify(options.json))});return h.answer();};
  h.run('financeState.entity="e";financeState.capabilities={profile:'+JSON.stringify(profile)+'};financeOperationsToolbar($("workspace-toolbar"))');
  return h;
}
function find(n,value){if(n.textContent===value)return n;for(const c of n.children){const r=find(c,value);if(r)return r;}return null;}
function text(n){return [n.textContent,...n.children.map(text)].join(' ');}
function buttons(n){return [...(n.tag==='button'?[n.textContent]:[]),...n.children.flatMap(buttons)];}
async function click(h,label){const button=find(h.node('workspace-toolbar'),label)||find(h.node('workspace-content'),label);assert(button,'缺少按钮 '+label);return button.listeners.click();}
async function validReportIsReadOnlyAndUnverified(){
  const h=harness();await click(h,'内部试用检查');
  assert.deepEqual(h.calls,[{method:'POST',op:'getInternalRecordsReadiness',arguments:{legal_entity_id:'e'},confirmed:false}]);
  const rendered=text(h.node('workspace-content'));
  for(const value of ['内部试用检查','正式环境尚未验证','只读','收件','站内提醒','未装配','制度版本：9','制度代际：4','a'.repeat(64),'b'.repeat(64),'2026-10-23T08:00:00Z',...codes.map(code=>'核对 '+code),...labels])assert(rendered.includes(value),value);
  assert(!buttons(h.node('workspace-content')).some(value=>/批准|上线|执行/.test(value)));
  assert(!rendered.includes('已具备上线条件'));
  await click(h,'重新检查');assert.equal(h.calls.length,2);
  h.answer=async()=>({status:200,body:report({versions:[]})});await click(h,'重新检查');assert(text(h.node('workspace-content')).includes('尚无当前报销制度版本'));
}
async function generalHasNoEntryAndNoRequest(){
  const h=harness('general');assert(!find(h.node('workspace-toolbar'),'内部试用检查'));await h.run('financeInternalReadiness()');assert.equal(h.calls.length,0);
  assert(find(h.node('workspace-toolbar'),'财务提醒'));assert(find(h.node('workspace-toolbar'),'运行概览'));
}
async function blockedReportsRemainBlocked(){
  for(const code of codes){const h=harness(),value=report();value.status='blocked';value.checks.find(check=>check.code===code).status='blocked';h.answer=async()=>({status:200,body:value});await click(h,'内部试用检查');assert.match(h.node('workspace-feedback').textContent,/阻断/);assert(text(h.node('workspace-content')).includes('阻断 · 核对 '+code));assert(text(h.node('workspace-content')).includes('正式环境尚未验证'));}
}
async function malformedReportsShowErrors(){
  const mutations=[
    ()=>null,()=>[],value=>({...value,secret:'不得泄露'}),value=>({...value,legal_entity_id:'other'}),value=>({...value,profile:'general'}),value=>({...value,generated_at:'yesterday'}),value=>({...value,generated_at:'2026'}),value=>({...value,generated_at:'1'}),value=>({...value,generated_at:'2026-10-23'}),value=>({...value,status:'ready'}),value=>({...value,status:'blocked'}),value=>({...value,production_status:'passed'}),value=>({...value,configuration_digest:'bad'}),value=>({...value,checks:value.checks.slice(1)}),value=>({...value,checks:[...value.checks,value.checks[0]]}),value=>({...value,checks:value.checks.map((check,i)=>i?check:value.checks[1])}),
    value=>({...value,checks:value.checks.map((check,i)=>i?check:{...check,code:'unknown'})}),value=>({...value,checks:value.checks.map((check,i)=>i?check:{...check,status:'not_verified'})}),value=>({...value,checks:value.checks.map((check,i)=>i?check:{...check,status:'blocked'})}),value=>({...value,checks:value.checks.map((check,i)=>i?check:{...check,detail:7})}),value=>({...value,checks:value.checks.map((check,i)=>i?check:{...check,detail:''})}),value=>({...value,checks:value.checks.map((check,i)=>i?check:{...check,detail:'😀'.repeat(2001)})}),value=>({...value,checks:value.checks.map((check,i)=>i?check:{...check,raw:'secret'})}),
    value=>({...value,versions:null}),value=>({...value,versions:[...value.versions,...value.versions]}),value=>({...value,versions:[{...value.versions[0],kind:'settlement'}]}),value=>({...value,versions:[{...value.versions[0],version:0}]}),value=>({...value,versions:[{...value.versions[0],generation:'4'}]}),value=>({...value,versions:[{...value.versions[0],enabled:1}]}),value=>({...value,versions:[{...value.versions[0],digest:'A'.repeat(64)}]}),value=>({...value,versions:[{...value.versions[0],raw:'secret'}]}),
    value=>({...value,external_verifications:[]}),value=>({...value,external_verifications:[...external,external[0]]}),value=>({...value,external_verifications:external.map((item,i)=>i?item:external[1])}),value=>({...value,external_verifications:external.map((item,i)=>i?item:'unknown')}),value=>({...value,runtime:{...value.runtime,mode:'ready'}}),value=>({...value,runtime:{...value.runtime,instance_lock_lost:'false'}}),value=>({...value,runtime:{...value.runtime,receipt_worker:'healthy'}}),value=>({...value,runtime:{...value.runtime,notification_worker:null}}),value=>({...value,runtime:{...value.runtime,external_workers:'none'}}),value=>({...value,runtime:{...value.runtime,raw:'secret'}})
  ];
  for(const mutate of mutations){const h=harness();h.answer=async()=>({status:200,body:mutate(report())});await click(h,'内部试用检查');assert.match(h.node('workspace-feedback').textContent,/响应.*无效|格式.*错误/);assert(find(h.node('workspace-content'),'重试内部试用检查'));assert(!text(h.node('workspace-content')).includes('核对 company'));}
  // 任意缺失字段均为错误，不能由默认值补齐成功报告。
  for(const key of Object.keys(report())){const h=harness(),value=report();delete value[key];h.answer=async()=>({status:200,body:value});await click(h,'内部试用检查');assert.match(h.node('workspace-feedback').textContent,/响应.*无效|格式.*错误/,key);}
}
async function permissionsAndNetworkAllowRetry(){
  for(const failure of ['403','network']){const h=harness();h.answer=async()=>{if(failure==='network')throw new Error('连接中断');return {status:403,body:{error:{code:'FINANCE_ROLE_REQUIRED',message:'denied'}}};};await click(h,'内部试用检查');assert.match(h.node('workspace-feedback').textContent,failure==='403'?/财务复核岗位/:/连接中断/);assert.equal(h.calls.length,1);assert(!h.calls.some(call=>call.op==='getFinanceReadiness'));h.answer=async()=>({status:200,body:report()});await click(h,'重试内部试用检查');assert.equal(h.calls.length,2);assert(text(h.node('workspace-content')).includes('正式环境尚未验证'));}
}
async function pendingWriteBlocksNavigation(){
  const h=harness();h.run('financeState.pending={operation:"createExpenseClaim",arguments:{"Idempotency-Key":"keep"}}');const before=h.run('JSON.stringify({pending:financeState.pending,revision:financeState.revision})');await click(h,'内部试用检查');assert.equal(h.calls.length,0);assert.equal(h.run('JSON.stringify({pending:financeState.pending,revision:financeState.revision})'),before);assert(find(h.node('workspace-content'),'按原请求核对并重试'));
}
async function lateSuccessAndFailureStayQuiet(){
  for(const change of ['revision','company','epoch','generation','workspace','pending','profile'])for(const outcome of ['success','failure']){
    const h=harness();let resolve,reject;h.answer=()=>new Promise((r,j)=>{resolve=r;reject=j;});const old=click(h,'内部试用检查');await new Promise(r=>setImmediate(r));
    const changes={revision:'financeInvalidate()',company:'financeState.entity="f"',epoch:'conn.epoch++',generation:'financeState.generation++',workspace:'workspace.kind="other"',pending:'financeState.pending={operation:"save"}',profile:'financeState.capabilities={profile:"general"}'};h.run(changes[change]);h.node('workspace-content').replaceChildren(h.ctx.wsText('p','新页面'));h.node('workspace-feedback').textContent='新反馈';
    if(outcome==='success')resolve({status:200,body:report()});else reject(new Error('过期错误'));
    await old;assert.equal(text(h.node('workspace-content')),' 新页面',change+' '+outcome);assert.equal(h.node('workspace-feedback').textContent,'新反馈');
  }
  // 同一公司第二次请求和旧重试按钮也有独立页面代际。
  const h=harness();let release;h.answer=()=>new Promise(r=>release=r);const old=click(h,'内部试用检查');await new Promise(r=>setImmediate(r));h.answer=async()=>({status:200,body:report({configuration_digest:'c'.repeat(64)})});await click(h,'内部试用检查');release({status:200,body:report()});await old;assert(text(h.node('workspace-content')).includes('c'.repeat(64)));assert(!text(h.node('workspace-content')).includes('b'.repeat(64)));
  h.answer=async()=>{throw new Error('临时失败');};await click(h,'内部试用检查');const retry=find(h.node('workspace-content'),'重试内部试用检查');h.run('financeInvalidate()');const count=h.calls.length;await retry.listeners.click();assert.equal(h.calls.length,count);
}
async function maximumValidAndTextInjectionStayLiteral(){
  const h=harness(),value=report();value.legal_entity_id='😀'.repeat(128);value.checks=value.checks.map(check=>({...check,detail:'😀'.repeat(2000)}));value.versions[0].version=Number.MAX_SAFE_INTEGER;value.versions[0].generation=Number.MAX_SAFE_INTEGER;h.run('financeState.entity="😀".repeat(128)');h.answer=async()=>({status:200,body:value});await click(h,'内部试用检查');assert(!/响应.*无效/.test(h.node('workspace-feedback').textContent));assert(text(h.node('workspace-content')).includes('😀'.repeat(2000)));
  const payload='<img src=x onerror=alert(1)>';value.legal_entity_id='e';value.checks[0].detail=payload;h.run('financeState.entity="e"');await click(h,'内部试用检查');assert(text(h.node('workspace-content')).includes(payload));assert(!h.node('workspace-content').children.some(child=>child.tag==='img'));
  for(const mode of ['enabled','draining','read_only','disabled'])for(const worker of ['running','failed','not_started'])for(const presence of ['absent','present']){const g=harness(),body=report();body.runtime={mode,instance_lock_lost:true,receipt_worker:worker,notification_worker:worker,external_workers:presence};body.status='blocked';body.checks[0].status='blocked';g.answer=async()=>({status:200,body});await click(g,'内部试用检查');assert(!/响应.*无效/.test(g.node('workspace-feedback').textContent),'合法运行态枚举');}
}
(async()=>{for(const test of [validReportIsReadOnlyAndUnverified,generalHasNoEntryAndNoRequest,blockedReportsRemainBlocked,malformedReportsShowErrors,permissionsAndNetworkAllowRetry,pendingWriteBlocksNavigation,lateSuccessAndFailureStayQuiet,maximumValidAndTextInjectionStayLiteral]){await test();console.log(test.name+' passed');}console.log('finance internal readiness behavior passed');})().catch(error=>{console.error(error);process.exitCode=1;});
