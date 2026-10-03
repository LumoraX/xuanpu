// 真实浏览器验收：只走授权重定向拿 cookie，不注入平台会话。
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.argv[3]);
const state = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = path.dirname(process.argv[2]);
const {base, key, mock} = state;
const consumedCodes = [];

async function adminApi(method, route, data) {
  const response = await fetch(base + route, {method, headers: {Authorization: 'Bearer ' + key, 'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
  assert(response.ok, `${route}: ${response.status} ${await response.clone().text()}`);
  return response.json();
}

async function login(page, user, tenant = false) {
  await page.goto(base + '/console/' + (tenant ? '?tenant=acme' : ''));
  await page.locator('#feishu-login').click();
  await page.waitForURL(mock + '/open-apis/authen/v1/authorize?**');
  await page.locator('#' + user).click();
  await page.waitForURL(base + '/console/**');
  await page.waitForFunction(() => document.getElementById('conn-status').textContent.startsWith('已连接'));
  return page.evaluate(async () => (await fetch('/v1/me')).json());
}

async function newSession(page) {
  await page.locator('#new-package').fill('demo');
  await page.locator('#new-session button').click();
  await page.waitForFunction(() => document.querySelectorAll('#session-list li').length > 0);
  return page.locator('#session-list li').last().getAttribute('data-id');
}

async function main() {
  const browser = await chromium.launch({headless: true, ...(process.argv[4] ? {executablePath: process.argv[4]} : {})});
  const failures = [];
  try {
    const aliceContext = await browser.newContext();
    const bobContext = await browser.newContext();
    const alice = await aliceContext.newPage();
    const bob = await bobContext.newPage();
    for (const page of [alice, bob]) {
      page.on('pageerror', error => failures.push(error.message));
      page.on('request', request => {
        const url = new URL(request.url());
        if (url.pathname === '/v1/auth/feishu/callback' && url.searchParams.has('code')) consumedCodes.push(url.searchParams.get('code'));
      });
      await page.route('https://example.com/avatar.png', route => route.fulfill({contentType:'image/svg+xml', body:'<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30"><circle cx="15" cy="15" r="15" fill="blue"/></svg>'}));
    }
    // 先保留一把旧 key，再切飞书，确保回调后属于员工而不是 key。
    await alice.goto(base + '/console/');
    await alice.locator('#api-key').fill(key);
    await alice.locator('#connect button[type=submit]').click();
    await alice.waitForFunction(() => document.getElementById('conn-status').textContent.startsWith('已连接'));
    const aliceMe = await login(alice, 'alice', true);
    assert.equal(aliceMe.key_id, null);
    assert.equal(await alice.evaluate(() => sessionStorage.getItem('xuanpu.apiKey')), null);
    const aliceSession = await newSession(alice);
    await adminApi('PUT', `/v1/users/${aliceMe.user_id}/role`, {role:'tenant_admin'});
    await alice.reload();
    await alice.locator('[data-tab=users]').waitFor({state:'visible'});
    const memberRequests = [];
    bob.on('request', req => { if (req.url().startsWith(base + '/v1/')) memberRequests.push({url:req.url(), headers:req.headers(), method:req.method()}); });
    const bobMe = await login(bob, 'bob');
    assert.equal(bobMe.role, 'tenant_member');
    assert.equal(await bob.locator('[data-tab=users]').isVisible(), false);
    assert.equal(await bob.locator('#user-avatar').getAttribute('src'), null);
    assert.equal(await bob.evaluate(() => window.pwned), undefined);
    assert((await bob.locator('#user-name').textContent()).includes('<img src=x'));
    const bobSession = await newSession(bob);
    assert.notEqual(aliceSession, bobSession);
    assert.equal(await bob.locator('#session-list li').count(), 1);
    assert.equal(await bob.locator('#session-list li').getAttribute('data-id'), bobSession);
    await bob.waitForFunction(() => document.getElementById('transcript').textContent.includes('成员暂不支持加载历史对话'));
    assert.equal(await bob.evaluate(async id => (await fetch('/v1/sessions/' + id)).status, aliceSession), 404);
    assert(!memberRequests.some(req => /\/v1\/(audit|usage|packages)(\?|$)/.test(req.url)));
    assert(!memberRequests.some(req => req.headers.authorization));
    assert(memberRequests.filter(req => req.method === 'POST').every(req => req.headers['x-requested-by'] === 'xuanpu'));
    await bob.setViewportSize({width:375, height:812});
    assert(await bob.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '成员页面发生横向溢出');
    await bob.screenshot({path:path.join(out, 'member-mobile.png'), fullPage:true});
    await bob.locator('#logout').click();
    await bob.waitForFunction(() => document.getElementById('conn-status').textContent === '已退出');
    assert.equal(await bob.locator('#session-list li').count(), 0);
    assert.equal(await bob.evaluate(async () => (await fetch('/v1/me')).status), 401);
    await login(bob, 'bob');
    await checkAdmin(alice, bob, aliceMe, bobMe);
    await checkKey(browser, bobMe);
    const response = await fetch(base + '/console/');
    assert.equal(response.headers.get('x-frame-options'), 'DENY');
    assert(consumedCodes.length >= 5);
    const replay = await fetch(mock + '/open-apis/authen/v2/oauth/token', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code:consumedCodes[0]})});
    assert.equal(replay.status, 400);
    const stats = await (await fetch(mock + '/healthz')).json();
    assert.equal(stats.replay_rejected, 1);
    assert(stats.pkce_verified >= 5);
    assert.equal(stats.pkce_verified, stats.exchanges);
    assert.deepEqual(failures, []);
    fs.writeFileSync(path.join(out, 'result.json'), JSON.stringify({passed:true, checks:['真实CLI','真实PKCE登录','本人会话隔离','退出清cookie','管理员停用启用','409最后管理员','API key','姓名XSS/头像','过滤分页/收尾失败','375px无溢出','DENY响应头'], stats, aliceSession, bobSession}, null, 2));
    console.log('console browser acceptance passed');
  } finally { await browser.close(); }
}

async function checkAdmin(alice, bob, aliceMe, bobMe) {
    // 唯一管理员的降级和停用均被真正的后端 409 拒绝。
    await alice.locator('[data-tab=users]').click();
    const aliceRow = alice.locator(`tr[data-user-id="${aliceMe.user_id}"]`);
    await aliceRow.getByRole('button', {name:'改为成员', exact:true}).click();
    await alice.waitForFunction(() => document.getElementById('users-notice').textContent.startsWith('无法修改'));
    assert((await alice.locator('#users-notice').textContent()).includes('管理员'));
    await aliceRow.getByRole('button', {name:'停用', exact:true}).click();
    await alice.waitForFunction(() => document.getElementById('users-notice').textContent.startsWith('无法修改'));
    const bobRow = alice.locator(`tr[data-user-id="${bobMe.user_id}"]`);
    await bobRow.getByRole('button', {name:'停用', exact:true}).click();
    await alice.waitForFunction(() => document.getElementById('users-notice').textContent === '员工已停用。');
    assert.equal((await adminApi('GET', `/v1/users/${bobMe.user_id}`)).status, 'disabled');
    assert.equal(await bob.evaluate(async () => (await fetch('/v1/me')).status), 401);
    assert.equal(await alice.evaluate(() => window.pwned), undefined);
    await alice.setViewportSize({width:375, height:812});
    assert(await alice.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '用户页面发生横向溢出');
    await alice.screenshot({path:path.join(out, 'admin-users-mobile.png'), fullPage:true});
    // 确认后端禁用原因映射，账号启用后可再登录。
    await loginDisabled(bob);
    await alice.locator('#users-status').selectOption('disabled');
    await alice.locator('#users-filter button[type=submit]').click();
    await alice.waitForFunction(() => document.querySelectorAll('#users-table tbody tr').length === 1);
    await alice.locator(`tr[data-user-id="${bobMe.user_id}"]`).getByRole('button', {name:'启用', exact:true}).click();
    await alice.waitForFunction(() => document.getElementById('users-notice').textContent.startsWith('已启用'));
    await login(bob, 'bob');
}

async function checkKey(browser, bobMe) {
    // API key 保留可用：另一个浏览器上下文中连接，跨源地址不显示飞书入口。
    const keyPage = await (await browser.newContext()).newPage();
    await keyPage.goto(base + '/console/');
    await keyPage.locator('#base-url').fill('https://other.example');
    assert.equal(await keyPage.locator('#feishu-login').isVisible(), false);
    await keyPage.locator('#base-url').fill('');
    await keyPage.locator('#api-key').fill(key);
    await keyPage.locator('#connect button[type=submit]').click();
    await keyPage.locator('[data-tab=users]').waitFor({state:'visible'});
    await keyPage.locator('[data-tab=users]').click();
    await keyPage.waitForFunction(() => document.querySelectorAll('#users-table tbody tr').length === 2);
    // 难以在真实服务中稳定制造的收尾失败与分页，独立拦截单个响应测试展示。
    await keyPage.route('**/v1/users/*/status', route => route.fulfill({json:{status:'disabled', cleanup_failed:['sessions']}}));
    await keyPage.locator(`tr[data-user-id="${bobMe.user_id}"]`).getByRole('button', {name:'停用', exact:true}).click();
    await keyPage.waitForFunction(() => document.getElementById('users-notice').textContent.includes('收尾失败'));
    let querySeen = '';
    await keyPage.route('**/v1/users?**', route => {
      querySeen = route.request().url();
      const next = new URL(querySeen).searchParams.get('after');
      return route.fulfill({json:{users:next ? [] : [{user_id:'page-user', display_name:'分页员工', role:'tenant_member', status:'active', last_login_at:100, unverified_since:100}], next_after:next ? null : 'page-user'}});
    });
    await keyPage.locator('#users-unverified').selectOption('true');
    await keyPage.locator('#users-filter button[type=submit]').click();
    await keyPage.locator('tr[data-user-id=page-user]').waitFor();
    assert(querySeen.includes('unverified=true'));
    assert((await keyPage.locator('#users-table').textContent()).includes('飞书查不到'));
    await keyPage.locator('#users-more').click();
    await keyPage.waitForFunction(() => document.getElementById('users-more').disabled);
    assert(querySeen.includes('after=page-user'));
}

async function loginDisabled(page) {
  await page.goto(base + '/console/');
  await page.locator('#feishu-login').click();
  await page.waitForURL(mock + '/open-apis/authen/v1/authorize?**');
  await page.locator('#bob').click();
  await page.waitForURL(base + '/console/?login_error=user_disabled');
  assert((await page.locator('#login-error').textContent()).includes('已停用'));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
