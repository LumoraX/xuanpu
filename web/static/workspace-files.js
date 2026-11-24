// 文件夹、版本、回收站与附件共享服务端个人空间，不在浏览器保存文件正文。
"use strict";
function fileSize(bytes) { return bytes>=1048576?(bytes/1048576).toFixed(1)+" MB":bytes>=1024?(bytes/1024).toFixed(1)+" KB":bytes+" B"; }
function renderFileToolbar(box) {
  const upload=document.createElement("input");upload.type="file";upload.multiple=true;upload.className="hidden";upload.setAttribute("aria-label","上传文件");
  upload.addEventListener("change",()=>uploadWorkspaceFiles(upload.files,{parent:workspace.parent}).catch(fail));
  box.append(upload,wsButton("上传文件",()=>upload.click(),""),wsButton("新建文件夹",()=>editWorkspaceRecord("folder")),wsButton("关联在线文档",()=>editWorkspaceRecord("file")));
  const search=document.createElement("input");search.type="search";search.placeholder="搜索名称、标签";search.setAttribute("aria-label","搜索文件");search.value=workspace.search;
  search.addEventListener("change",()=>{workspace.search=search.value.trim();return refreshWorkspace();});
  box.append(search,wsButton(workspace.trashed?"返回文件":"回收站",()=>{workspace.trashed=!workspace.trashed;workspace.parent=null;return refreshWorkspace();}),wsButton("刷新",refreshWorkspace));
  const crumbs=wsText("div","","workspace-breadcrumb");
  crumbs.append(wsButton(workspace.trashed?"回收站":"个人空间",()=>{workspace.parent=null;workspace.search="";return refreshWorkspace();}));
  const chain=[],seen=new Set();let id=workspace.parent;
  while(id && !seen.has(id)){seen.add(id);const folder=workspace.items.find(x=>x.id===id);if(!folder)break;chain.unshift(folder);id=folder.data.parent_id;}
  for(const folder of chain)crumbs.append(wsText("span","/"),wsButton(folder.data.name,()=>{workspace.parent=folder.id;return refreshWorkspace();}));
  box.append(crumbs);
  if(workspace.summary)box.append(wsText("p",`空间 ${fileSize(workspace.summary.used_bytes)} / ${fileSize(workspace.summary.quota_bytes)} · 单文件上限 20 MB（历史版本和回收站计入）`,"status"));
}
function renderWorkspaceFiles(items) {
  const box=$("workspace-content");
  const files=items.filter(x=>["file","folder"].includes(x.kind) && (workspace.search || workspace.trashed || (x.data.parent_id || null)===workspace.parent));
  if(!files.length)box.append(wsText("p",workspace.trashed?"回收站是空的。":"这个文件夹还没有内容。可以上传文件，也可以直接拖入。","workspace-empty"));
  for(const item of files.sort((a,b)=>a.kind===b.kind?a.data.name.localeCompare(b.data.name):a.kind==="folder"?-1:1))box.append(workspaceFileCard(item));
}
function workspaceFileCard(item) {
  const data=item.data,card=wsText("article","","workspace-record");card.dataset.fileId=item.id;
  card.append(wsText("h3",(item.kind==="folder"?"▱ ":"▤ ")+data.name));
  card.append(wsText("p",[item.kind==="folder"?"文件夹":data.source_url?"外部文档链接":fileSize(data.size)+" · 版本 "+data.current_revision,workspaceTime(item.updated_at)].join(" · "),"meta"));
  if(data.tags?.length)card.append(wsText("p",data.tags.join(" · "),"meta"));
  const actions=wsText("div","","row");
  if(item.trashed_at)actions.append(wsButton("恢复",()=>workspaceMutation(()=>api("POST",`${workspaceBase}/items/${item.id}/restore?version=${item.version}`))));
  else {
    if(item.kind==="folder")actions.append(wsButton("打开",()=>{workspace.parent=item.id;workspace.search="";return refreshWorkspace();}));
    else if(data.source_url) {
      try {
        const parsed=new URL(data.source_url);
        if(parsed.protocol!=="https:" || parsed.username || parsed.password)throw new TypeError("无效链接");
        const link=wsText("a","打开原文");link.href=parsed.href;link.target="_blank";link.rel="noopener noreferrer";actions.append(link);
      } catch(error) {
        if(!(error instanceof TypeError))throw error;
        card.append(wsText("p","文档链接无效，请移入回收站后重新关联有效链接。","status"));
      }
    } else actions.append(wsButton("下载",()=>downloadWorkspaceFile(item)),wsButton("版本",()=>showFileVersions(item)));
    if(item.kind==="file")actions.append(wsButton(workspace.picking?"选择附件":"加入对话",()=>{stageWorkspaceFile(item);if(workspace.picking)closeWorkspace();}));
    actions.append(wsButton("属性 / 移动",()=>editWorkspaceRecord(item.kind,item)),wsButton("移入回收站",()=>trashWorkspaceItem(item)));
  }
  card.append(actions);return card;
}
async function uploadWorkspaceFiles(fileList,{parent=null,attach=false}={}) {
  const epoch=conn.epoch,files=Array.from(fileList || []);
  for(const file of files) {
    if(epoch!==conn.epoch)return;
    if(file.size>20*1024*1024)throw new Error(`“${file.name}”超过单文件 20 MB 限制。`);
    if(attach && workspaceChat.attachments.length>=8)throw new Error("一次最多添加 8 个附件。已上传的文件仍在个人空间中。");
    toast("正在上传："+file.name,"info");
    const params=new URLSearchParams({name:file.name,request_id:crypto.randomUUID()});if(parent)params.set("parent_id",parent);
    const item=await api("POST",workspaceBase+"/files?"+params,{body:file,headers:{"Content-Type":file.type || "application/octet-stream"}});
    if(epoch!==conn.epoch)return;
    if(attach)stageWorkspaceFile(item);
  }
  if(epoch!==conn.epoch)return;
  if(workspace.kind==="file")await refreshWorkspace();
  await refreshWorkspaceSummary();toast("文件已保存到个人空间。","info");
}
async function downloadWorkspaceFile(item,revision=null) {
  const epoch=conn.epoch,path=`${workspaceBase}/files/${item.id}/content`+(revision?`?revision=${revision}`:"");
  const response=await fetch(url(path),{...requestOptions(),headers:headers()});
  if(!response.ok)throw new Error((await response.json()).error?.message || "下载失败");
  const blob=await response.blob();if(epoch!==conn.epoch)return;
  const href=URL.createObjectURL(blob),link=document.createElement("a");link.href=href;link.download=item.data.name;link.click();setTimeout(()=>URL.revokeObjectURL(href),1000);
}
async function showFileVersions(item) {
  const generation=++workspace.generation,epoch=conn.epoch;
  const {items}=await api("GET",`${workspaceBase}/files/${item.id}/versions`);
  if(!workspaceCurrent(generation,epoch))return;
  $("workspace-toolbar").replaceChildren(wsButton("返回文件",refreshWorkspace));
  const box=$("workspace-content");box.replaceChildren(wsText("h3",item.data.name));
  const upload=document.createElement("input");upload.type="file";upload.setAttribute("aria-label","上传新版本");
  const requestId=crypto.randomUUID();
  upload.addEventListener("change",async()=>{
    const file=upload.files[0];if(!file)return;
    try{await workspaceMutation(()=>api("PUT",`${workspaceBase}/files/${item.id}/content?version=${item.version}&request_id=${requestId}`,{body:file,headers:{"Content-Type":file.type || "application/octet-stream"}}),"新版本已保存，旧版本仍可下载。");}
    catch(error){fail(error);}
  });box.append(wsText("p","上传新版本（保留原文件名称与历史版本）","status"),upload);
  for(const version of items) {
    const card=wsText("article","","workspace-record");card.append(wsText("h3","版本 "+version.revision),wsText("p",fileSize(version.size)+" · "+workspaceTime(version.created_at),"meta"),wsButton("下载此版本",()=>downloadWorkspaceFile(item,version.revision)));box.append(card);
  }
}
$("workspace-content").addEventListener("dragover",event=>{if(workspace.kind==="file"){event.preventDefault();$("workspace-content").classList.add("file-drop-active");}});
$("workspace-content").addEventListener("dragleave",()=>$("workspace-content").classList.remove("file-drop-active"));
$("workspace-content").addEventListener("drop",event=>{
  if(workspace.kind!=="file")return;event.preventDefault();$("workspace-content").classList.remove("file-drop-active");
  uploadWorkspaceFiles(event.dataTransfer.files,{parent:workspace.parent}).catch(fail);
});
