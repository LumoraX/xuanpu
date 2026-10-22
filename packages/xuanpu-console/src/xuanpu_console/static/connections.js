// 企业连接以本人原生同意为准；模型、切页与查看状态都不能自动授权。
"use strict";
const connectionState={generation:0};
function connectionLive(epoch,generation){return epoch===conn.epoch&&generation===connectionState.generation;}
function connectionText(tag,text){const node=document.createElement(tag);node.textContent=text;return node;}
function connectionButton(text,action){const node=connectionText("button",text);node.type="button";node.addEventListener("click",action);return node;}
function resetConnections(){connectionState.generation++;$("connections-list").replaceChildren();$("connections-feedback").textContent="";$("connections-panel").classList.add("hidden");}
async function openConnections(){
  const epoch=conn.epoch,generation=++connectionState.generation;
  $("connections-panel").classList.remove("hidden");$("connections-panel").scrollIntoView?.({block:"start"});$("connections-list").replaceChildren();$("connections-feedback").textContent="正在读取本人的连接状态…";
  try{
    const result=await api("GET","/v1/me/connections");if(!connectionLive(epoch,generation))return;
    $("connections-feedback").textContent=result.enabled?"只读范围由企业应用与本人同意共同决定。撤销后停止新的读取。":"企业连接暂未启用，已有同意仍可撤销。";
    if(!result.connections.length)$("connections-list").append(connectionText("p","管理员尚未配置企业微信应用。"));
    for(const record of result.connections)$("connections-list").append(connectionCard(record,result.enabled,epoch,generation));
  }catch(error){if(connectionLive(epoch,generation))$("connections-feedback").textContent=error.message;}
}
function connectionCard(record,enabled,epoch,generation){
  const card=connectionText("article","");card.className="function-card";
  card.append(connectionText("h3",record.name),connectionText("p",record.status==="active"?"企业应用已配置；本人同意单独管理。":"企业应用尚未启用或权限尚待核定。"));
  const cid=encodeURIComponent(record.id),active=enabled&&record.status==="active";
  if(active){
    card.append(connectionButton("绑定我的企业微信",async()=>{
      try{const result=await api("POST",`/v1/me/connections/${cid}/binding/start`,{json:{}});if(!connectionLive(epoch,generation))return;
        const target=new URL(result.authorization_url);if(target.origin!=="https://open.weixin.qq.com")throw new Error("授权地址无效");location.assign(target.href);
      }catch(error){if(connectionLive(epoch,generation))$("connections-feedback").textContent=error.message;}
    }));
    if(record.operations.includes("self_profile.read"))card.append(connectionButton("授权本人资料只读（1小时）",()=>connectionConsent(card,record,"self_profile.read",epoch,generation)));
    if(isAdmin()&&record.operations.includes("directory_preview.read"))card.append(connectionButton("授权通讯录预览（1小时）",()=>connectionConsent(card,record,"directory_preview.read",epoch,generation)));
    card.append(connectionButton("读取我的基础资料",()=>connectionRead(card,record,"self_profile.read",epoch,generation)));
    if(isAdmin())card.append(connectionButton("预览应用可见通讯录",()=>connectionRead(card,record,"directory_preview.read",epoch,generation)));
  }
  for(const consent of record.consents||[]){
    const row=connectionText("div","");
    const expires=new Date(consent.expires_at*1000).toLocaleString();
    row.append(connectionText("p",`${consent.status==="active"?"本人已同意":consent.status==="revoked"?"本人已撤销":"本人同意已到期"} · ${consent.purpose} · 到期 ${expires}`));
    if(consent.status==="active"){
      const request={request_id:crypto.randomUUID(),expected_version:consent.version};
      const revoke=connectionButton("撤销这项授权",async()=>{
        revoke.disabled=true;try{await api("POST",`/v1/me/connections/${cid}/consents/${encodeURIComponent(consent.consent_id)}/revoke`,{json:request});
          if(connectionLive(epoch,generation))await openConnections();
        }catch(error){if(connectionLive(epoch,generation)){$("connections-feedback").textContent=error.message;revoke.disabled=false;revoke.textContent="重试同一次撤销";}}
      });row.append(revoke);
    }card.append(row);
  }return card;
}
function connectionConsent(card,record,operation,epoch,generation){
  if(!connectionLive(epoch,generation))return;
  const review=connectionText("section","");review.className="reminder-preview";
  const directory=operation==="directory_preview.read";
  const purpose=directory?"管理员核对应用可见通讯录":"本人基础资料核对";
  review.append(connectionText("h4","请确认本次只读授权"),connectionText("p",directory?"允许玄圃预览此应用可见的部门和成员。范围可能不完整。":"允许玄圃读取你本人的成员账号、姓名、岗位和部门；供应商未提供的字段保持缺失。"),connectionText("p","用途："+purpose+"。有效期：1小时。可随时撤销；撤销会阻止新读取，但无法追回已查看或下载的内容。"));
  const request={request_id:crypto.randomUUID(),operations:[operation],expires_at:Date.now()/1000+3600,purpose};
  const approve=connectionButton(directory?"同意并允许预览通讯录":"同意并允许读取本人资料",async()=>{
    approve.disabled=true;try{await api("POST",`/v1/me/connections/${encodeURIComponent(record.id)}/consents`,{json:request});
      if(connectionLive(epoch,generation))await openConnections();
    }catch(error){if(connectionLive(epoch,generation)){$("connections-feedback").textContent=error.message;approve.disabled=false;approve.textContent="重试同一确认";}}
  });review.append(approve,connectionButton("暂不同意",()=>review.remove()));card.append(review);
}
async function connectionRead(card,record,operation,epoch,generation){
  const resultNode=connectionText("div","");card.append(resultNode);
  try{
    const result=await api("POST",`/v1/me/connections/${encodeURIComponent(record.id)}/read`,{json:{operation,parameters:{}}});
    if(!connectionLive(epoch,generation))return;
    if(operation==="self_profile.read"){
      const data=result.data;
      resultNode.replaceChildren(connectionText("p",`姓名：${data.name??"供应商未提供"}；岗位：${data.position??"供应商未提供"}；部门：${data.department?.join("、")??"供应商未提供"}`));
    }else{
      resultNode.replaceChildren(connectionText("p","以下仅为应用当前可见范围，不能据此判断未出现成员已离职。"));
      for(const dept of result.departments)resultNode.append(connectionText("p",`${dept.name??"未提供部门名称"}：${dept.members.map(m=>m.name??"未提供姓名").join("、")}`));
    }
  }catch(error){if(connectionLive(epoch,generation))resultNode.textContent=error.message;}
}
async function handleConnectionRoute(route,text){
  if(!route.open_connections)return false;
  append("user",text);append("assistant",route.message);$("message").value="";chat.routeContext=[];await openConnections();return true;
}
if(typeof document!=="undefined"){
  $("open-connections").addEventListener("click",()=>openConnections());
  $("close-connections").addEventListener("click",()=>resetConnections());
}
