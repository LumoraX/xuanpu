// 能力运营原生视图的实际脚本行为：只显示计数与用量、未知不当 0、停止/撤销须先确认再提交，未使用生产账号。
"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const dir=process.argv[2];
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.value='';this.disabled=false;this.textContent='';this.classList={add(){},remove(){}};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  addEventListener(name,fn){this.events[name]=fn;}
  scrollIntoView(){}
  remove(){this.removed=true;}
  async click(){if(!this.disabled&&this.events.click)await this.events.click({preventDefault(){}});}
}
const summary=(tasks,rate)=>({totals:{tasks,succeeded:1,failed:1,outcome_unknown:0,accepted:0,running:0,waiting_user:0},
  technical_completion:{rate},tokens:{settled:150,held_upper_bound:0,tasks_without_records:1},cost:{status:'price_unavailable',amount:null,currency:null}});
const elements=new Map(),calls=[];let counter=0;
const responses={
  '/v1/me/capabilities':{capabilities:[],total:0},
  '/v1/me/capability-outcomes?limit=20':{...summary(2,0.5),items:[{task_id:'t1',capability_id:'cap_x',capability_revision:1,status:'succeeded',tokens_settled:null}]},
  '/v1/capabilities?limit=50':{items:[],installations:[],total:0,next_cursor:null,configuration_enabled:false},
  '/v1/admin/capability-outcomes':{...summary(2,0.5),capabilities:[{capability_id:'cap_x',capability_revision:1,...summary(2,null)}]},
  '/v1/capabilities/cap_x/releases':{releases:[{id:'crl_1',capability_revision:1,status:'active',targets:[{user_id:'usr_a'}],version:3}],total:1},
  '/v1/capabilities/cap_x/assignments':{assignments:[{id:'cas_1',selector:{kind:'user',id:'usr_a'},allowed_revisions:[1],status:'active',version:2}],total:1},
};
const fetched=[],clicked=[];
const c={console,JSON,Map,Set,Number,Object,Array,String,Math,Promise,Blob,
  URL:{createObjectURL:()=>'blob:x',revokeObjectURL(){}},setTimeout:fn=>fn(),
  url:p=>p,requestOptions:()=>({}),headers:()=>({}),
  fetch:async p=>{fetched.push(p);return {ok:true,blob:async()=>'csv'};},
  document:{createElement:tag=>{const n=new Element(tag);if(tag==='a')n.click=()=>clicked.push(n.download);return n;},addEventListener(){}},
  crypto:{randomUUID:()=>'req-'+(++counter)},conn:{epoch:1,me:{user_id:'usr_admin'}},isAdmin:()=>true,
  $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  api:async(method,url,opts)=>{calls.push({method,url,opts});if(method==='POST')return {};
    if(!(url in responses))throw new Error('未预期的请求 '+url);return responses[url];},
};
c.connectionText=(tag,text)=>{const n=new Element(tag);n.textContent=text;return n;};
c.connectionButton=(text,action)=>{const n=c.connectionText('button',text);n.addEventListener('click',action);return n;};
vm.createContext(c);
for(const name of ['capabilities.js','capability-operations.js'])vm.runInContext(fs.readFileSync(path.join(dir,name),'utf8'),c);
const text=node=>node.textContent+node.children.map(text).join('');
const all=node=>[node,...node.children.flatMap(all)];
const button=(host,label)=>all(host).find(n=>n.tagName==='button'&&n.textContent===label);
async function main(){
  await c.openCapabilities(false);
  assert.ok(!calls.some(x=>x.url==='/v1/admin/capability-outcomes'),'员工视图不读运营统计');
  const mine=text(c.$('capability-list'));
  assert.ok(mine.includes('我的能力工作')&&mine.includes('无用量记录'),'员工看到本人结果，没有用量记录的不显示 0');
  await button(c.$('capability-list'),'导出本人记录（CSV）').click();
  assert.deepEqual([...fetched],['/v1/me/capability-outcomes/export']);assert.equal(clicked.at(-1),'我的能力工作.csv');
  await c.openCapabilities(true);
  const host=c.$('capability-list'),shown=text(host);
  assert.ok(shown.includes('技术完成率 50%')&&shown.includes('技术完成率 暂无'),'没有数据的完成率不能显示成 0');
  assert.ok(shown.includes('未配置价格表')&&!shown.includes('0 元'),'没有价格表时不估算费用');
  await button(host,'导出汇总（CSV）').click();
  assert.equal(fetched.at(-1),'/v1/admin/capability-outcomes/export');
  await button(host,'查看发布与分配').click();
  const before=calls.filter(x=>x.method==='POST').length;
  await button(host,'撤销发布').click();
  assert.equal(calls.filter(x=>x.method==='POST').length,before,'显示确认卡不能提前提交');
  await button(host,'确认撤销发布').click();
  const posts=calls.filter(x=>x.method==='POST');
  assert.equal(posts.at(-1).url,'/v1/capability-releases/crl_1/revoke');
  assert.equal(posts.at(-1).opts.json.expected_version,3,'带当前版本，不覆盖已变化的发布');
  // 确认成功后面板重读，需重新展开
  await button(host,'查看发布与分配').click();
  await button(host,'撤销分配').click();await button(host,'确认撤销分配').click();
  assert.equal(calls.filter(x=>x.method==='POST').at(-1).url,'/v1/capability-assignments/cas_1/revoke');
  console.log('能力运营：员工不读统计、未知不当 0、费用不估算、停止撤销先确认通过');
}
let finished=false;
process.on('exit',()=>{if(!finished){console.error('行为脚本没有跑完');process.exitCode=1;}});
main().then(()=>{finished=true;}).catch(error=>{console.error(error);process.exitCode=1;});
