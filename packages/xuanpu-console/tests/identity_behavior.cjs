// 用轻量 DOM 替身运行真实脚本，验证凭据隔离；完整 DOM 由浏览器验收。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { value: '', textContent: '', innerHTML: '', children: [],
    classList: { values: new Set(), add(v) {this.values.add(v);}, remove(v) {this.values.delete(v);}, toggle(v, yes) {yes ? this.values.add(v) : this.values.delete(v);}, contains(v) {return this.values.has(v);} }, listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
    querySelector() { return element(id + '-body'); }, querySelectorAll() { return []; },
    removeAttribute() {}, setAttribute() {}, appendChild() {}, dataset: {} });
  return elements.get(id);
}
const storage = () => ({ getItem(key) { return key === 'xuanpu.baseUrl' ? 'https://old.example' : key === 'xuanpu.apiKey' ? 'old-admin-key' : ''; }, setItem() {}, removeItem() {} });
const context = vm.createContext({ console, URL, URLSearchParams, AbortController, DOMException,
  setTimeout, clearTimeout, localStorage: storage(), sessionStorage: storage(),
  location: { origin: 'http://localhost', pathname: '/console/', search: '', assign() {} },
  document: { getElementById: element, querySelectorAll() { return []; }, createElement: element },
  fetch: async () => ({ ok: false, status: 401, text: async () => '{"error":{"message":"未登录"}}' }),
});
vm.runInContext(fs.readFileSync(process.argv[2] + '/app.js', 'utf8'), context);
vm.runInContext(fs.readFileSync(process.argv[2] + '/identity.js', 'utf8'), context);
const run = code => vm.runInContext(code, context);
(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(run('conn.baseUrl'), '', '普通入口不得读取旧管理员地址');
  assert.equal(element('admin-connection').classList.contains('hidden'), true);
  assert.equal(run('headers()["Authorization"]'), undefined);
  assert.equal(run('headers()["X-Requested-By"]'), 'xuanpu');
  assert.equal(run('requestOptions().credentials'), 'same-origin');
  run('conn.apiKey = "demo"');
  assert.equal(run('headers().Authorization'), 'Bearer demo');
  assert.equal(run('requestOptions().credentials'), 'omit');
  assert.equal(run('safeAvatar("javascript:alert(1)")'), null);
  assert.equal(run('safeAvatar("http://example.com/a.png")'), null);
  assert.equal(run('safeAvatar("//example.com/a.png")'), null);
  assert.equal(run('safeAvatar("https://example.com/a.png")'), 'https://example.com/a.png');
  run('conn.baseUrl = "https://other.example"');
  assert.equal(run('sameOrigin()'), false);
  run('conn.baseUrl = ""; conn.apiKey = ""');
  run('$("send-btn").classList.add("hidden"); resetIdentity()');
  assert.equal(element('send-btn').classList.contains('hidden'), false);
  assert.equal(element('send-btn').disabled, true);
  let release;
  context.fetch = () => new Promise(resolve => { release = resolve; });
  const pending = run('api("GET", "/v1/sessions")');
  run('resetIdentity()');
  release({ok:true, text:async () => '{"sessions":[{"secret":"old"}]}'});
  await assert.rejects(pending, error => error.name === 'AbortError');
  run('conn.me = {role: "tenant_admin"}; $("users-status").value="disabled"; $("users-role").value="tenant_member"; $("users-unverified").value="true"');
  assert.match(run('usersQuery("next").toString()'), /status=disabled/);
  assert.match(run('usersQuery("next").toString()'), /after=next/);
  assert.match(run('usersQuery("next").toString()'), /unverified=true/);
  assert.match(run('userChangeNotice({status:"disabled", cleanup_failed:["sessions"]})'), /已停用.*sessions.*重试/);
  assert.match(run('userChangeNotice({status:"active", cleanup_failed:[]})'), /已启用/);
  let finishRead;
  let posts = 0;
  element('archive').files = [{arrayBuffer: () => new Promise(resolve => { finishRead = resolve; })}];
  context.fetch = async () => { posts++; return {ok:true, text:async () => '{}'}; };
  const upload = element('publish').listeners.submit({preventDefault() {}});
  run('resetIdentity()');
  finishRead(new ArrayBuffer(2));
  await upload;
  assert.equal(posts, 0, '身份切换后旧文件不得上传');
  let rejectLogout;
  context.fetch = () => new Promise((resolve, reject) => { rejectLogout = reject; });
  const logout = element('logout').listeners.click();
  run('resetIdentity(); conn.apiKey="new-key"; $("conn-status").textContent="新身份已连接"');
  rejectLogout(new DOMException('切换', 'AbortError'));
  await logout;
  assert.equal(element('conn-status').textContent, '新身份已连接');
  assert.equal(run('conn.apiKey'), 'new-key');
  let finishMe;
  run('conn.me={role:"tenant_admin", user_id:"self"}; conn.apiKey="old"');
  context.fetch = async target => {
    if (target.endsWith('/role')) return {ok:true, text:async () => '{"role":"tenant_member"}'};
    return new Promise(resolve => { finishMe = resolve; });
  };
  const change = run('changeUser({disabled:false}, {user_id:"self"}, "role", "tenant_member")');
  await new Promise(resolve => setTimeout(resolve, 0));
  run('resetIdentity(); $("users-notice").textContent="新身份"');
  finishMe({ok:true, text:async () => '{"role":"tenant_member"}'});
  await change;
  assert.equal(element('users-notice').textContent, '新身份');
  // 普通员工无需填写连接地址、密钥、业务包名；只选择已生效的业务。
  run('conn.me={role:"tenant_member", user_id:"member"}; applyPermissions()');
  assert.equal(element('admin-connection').classList.contains('hidden'), true);
  context.fetch = async () => ({ok:true, text:async () => JSON.stringify({packages:[
    {name:'old', active:false}, {name:'support-desk', active:true}
  ]})});
  await run('loadBusinessChoices()');
  assert.equal(element('new-package').value, 'support-desk');
  assert.equal(element('new-session-btn').disabled, false);
  assert(!element('new-package').innerHTML.includes('old'));
  context.fetch = async () => ({ok:true, text:async () => '{"packages":[]}'});
  await run('loadBusinessChoices()');
  assert.equal(element('new-session-btn').disabled, true);
  assert.match(element('business-hint').textContent, /管理员/);
  run('resetIdentity()');
  assert.equal(element('new-session-btn').disabled, true);
  run('conn.me={role:"tenant_admin"}');
  context.fetch = async () => ({ok:true, text:async () => JSON.stringify({packages:[
    {name:'new-business', version:'1.0.0', digest:'sha256:abc', active:true}
  ]})});
  await run('loadPackages()');
  assert.equal(element('new-session-btn').disabled, false, '发布或启用后无需重连');
  assert.equal(element('new-package').value, 'new-business');
  // 旧会话延迟返回时，不允许覆盖已切换会话的历史或审批。
  run('chat.sessionId="old-session"; chat.generation=70');
  let releaseHistory;
  context.fetch = () => new Promise(resolve => { releaseHistory = resolve; });
  const history = run('loadHistory("old-session", 70)');
  run('chat.sessionId="new-session"; chat.generation=71');
  element('div').textContent = '当前会话';
  releaseHistory({ok:true, text:async () => JSON.stringify({records:[{kind:'message_sent', data:{text:'旧会话秘密'}}], next_after_id:null})});
  await history;
  assert.equal(element('div').textContent, '当前会话');
  let releasePending;
  context.fetch = () => new Promise(resolve => { releasePending = resolve; });
  const approval = run('loadPending()');
  run('chat.sessionId="third-session"; chat.generation=72');
  element('pending').innerHTML = '当前会话审批';
  releasePending({ok:true, text:async () => '{"pending":[]}'});
  await approval;
  assert.equal(element('pending').innerHTML, '当前会话审批');
  let staleApprovalRequests = 0;
  context.fetch = async () => { staleApprovalRequests++; throw new Error('旧审批不应发请求'); };
  await run('resolve("old-request", true, "old-session", 70)');
  assert.equal(staleApprovalRequests, 0);
  run('chat.streaming={sessionId:"third-session"}');
  await element('new-session').listeners.submit({preventDefault() {}});
  assert.equal(staleApprovalRequests, 0, '回复期间新建不得悄悄创建额外会话');
  await run('closeSession()');
  assert.equal(staleApprovalRequests, 0, '回复期间关闭应引导先停止');
  run('clearTimeout(toast.timer)');
  assert.equal(run('identityStatus({user_id:"user", display_name:"张三", key_id:null})'), '已登录 · 张三（飞书）');
  assert.equal(run('identityStatus({user_id:null, key_id:"key"})'), '后台凭据已连接 · 非个人登录');
  assert.equal(run('identityStatus({user_id:"user", display_name:"张三", key_id:"delegated"})'), '后台凭据已连接 · 非个人登录');
  console.log('identity behavior passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
