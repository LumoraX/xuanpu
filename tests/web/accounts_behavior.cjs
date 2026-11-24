// 用轻量 DOM 替身运行真实脚本：设置链接的令牌从地址栏抹掉、只经同源 cookie 请求提交，口令登录
// 不带 API key；完整 DOM 由浏览器验收。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { value: '', textContent: '', innerHTML: '', children: [],
    classList: { values: new Set(), add(v) {this.values.add(v);}, remove(v) {this.values.delete(v);}, toggle(v, yes) {(yes ?? !this.values.has(v)) ? this.values.add(v) : this.values.delete(v);}, contains(v) {return this.values.has(v);} }, listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
    querySelector() { return element(id + '-body'); }, querySelectorAll() { return []; },
    removeAttribute() {}, setAttribute() {}, appendChild() {}, dataset: {} });
  return elements.get(id);
}
const storage = () => ({ getItem() { return ''; }, setItem() {}, removeItem() {} });
const requests = [];
const replaced = [];
const context = vm.createContext({ console, URL, URLSearchParams, AbortController, DOMException,
  setTimeout, clearTimeout, localStorage: storage(), sessionStorage: storage(),
  location: { origin: 'https://xuanpu.example', pathname: '/console/', search: '?tenant=acme', hash: '#password_setup=tok-123', assign() {} },
  history: { replaceState(_state, _title, url) { replaced.push(url); } },
  document: { getElementById: element, querySelectorAll() { return []; }, createElement: element, addEventListener() {} },
  window: { addEventListener() {} },
  fetch: async (url, init) => {
    requests.push({ url, init });
    if (url.endsWith('/inspect')) return { ok: true, status: 200, text: async () => '{"username":"zhang","display_name":"张三","purpose":"setup","expires_at":1}' };
    if (url.endsWith('/v1/me')) return { ok: false, status: 401, text: async () => '{"error":{"message":"未登录"}}' };
    return { ok: true, status: 200, text: async () => '{"tenant_id":"acme","user_id":"usr_1"}' };
  },
});
for (const name of ['app.js', 'notification-alerts.js', 'notification-center.js', 'reminders.js', 'identity.js', 'accounts.js']) {
  vm.runInContext(fs.readFileSync(process.argv[2] + '/' + name, 'utf8'), context);
}
const run = code => vm.runInContext(code, context);
(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(replaced, ['/console/?tenant=acme'], '令牌必须从地址栏抹掉');
  const inspect = requests.find(request => request.url.endsWith('/v1/auth/password/setup/inspect'));
  assert.ok(inspect, '应先查看设置链接');
  assert.deepEqual(JSON.parse(inspect.init.body), { token: 'tok-123' });
  assert.equal(inspect.init.credentials, 'same-origin');
  assert.equal(inspect.init.headers['X-Requested-By'], 'xuanpu');
  assert.equal(inspect.init.headers.Authorization, undefined);
  assert.match(element('password-setup-who').textContent, /zhang/);
  element('setup-password').value = 'a long enough secret';
  element('setup-confirm').value = 'different secret';
  await element('password-setup-form').listeners.submit({ preventDefault() {} });
  assert.match(element('password-setup-notice').textContent, /不一致/);
  assert.equal(requests.some(request => request.url.endsWith('/v1/auth/password/setup')), false);
  element('setup-confirm').value = 'a long enough secret';
  await element('password-setup-form').listeners.submit({ preventDefault() {} });
  const redeem = requests.find(request => request.url.endsWith('/v1/auth/password/setup'));
  assert.deepEqual(JSON.parse(redeem.init.body), { token: 'tok-123', password: 'a long enough secret' });
  element('login-username').value = ' Zhang ';
  element('login-password').value = 'a long enough secret';
  await element('password-login').listeners.submit({ preventDefault() {} });
  const login = requests.find(request => request.url.endsWith('/v1/auth/password/login'));
  assert.deepEqual(JSON.parse(login.init.body), { tenant: 'acme', username: 'Zhang', password: 'a long enough secret' });
  assert.equal(login.init.headers.Authorization, undefined);
  assert.equal(element('login-password').value, '', '登录后清掉口令输入框');
  console.log('accounts behavior passed');
})().catch(error => { console.error(error); process.exit(1); });
