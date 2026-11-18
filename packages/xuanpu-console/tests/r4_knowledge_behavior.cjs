// 知识与记忆原生入口的实际脚本行为：迟到响应不回填、断线重试沿用操作号、依据失效先清屏，未使用生产账号。
"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=path.join(process.argv[2],'knowledge.js');
assert.ok(fs.existsSync(source),'尚未实现知识与记忆原生入口');
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.value='';this.checked=false;this.disabled=false;this.textContent='';this.classList={add(){},remove(){}};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  addEventListener(name,fn){this.events[name]=fn;}
  scrollIntoView(){}
  remove(){this.removed=true;}
}
const elements=new Map(),calls=[];
let reader=null,counter=0;
const defaults=(method,url)=>{
  if(url==='/v1/me/knowledge/sources')return {sources:[],total:0};
  if(url==='/v1/me/work-memory')return {memories:[{id:'wm_1',kind:'user_assertion',text:'周会在周一',status:'active',version:1}],total:1};
  if(url==='/v1/me/knowledge/search')return {hits:[{source_id:'ksr_1',ordinal:0,title:'口径.txt',revision:1,excerpt:'按到账日确认'}],total:1};
  if(url==='/v1/me/knowledge/contexts')return {id:'kcx_1',status:'active',version:1};
  return {id:'kan_1',question:'口径？',text:'按到账日 [1]',grounded:true,citations:[{index:1,title:'口径.txt',revision:1,ordinal:0}]};
};
const c={console,JSON,Map,Set,Number,Object,Array,String,
  document:{createElement:tag=>new Element(tag),addEventListener(){}},
  crypto:{randomUUID:()=>'req-'+(++counter)},conn:{epoch:1,me:{user_id:'usr_alice'}},
  $:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  api:async(method,url,opts)=>{calls.push({method,url,opts});return reader?reader(method,url,opts):defaults(method,url);},
};
c.connectionText=(tag,text)=>{const n=new Element(tag);n.textContent=text;return n;};
c.connectionButton=(text,action)=>{const n=c.connectionText('button',text);n.addEventListener('click',action);return n;};
vm.createContext(c);vm.runInContext(fs.readFileSync(source,'utf8'),c);
const text=node=>node.textContent+node.children.map(text).join('');
const all=node=>[node,...node.children.flatMap(all)];
async function main(){
  await c.openKnowledge();
  assert.ok(text(c.$('knowledge-memories')).includes('本人陈述'),'本人陈述须标明未经核实');
  let finish;reader=()=>new Promise(resolve=>{finish=resolve;});
  const pending=c.openKnowledge();c.resetKnowledge();finish({sources:[{id:'x',title:'旧身份',revision:1,status:'ready',chunks:1,version:1}]});await pending;
  assert.ok(!text(c.$('knowledge-sources')).includes('旧身份'),'迟到响应不能回填旧身份');
  reader=null;await c.openKnowledge();
  c.$('knowledge-question').value='口径？';
  await c.knowledgeAsk();
  assert.equal(calls.filter(x=>x.url==='/v1/me/knowledge/contexts').length,0,'未选依据不能建问答');
  c.$('knowledge-query').value='到账';await c.knowledgeSearch();
  const box=all(c.$('knowledge-hits')).find(n=>n.tagName==='input');box.checked=true;box.events.change();
  let failOnce=true;
  reader=(method,url,opts)=>{if(url.endsWith('/ask')&&failOnce){failOnce=false;throw Object.assign(new Error('网络中断'),{status:503});}return defaults(method,url);};
  await c.knowledgeAsk();await c.knowledgeAsk();
  const creates=calls.filter(x=>x.url==='/v1/me/knowledge/contexts'),asks=calls.filter(x=>x.url.endsWith('/ask'));
  assert.equal(creates.length,1,'重试不能重复建问答');
  assert.equal(JSON.stringify(creates[0].opts.json.pins),JSON.stringify([{source_id:'ksr_1',ordinal:0}]));
  assert.equal(asks.length,2);assert.equal(asks[0].opts.json.request_id,asks[1].opts.json.request_id,'断线重试沿用同一次提问');
  assert.ok(text(c.$('knowledge-answer')).includes('[1] 口径.txt'),'回答须显示依据');
  reader=(method,url)=>{if(url.endsWith('/ask'))throw Object.assign(new Error('来源失效'),{status:403,code:'knowledge_source_revoked'});return defaults(method,url);};
  await c.knowledgeAsk();
  assert.equal(c.$('knowledge-answer').children.length,0,'依据失效须先清掉旧回答');
  assert.ok(c.$('knowledge-feedback').textContent.includes('重新检索'));
  console.log('知识与记忆：迟到响应、未选依据、重试操作号、依据失效清屏通过');
}
let finished=false;
// 有未完成的等待时 Node 会静默退出：没跑完全部断言也算失败
process.on('exit',()=>{if(!finished){console.error('行为脚本没有跑完');process.exitCode=1;}});
main().then(()=>{finished=true;}).catch(error=>{console.error(error);process.exitCode=1;});
