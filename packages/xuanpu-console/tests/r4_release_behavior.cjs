// 能力分配、评测与发布原生表单的实际脚本行为：先确认再提交、名单按员工与代次固定、
// 发布带预览摘要与重叠发布、评测编号带入发布表单、预览不符不发布，未使用生产账号。
"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const dir=process.argv[2];
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.value='';this.checked=false;this.disabled=false;this.textContent='';this.files=[];this.classList={add(){},remove(){}};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  addEventListener(name,fn){this.events[name]=fn;}
  scrollIntoView(){}
  remove(){this.removed=true;}
  async click(){if(!this.disabled&&this.events.click)await this.events.click({preventDefault(){}});}
}
const elements=new Map(),calls=[];let counter=0,previewFails=false;
const users={users:[{user_id:'usr_a',display_name:'张三',lifecycle_epoch:2},{user_id:'usr_b',display_name:'李四',lifecycle_epoch:1}]};
const c={console,JSON,Map,Set,Number,Object,Array,String,Math,Promise,Blob,URL,
  document:{createElement:tag=>new Element(tag),addEventListener(){}},
  crypto:{randomUUID:()=>'req-'+(++counter)},conn:{epoch:1,me:{user_id:'usr_admin'}},isAdmin:()=>true,
  $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  api:async(method,url,opts)=>{calls.push({method,url,opts});
    if(url.startsWith('/v1/users'))return users;
    if(url==='/v1/capability-evaluations')return {id:'cev_1',status:'running'};
    if(url.endsWith('/release-previews')){if(previewFails)throw Object.assign(new Error('评测未通过'),{status:409});
      return {digest:'d'.repeat(64),capability_revision:1,changes:[{user_id:'usr_a',from_revision:null,to_revision:1}],supersedes:['crl_old']};}
    if(method==='POST')return {};
    if(url==='/v1/me/capabilities')return {capabilities:[],total:0};
    return {items:[],installations:[],total:0,next_cursor:null,configuration_enabled:true};},
};
c.connectionText=(tag,text)=>{const n=new Element(tag);n.textContent=text;return n;};
c.connectionButton=(text,action)=>{const n=c.connectionText('button',text);n.addEventListener('click',action);return n;};
vm.createContext(c);
for(const name of ['capabilities.js','capability-release.js'])vm.runInContext(fs.readFileSync(path.join(dir,name),'utf8'),c);
const all=node=>[node,...node.children.flatMap(all)];
const button=(host,label)=>all(host).filter(n=>n.tagName==='button'&&n.textContent===label).at(-1);
const posts=()=>calls.filter(x=>x.method==='POST');
async function main(){
  await c.openCapabilities(true);
  const item={capability_id:'cap_x',revision:1},installation={id:'ins_1',status:'staged',version:1};
  const card=new Element('article'),gen=()=>vm.runInContext('capabilityState.generation',c);
  await c.capabilityAssign(card,item,1,gen());
  all(card).find(n=>n.tagName==='input'&&n.value==='usr_a').checked=true;
  await button(card,'核对分配').click();
  assert.equal(posts().length,0,'显示确认卡不能提前提交');
  await button(card,'确认分配').click();
  const assign=posts().at(-1);
  assert.equal(assign.url,'/v1/capabilities/cap_x/assignments');
  assert.equal(JSON.stringify(assign.opts.json.selector),JSON.stringify({kind:'user',id:'usr_a',user_epoch:2}),'按员工与代次固定');
  const evalCard=new Element('article');
  c.capabilityEvaluate(evalCard,item,installation,1,gen());
  const file=all(evalCard).find(n=>n.tagName==='input');
  file.files=[{size:100,text:async()=>JSON.stringify({format:'xuanpu-eval-suite/v1',cases:[{name:'a'}]})}];
  await file.events.change();
  await button(evalCard,'确认发起评测').click();
  assert.equal(posts().at(-1).opts.json.installation_id,'ins_1');
  const releaseCard=new Element('article');
  await c.capabilityRelease(releaseCard,item,installation,1,gen());
  assert.equal(all(releaseCard).find(n=>n.tagName==='input'&&n.type==='text').value,'cev_1','评测编号带入发布表单');
  all(releaseCard).find(n=>n.tagName==='input'&&n.value==='usr_a').checked=true;
  previewFails=true;await button(releaseCard,'预览发布').click();
  assert.ok(c.$('capability-feedback').textContent.includes('不符合发布条件'));
  assert.ok(!posts().some(x=>x.url.endsWith('/releases')),'预览不通过不能发布');
  previewFails=false;await button(releaseCard,'预览发布').click();
  assert.ok(!posts().some(x=>x.url.endsWith('/releases')),'预览只读，确认前不发布');
  await button(releaseCard,'确认发布').click();
  const release=posts().at(-1);
  assert.equal(release.url,'/v1/capabilities/cap_x/releases');
  assert.equal(release.opts.json.preview_digest,'d'.repeat(64));
  assert.equal(JSON.stringify(release.opts.json.supersedes),JSON.stringify(['crl_old']));
  assert.equal(release.opts.json.confirmed,true);
  assert.equal(JSON.stringify(release.opts.json.targets),JSON.stringify([{user_id:'usr_a',user_epoch:2}]));
  console.log('能力分配、评测与发布：确认后提交、名单固定代次、预览摘要与重叠发布、预览不符不发布通过');
}
let finished=false;
process.on('exit',()=>{if(!finished){console.error('行为脚本没有跑完');process.exitCode=1;}});
main().then(()=>{finished=true;}).catch(error=>{console.error(error);process.exitCode=1;});
