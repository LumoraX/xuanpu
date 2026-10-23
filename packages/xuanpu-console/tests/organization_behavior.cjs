// 原生确认不得靠模型可用，也不能在身份切换后回填上一位员工的数据。
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {harness}=require('./work_task_behavior.cjs');
function setup(){
 const h=harness();h.context.isAdmin=()=>false;
 vm.runInContext(fs.readFileSync(path.join(process.argv[2],'connections.js'),'utf8'),h.context);
 vm.runInContext(fs.readFileSync(path.join(process.argv[2],'organization.js'),'utf8'),h.context);
 return h;
}
function find(node,text){if(node.textContent===text)return node;for(const child of node.children){const v=find(child,text);if(v)return v;}return null;}
(async()=>{
 const h=setup();let calls=[];
 h.context.api=async(method,url,options)=>{calls.push({method,url,options});return url.endsWith('/me')?{roles:[],memberships:[],units:[]}:{items:[],total:0};};
 await h.run('openOrganization()');assert(calls.every(c=>c.method==='GET'),'查看组织不能自动写入');
 h.context.target=h.node('org-test');
 h.run('organizationReview(target,"保存部门","PATCH","/v1/admin/organization/units/unit",{request_id:"same",expected_version:1,name:"新部门"},["新部门"])');
 assert.equal(calls.length,2,'查看确认卡不能自动提交');
 const apply=find(h.node('org-test'),'确认保存部门');assert(apply);
 let sent=[];h.context.api=async(method,url,options)=>{sent.push(JSON.stringify(options.json));throw new Error('断线');};
 await apply.listeners.click();await apply.listeners.click();assert.equal(sent[0],sent[1],'网络重试保持原操作号与版本');
 const error=new Error('版本冲突');error.status=409;h.context.api=async()=>{throw error;};
 await apply.listeners.click();assert.equal(apply.disabled,true,'冲突不能自动改用新版本覆盖');
 let done;h.context.api=async()=>new Promise(resolve=>done=resolve);
 const pending=h.run('openOrganization()');h.run('conn.epoch++;resetOrganization()');done({roles:[],memberships:[],units:[{name:"旧员工部门"}]});await pending;
 assert(!find(h.node('organization-list'),'旧员工部门'));
 console.log('organization behavior passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
