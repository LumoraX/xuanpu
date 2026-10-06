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
  await page.waitForFunction(() => (/^(已登录|后台凭据已连接)/).test(document.getElementById('conn-status').textContent));
  return page.evaluate(async () => (await fetch('/v1/me')).json());
}

async function newSession(page) {
  await page.locator('#new-session button').click();
  await page.locator('#message').fill('你好，请帮我处理业务');
  await page.locator('#send-btn').click();
  await page.getByText('这是可恢复的测试回复。', {exact:true}).waitFor();
  await page.waitForFunction(() => !document.getElementById('send-btn').disabled);
  return page.locator('#session-list li.active').getAttribute('data-id');
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
    await alice.goto(base + '/console/?admin=1');
    await alice.locator('#admin-connection summary').click();
    await alice.locator('#api-key').fill(key);
    await alice.locator('#connect button[type=submit]').click();
    await alice.waitForFunction(() => (/^(已登录|后台凭据已连接)/).test(document.getElementById('conn-status').textContent));
    const aliceMe = await login(alice, 'alice', true);
    assert.equal(aliceMe.key_id, null);
    assert.equal(await alice.evaluate(() => sessionStorage.getItem('xuanpu.apiKey')), null);
    const aliceSession = await newSession(alice);
    await adminApi('PUT', `/v1/users/${aliceMe.user_id}/role`, {role:'tenant_admin'});
    await alice.reload();
    await alice.locator('[data-tab=users]').waitFor({state:'visible'});
    const memberRequests = [];
    bob.on('request', req => { if (req.url().startsWith(base + '/v1/')) memberRequests.push({url:req.url(), headers:req.headers(), method:req.method()}); });
    await bob.goto(base + '/console/');
    assert.equal(await bob.locator('#admin-connection').isVisible(), false);
    await bob.screenshot({path:path.join(out, 'welcome.png'), fullPage:true});
    await bob.evaluate(() => {
      localStorage.setItem('xuanpu.baseUrl', 'https://old.invalid');
      sessionStorage.setItem('xuanpu.apiKey', 'stale-admin-key');
    });
    await bob.reload();
    assert.equal(await bob.locator('#feishu-login').isVisible(), true);
    assert.equal(await bob.locator('#base-url').isVisible(), false);
    const bobMe = await login(bob, 'bob');
    assert.equal(bobMe.role, 'tenant_member');
    assert.equal(await bob.locator('[data-tab=users]').isVisible(), false);
    assert.equal(await bob.locator('#base-url').isVisible(), false);
    assert.equal(await bob.locator('#api-key').isVisible(), false);
    assert.equal(await bob.locator('#new-package').isVisible(), false);
    await bob.waitForFunction(() => !document.getElementById('message').disabled);
    assert.equal(await bob.locator('#user-avatar').getAttribute('src'), null);
    assert.equal(await bob.evaluate(() => window.pwned), undefined);
    assert((await bob.locator('#user-name').textContent()).includes('<img src=x'));
    await require('./composer.cjs').welcome(bob,out);
    await require('./notification-center.cjs')(bob,out);
    const bobSession = await newSession(bob);
    assert.notEqual(aliceSession, bobSession);
    assert.equal(await bob.locator('#session-list li').count(), 1);
    assert.equal(await bob.locator('#session-list li').getAttribute('data-id'), bobSession);
    await bob.waitForFunction(() => !document.getElementById('message').disabled);
    await bob.locator('#message').fill('历史恢复验收');
    await bob.locator('#send-btn').click();
    await bob.getByText('这是可恢复的测试回复。', {exact:true}).first().waitFor();
    await bob.waitForFunction(() => !document.getElementById('send-btn').disabled);
    await bob.reload();
    await bob.getByText('这是可恢复的测试回复。', {exact:true}).first().waitFor();
    assert.equal(await bob.locator('#session-list li.active').getAttribute('data-id'), bobSession);
    assert.equal(await bob.locator('#welcome').isVisible(), false);
    assert.equal(await bob.evaluate(async id => (await fetch('/v1/sessions/' + id)).status, aliceSession), 404);
    await require('./workspace.cjs')(bob,alice,out);
    await bob.locator('.session-history summary').click();
    await bob.locator('#session-list li').first().click();
    assert(!memberRequests.some(req => /\/v1\/(audit|usage)(\?|$)/.test(req.url)));
    assert(!memberRequests.some(req => req.headers.authorization));
    assert(memberRequests.filter(req => req.method === 'POST').every(req => req.headers['x-requested-by'] === 'xuanpu'));
    // 提醒必须先确认，确认前无后台任务；后台到点与站内通知独立于对话。
    assert.equal(await bob.locator('aside #reminders-panel').count(),0);
    assert.equal(await bob.locator('#new-reminder').isVisible(),false);
    assert.equal(await bob.locator('#reminders-panel').isVisible(),false);
    await bob.locator('#message').fill('5秒后提醒我：浏览器提醒验收');
    await bob.locator('#send-btn').click();
    await bob.locator('#confirm-reminder').waitFor();
    assert.equal(await bob.evaluate(async()=> (await (await fetch('/v1/reminders')).json()).items.length),0);
    await bob.locator('#confirm-reminder').click();
    await bob.locator('#reminder-preview').waitFor({state:'hidden'});
    assert.equal(await bob.locator('#reminders-panel').isVisible(),false);
    await bob.locator('#message').fill('正在输入，提醒不要打断');
    await bob.locator('#message').focus();
    const popup=bob.locator('.notification-alert').filter({hasText:'浏览器提醒验收'});
    await popup.waitFor({timeout:15000});
    assert.equal(await bob.evaluate(()=>document.activeElement.id),'message','弹卡不能抢走输入焦点');
    assert.equal(await bob.locator('#message').inputValue(),'正在输入，提醒不要打断');
    await bob.emulateMedia({colorScheme:'dark'});
    await bob.screenshot({path:path.join(out,'notification-popup-dark.png'),fullPage:true});
    await bob.setViewportSize({width:375,height:812});
    assert(await bob.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'悬浮通知窄屏溢出');
    const popupBox=await popup.boundingBox();assert(popupBox.x>=0 && popupBox.x+popupBox.width<=375);
    await bob.screenshot({path:path.join(out,'notification-popup-mobile.png'),fullPage:true});
    await popup.getByRole('button',{name:'关闭通知提示'}).click();
    assert.equal(await bob.locator('.notification-alert').count(),0);
    assert.equal(await bob.evaluate(async()=> (await (await fetch('/v1/notifications')).json()).items[0].read_at),null);
    await bob.setViewportSize({width:1280,height:900});await bob.emulateMedia({colorScheme:'light'});
    await bob.locator('#message').fill('');
    await bob.locator('#notification-toggle').getByText('通知 · 有未读',{exact:true}).waitFor({timeout:15000});
    assert.equal(await bob.locator('#notifications-panel').isVisible(),false);
    await bob.locator('#notification-toggle').click();
    await bob.locator('#notification-list').getByText('浏览器提醒验收',{exact:true}).waitFor({timeout:15000});
    await bob.reload();
    assert.equal(await bob.locator('#notifications-panel').isVisible(),false);
    const summary=bob.locator('.notification-alert').filter({hasText:'你有未读通知'});
    await summary.waitFor();
    assert.equal(await bob.locator('.notification-alert').count(),1,'刷新只汇总，不重播历史');
    await summary.getByRole('button',{name:'查看通知'}).click();
    await bob.locator('#notification-list').getByText('浏览器提醒验收',{exact:true}).waitFor();
    await bob.locator('#notification-list').getByRole('button',{name:'标为已读'}).click();
    await bob.locator('#notification-list').getByText('已读',{exact:true}).waitFor();
    assert.equal(await alice.evaluate(async()=> (await (await fetch('/v1/notifications')).json()).items.length),0);
    await bob.locator('#close-notifications').click();
    await checkNotificationPopupRead(bob);
    await bob.locator('#message').fill('一小时后提醒我提交报告');
    await bob.locator('#send-btn').click();
    await bob.locator('#confirm-reminder').click();
    await bob.locator('#reminder-preview').waitFor({state:'hidden'});
    assert.equal(await bob.locator('#reminders-panel').isVisible(),false);
    await bob.locator('#message').fill('查看我的提醒');
    await bob.locator('#send-btn').click();
    await bob.locator('.chat-pane #reminders-panel').waitFor();
    const scheduled = bob.locator('#reminder-list li').filter({hasText:'可取消提醒'});
    await scheduled.getByRole('button',{name:'修改',exact:true}).click();
    await bob.getByRole('textbox',{name:'提醒内容',exact:true}).fill('修改后的提醒');
    await bob.getByRole('button',{name:'保存提醒',exact:true}).click();
    const edited = bob.locator('#reminder-list li').filter({hasText:'修改后的提醒'});
    await edited.getByRole('button',{name:'取消提醒',exact:true}).click();
    await edited.waitFor({state:'detached'});
    await bob.locator('#reminder-status').selectOption('cancelled');
    await edited.getByText('已取消',{exact:true}).waitFor();
    await bob.locator('#close-reminders').click();
    await bob.locator('#message').fill('查看我的提醒');
    await bob.locator('#send-btn').click();
    await bob.locator('#reminders-panel').waitFor();
    await bob.locator('#message').fill('继续处理业务');
    await bob.locator('#send-btn').click();
    await bob.locator('#reminders-panel').waitFor({state:'hidden'});
    await bob.waitForFunction(() => !document.getElementById('send-btn').disabled);
    await bob.locator('#message').fill('查看我的提醒');
    await bob.locator('#send-btn').click();
    await bob.locator('#reminders-panel').waitFor();
    await bob.locator('#new-session-btn').click();
    assert.equal(await bob.locator('#reminders-panel').isVisible(),false);
    await checkFunctionFallback(bob);
    await checkLateRouteDuringManualEdit(bob);
    await bob.setViewportSize({width:375, height:812});
    assert(await bob.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '成员页面发生横向溢出');
    await bob.screenshot({path:path.join(out, 'member-mobile.png'), fullPage:true});
    await bob.locator('#logout').click();
    await bob.waitForFunction(() => document.getElementById('conn-status').textContent === '已退出');
    assert.equal(await bob.locator('#session-list li').count(), 0);
    assert.equal(await bob.locator('#notification-list li').count(), 0);
    assert.equal(await bob.locator('#functions-tab').isVisible(), false);
    assert.equal(await bob.locator('#reminder-preview').isVisible(), false);
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
    fs.writeFileSync(path.join(out, 'result.json'), JSON.stringify({passed:true, checks:['真实CLI','真实PKCE登录','本人会话隔离','退出清cookie','管理员停用启用','409最后管理员','API key','姓名XSS/头像','过滤分页/收尾失败','375px无溢出','DENY响应头','无模型手动提醒CRUD','对话与功能同数据','切页隔离迟到识别'], stats, aliceSession, bobSession}, null, 2));
    console.log('console browser acceptance passed');
  } finally { await browser.close(); }
}

// 关闭对话识别通路仍能完整管理提醒，刷新和切换入口共用记录。
async function checkNotificationPopupRead(page) {
  const response=await page.evaluate(async()=>{
    const result=await fetch('/v1/reminders',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-By':'xuanpu'},body:JSON.stringify({title:'悬浮通知标读验收',due_at:Date.now()/1000+3,timezone:'Asia/Shanghai',repeat:'once',channels:['inbox'],request_id:crypto.randomUUID()})});
    return result.status;
  });
  assert.equal(response,200);
  const popup=page.locator('.notification-alert').filter({hasText:'悬浮通知标读验收'});
  await popup.waitFor({timeout:15000});
  await popup.getByRole('button',{name:'标为已读',exact:true}).click();
  await popup.waitFor({state:'detached'});
  assert(await page.evaluate(async()=> (await (await fetch('/v1/notifications')).json()).items.find(item=>item.title==='悬浮通知标读验收').read_at));
}

async function checkFunctionFallback(page) {
  let modelCalls=0;
  await page.route('**/v1/assistant/route',async route=>{modelCalls++;await route.fulfill({status:503,body:'{}'});});
  await page.locator('#functions-tab').click();
  assert.equal(await page.locator('#reminders-panel').isVisible(),false);
  await page.locator('#open-reminder-module').click();
  await page.locator('#new-reminder').click();
  await page.getByRole('textbox',{name:'提醒内容',exact:true}).fill('手动兜底提醒');
  await page.getByLabel('重复方式',{exact:true}).selectOption('daily');
  await page.getByRole('button',{name:'保存提醒',exact:true}).click();
  await page.locator('#reminder-list li').filter({hasText:'手动兜底提醒'}).waitFor();
  await page.reload();
  await page.locator('#functions-tab').waitFor();
  assert.equal(await page.locator('#tab-sessions').isVisible(),true);
  assert.equal(await page.locator('#reminders-panel').isVisible(),false);
  await page.locator('#functions-tab').click();
  await page.locator('#open-reminder-module').click();
  await page.locator('#reminder-list li').filter({hasText:'手动兜底提醒'}).getByRole('button',{name:'修改',exact:true}).click();
  await page.getByRole('textbox',{name:'提醒内容',exact:true}).fill('手动修改后');
  await page.getByLabel('重复方式',{exact:true}).selectOption('weekly');
  await page.getByRole('button',{name:'保存提醒',exact:true}).click();
  await page.locator('#reminder-list li').filter({hasText:'手动修改后'}).getByText(/每周/).waitFor();
  assert.equal(modelCalls,0,'手动新建和修改不应调用模型');
  await page.unroute('**/v1/assistant/route');
  await page.locator('[data-tab=sessions]').click();
  await page.locator('#message').fill('查看我的提醒');
  await page.locator('#send-btn').click();
  await page.locator('.chat-pane #reminder-list li').filter({hasText:'手动修改后'}).waitFor();
  assert.equal(await page.locator('#new-reminder').isVisible(),false);
  await page.route('**/v1/assistant/route',async route=>{modelCalls++;await route.fulfill({status:503,body:'{}'});});
  await page.locator('#functions-tab').click();
  await page.locator('#open-reminder-module').click();
  const row=page.locator('#reminder-list li').filter({hasText:'手动修改后'});
  await row.getByRole('button',{name:'取消提醒',exact:true}).click();
  await row.waitFor({state:'detached'});
  await page.locator('#reminder-status').selectOption('cancelled');
  await row.getByText('已取消',{exact:true}).waitFor();
  await page.locator('#new-reminder').click();
  await page.setViewportSize({width:375,height:812});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'功能页面发生横向溢出');
  await page.screenshot({path:path.join(out,'functions-mobile.png'),fullPage:true});
  await page.getByRole('button',{name:'返回',exact:true}).click();
  await page.locator('#open-notification-module').click();
  await page.locator('#notification-list').getByText('浏览器提醒验收',{exact:true}).waitFor();
  await page.locator('#close-notifications').click();
  assert.equal(modelCalls,0,'手动取消与通知查看不应调用模型');
  await page.unroute('**/v1/assistant/route');
  await page.locator('[data-tab=sessions]').click();
}

async function checkLateRouteDuringManualEdit(page) {
  let release;
  await page.route('**/v1/assistant/route',async route=>{
    await new Promise(resolve=>{release=resolve;});
    await route.fulfill({json:{message:'旧草稿',reminder_draft:{title:'迟到的草稿',due_at:2000000000,timezone:'UTC',repeat:'once'}}});
  });
  await page.locator('#message').fill('等待中的提醒需求');
  const requested=page.waitForRequest('**/v1/assistant/route');
  await page.locator('#send-btn').click();await requested;
  await page.locator('#functions-tab').click();
  await page.locator('#open-reminder-module').click();
  await page.locator('#new-reminder').click();
  await page.getByRole('textbox',{name:'提醒内容',exact:true}).fill('正在手动编辑');
  release();
  await page.waitForFunction(()=>!document.getElementById('send-btn').disabled);
  assert.equal(await page.getByRole('textbox',{name:'提醒内容',exact:true}).inputValue(),'正在手动编辑');
  assert.equal(await page.locator('#confirm-reminder').count(),0);
  await page.unroute('**/v1/assistant/route');
  await page.locator('[data-tab=sessions]').click();
  assert.equal(await page.locator('#message').inputValue(),'等待中的提醒需求');
  await page.locator('#message').fill('');
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
    await keyPage.goto(base + '/console/?admin=1');
    await keyPage.locator('#admin-connection summary').click();
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
