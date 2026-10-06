// 通知中心必须浮于当前工作区之上，开关不改布局、输入和已读状态。
const assert=require('node:assert/strict');
const path=require('node:path');
module.exports=async function notificationCenterJourney(page,out) {
  const toggle=page.locator('#notification-toggle'),panel=page.locator('#notifications-panel');
  const position=()=>page.evaluate(()=>({main:document.querySelector('main').getBoundingClientRect().y+scrollY,scroll:scrollY}));
  await page.locator('#message').fill('未发送内容应完整保留');
  const before=await position();await toggle.click();await panel.waitFor();
  assert.deepEqual(await position(),before,'打开通知不能下推工作区或滚动页面');
  assert.equal(await panel.getAttribute('role'),'dialog');
  assert.equal(await toggle.getAttribute('aria-expanded'),'true');
  await toggle.click();assert.equal(await panel.isVisible(),false);
  await toggle.click();await page.keyboard.press('Escape');assert.equal(await panel.isVisible(),false);
  assert.equal(await toggle.evaluate(node=>node===document.activeElement),true);
  await toggle.click();await page.locator('#message').click();assert.equal(await panel.isVisible(),false);
  assert.equal(await page.locator('#message').evaluate(node=>node===document.activeElement),true);
  assert.equal(await page.locator('#message').inputValue(),'未发送内容应完整保留');
  await toggle.click();await page.keyboard.press('Tab');await page.keyboard.press('Tab');
  assert.equal(await panel.isVisible(),false,'键盘焦点离开空通知面板应关闭浮层');
  await page.locator('#workspace-nav [data-workspace=file]').click();await page.locator('#workspace-window').waitFor();
  await toggle.click();await page.keyboard.press('Escape');
  assert.equal(await page.locator('#workspace-window').isVisible(),true,'Esc 不能同时关闭后台文件窗口');
  await page.locator('#workspace-close').click();
  await page.locator('#functions-tab').click();await page.setViewportSize({width:375,height:812});
  await page.locator('#open-notification-module').scrollIntoViewIfNeeded();
  const modulePosition=await position();await page.locator('#open-notification-module').click();await panel.waitFor();
  assert.deepEqual(await position(),modulePosition,'从已滚动的功能页打开不能跳到顶部');
  await page.keyboard.press('Escape');assert.equal(await page.locator('#open-notification-module').evaluate(node=>node===document.activeElement),true);
  assert.deepEqual(await position(),modulePosition,'焦点返回功能入口不能改变滚动位置');
  await page.setViewportSize({width:1280,height:720});await page.locator('[data-tab=sessions]').click();
  // 长列表只在面板内部滚动，使用已读记录避免触发新通知提示卡。
  await page.route('**/v1/notifications?limit=100&latest=true',route=>route.fulfill({json:{items:Array.from({length:20},(_,i)=>({notification_id:900+i,title:'通知列表布局验收 '+i,due_at:2000000000,read_at:2000000001,deliveries:[]}))}}));
  for(const width of [1280,375]) {
    await page.setViewportSize({width,height:812});await page.emulateMedia({colorScheme:width===375?'dark':'light'});
    await toggle.click();await panel.getByText('通知列表布局验收 19',{exact:true}).waitFor();
    const box=await panel.boundingBox(),anchor=await toggle.boundingBox();
    assert(box.x>=0 && box.x+box.width<=width && box.y>=0 && box.y+box.height<=812,'浮层必须完整位于视口内');
    assert(Math.abs(box.y-anchor.y-anchor.height-8)<2,'通知浮层应关联入口按钮');
    assert(await page.locator('#notification-list').evaluate(node=>node.scrollHeight>node.clientHeight),'长列表应内部滚动');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'窄屏不应横向溢出');
    await page.screenshot({path:path.join(out,`notification-center-${width}.png`),fullPage:true});
    await page.keyboard.press('Escape');
  }
  await page.unroute('**/v1/notifications?limit=100&latest=true');
  await page.setViewportSize({width:1280,height:720});await page.emulateMedia({colorScheme:'light'});
  await toggle.click();await page.locator('#notification-list').getByText('暂无通知。离线期间的提醒会保存在这里。',{exact:true}).waitFor();
  await page.locator('#close-notifications').click();
  await page.locator('#message').fill('');
};
