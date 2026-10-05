// 输入框附件菜单的真实交互回归：关闭方式、键盘、文件选择和窄屏定位。
const assert=require('node:assert/strict');
const path=require('node:path');
module.exports=async function composerJourney(page,out) {
  const add=page.locator('#attach-file'),upload=page.getByText('上传本机文件',{exact:true});
  await page.locator('#message').fill('输入内容应该保留');
  await add.click();await upload.waitFor();await add.click();
  assert.equal(await upload.isVisible(),false,'再次点击加号应关闭附件菜单');
  assert.equal(await page.locator('#message').inputValue(),'输入内容应该保留');
  await add.click();await page.locator('#message').click();assert.equal(await upload.isVisible(),false);
  await add.click();await page.keyboard.press('Escape');assert.equal(await upload.isVisible(),false);
  assert.equal(await add.getAttribute('aria-expanded'),'false');
  assert.equal(await add.evaluate(node=>node===document.activeElement),true);
  await add.press('ArrowDown');await upload.waitFor();await page.keyboard.press('ArrowDown');
  assert.equal(await page.locator('#attachment-menu [role=menuitem]').nth(1).evaluate(node=>node===document.activeElement),true);
  await page.keyboard.press('Tab');assert.equal(await upload.isVisible(),false);
  assert.equal(await page.locator('#send-btn').evaluate(node=>node===document.activeElement),true,'Tab 应继续到发送按钮');
  await add.press('ArrowUp');await page.keyboard.press('Shift+Tab');
  assert.equal(await page.locator('#message').evaluate(node=>node===document.activeElement),true,'Shift+Tab 应返回输入区域');
  await add.click();
  const chooser=page.waitForEvent('filechooser');await upload.click();await (await chooser).setFiles([]);
  assert.equal(await upload.isVisible(),false);
  await add.click();await page.getByText('从我的文件选择',{exact:true}).click();
  await page.locator('#workspace-title').filter({hasText:'选择附件'}).waitFor();
  assert.equal(await upload.isVisible(),false);await page.locator('#workspace-close').click();
  await page.locator('#message').fill('');
  for(const width of [1280,375]) {
    await page.setViewportSize({width,height:812});await add.click();await upload.waitFor();
    const field=await page.locator('#message').boundingBox(),button=await add.boundingBox();
    const menu=await page.locator('#attachment-menu').boundingBox(),form=await page.locator('#send').boundingBox();
    assert(button.y>=field.y+field.height,'加号应位于输入区域下方');
    assert(menu.y>=0 && menu.x>=0 && menu.x+menu.width<=width,'附件菜单不能超出屏幕');
    assert(menu.y+menu.height<=button.y,'附件菜单应在加号上方');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'输入区域不能横向溢出');
    await page.screenshot({path:path.join(out,`composer-menu-${width}.png`),clip:{x:form.x,y:menu.y-4,width:form.width,height:form.y+form.height-menu.y+4}});
    await page.keyboard.press('Escape');
  }
  await page.setViewportSize({width:1280,height:720});
};

// 附件真实上传后检查窄屏和深色主题，并验证移除附件不会误发消息。
module.exports.withAttachments=async function composerAttachments(page,out) {
  const names=['员工手册与项目资料'.repeat(8)+'.txt','会议纪要.txt'];
  await page.locator('#attachment-input').setInputFiles(names.map(name=>({name,mimeType:'text/plain',buffer:Buffer.from('附件布局验收')})));
  await page.waitForFunction(()=>document.querySelectorAll('#attachment-chips .attachment-chip').length===2);
  await page.locator('#message').fill('这些附件暂时不发送');
  await page.setViewportSize({width:375,height:812});await page.emulateMedia({colorScheme:'dark'});
  await page.locator('#attach-file').click();await page.locator('#attachment-menu').waitFor();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'长文件名附件不能导致窄屏溢出');
  const form=await page.locator('#send').boundingBox();
  await page.screenshot({path:path.join(out,'composer-attachments-dark-375.png'),clip:form});
  await page.keyboard.press('Escape');
  for(const name of names)await page.getByRole('button',{name:'移除附件 '+name,exact:true}).click();
  assert.equal(await page.locator('#attachment-chips .attachment-chip').count(),0);
  assert.equal(await page.locator('#message').inputValue(),'这些附件暂时不发送');
  await page.locator('#message').fill('');await page.emulateMedia({colorScheme:'light'});
  await page.setViewportSize({width:1280,height:720});
};
