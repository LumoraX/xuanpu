// 实际脚本的原生确认、权限隔离及断线重试，未使用生产账号。
"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=path.join(process.argv[2],'capabilities.js');
assert.ok(fs.existsSync(source),'尚未实现能力原生入口');
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.value='';this.disabled=false;this.textContent='';this.files=[];this.classList={add(){},remove(){}};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  addEventListener(name,fn){this.events[name]=fn;}
  setAttribute(){}
  scrollIntoView(){}
  remove(){this.removed=true;}
  async click(){if(!this.disabled&&this.events.click)await this.events.click({preventDefault(){}});}
}
const elements=new Map(),calls=[];
let admin=false,reader=null,counter=0;
const c={console,JSON,Map,Set,Number,Object,Array,String,Date,Blob,URL,
  document:{createElement:tag=>new Element(tag),addEventListener(){}},
  crypto:{randomUUID:()=>String(++counter)},conn:{epoch:1,me:{user_id:'usr_alice'}},isAdmin:()=>admin,
  $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  api:async(method,url,opts)=>{calls.push({method,url,opts});if(reader)return reader(method,url,opts);
    if(url==='/v1/me/capabilities')return {capabilities:[],total:0,status:'not_assigned'};
    return {items:[],installations:[],total:0,next_cursor:null,configuration_enabled:true};},
};
c.connectionText=(tag,text)=>{const n=new Element(tag);n.textContent=text;return n;};
c.connectionButton=(text,action)=>{const n=c.connectionText('button',text);n.addEventListener('click',action);return n;};
vm.createContext(c);vm.runInContext(fs.readFileSync(source,'utf8'),c);
const text=node=>node.textContent+node.children.map(text).join('');
const all=node=>[node,...node.children.flatMap(all)];
async function main(){
  await c.openCapabilities();
  assert.ok(text(c.$('capability-list')).includes('尚未分配'));
  assert.equal(calls.filter(x=>x.url==='/v1/capabilities').length,0,'员工不能请求管理目录');
  let finish;reader=()=>new Promise(resolve=>{finish=resolve;});
  const pending=c.openCapabilities();c.resetCapabilities();finish({capabilities:[],total:0});await pending;
  assert.equal(c.$('capability-list').children.length,0,'迟到响应不能回填旧身份');
  reader=null;admin=true;await c.openCapabilities();
  const host=c.$('capability-list');
  assert.ok(text(host).includes('登记能力'));
  const before=calls.length;
  const reviewHost=new Element();let attempts=0;
  reader=async(method,url)=>{attempts++;if(attempts===1)throw Object.assign(new Error('网络中断'),{status:503});return {};};
  c.capabilityReview(reviewHost,'保存配置','/v1/capabilities/cap-test/installations',
    {request_id:'fixed-request',preview_id:'ipr-test',expected_version:0,confirmed:true},['固定版本，无执行授权']);
  assert.equal(calls.length,before,'显示确认卡不能提前提交');
  const submit=all(reviewHost).find(n=>n.tagName==='button'&&n.textContent.includes('确认'));
  await submit.click();await submit.click();
  const writes=calls.slice(before).filter(x=>x.method==='POST');
  assert.equal(writes.length,2);assert.equal(writes[0].opts.json.request_id,writes[1].opts.json.request_id,'断线重试保留操作号');
  assert.equal(writes[1].opts.json.confirmed,true);
  console.log('R4原生确认、员工隔离、迟到响应与原操作重试通过');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
