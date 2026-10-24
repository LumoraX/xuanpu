// 共享阅读始终有独立上下文和来源；受保护草稿不流入普通个人对话。
"use strict";
async function openSharedConversation(identifier,matterId=null){
 const epoch=conn.epoch,generation=++sharedState.generation;sharedState.context=null;const host=$("shared-list");host.replaceChildren();
 $("shared-panel").classList.remove("hidden");sharedNotice("正在重新核对阅读来源…");
 try{
  const source=await api("GET","/v1/shared-contexts/"+encodeURIComponent(identifier)+"/read");if(!sharedLive(epoch,generation))return;
  const id=matterId||source.sources[0]?.shared_id,matter=await api("GET",sharedPath(id));if(!sharedLive(epoch,generation))return;
  sharedState.context=identifier;sharedState.matter=matter;
  host.append(sharedButton("返回事项资料",()=>openSharedMatter(id)),sharedNode("h3",source.title),sharedNode("p","本次只读已确认的固定来源。重新授权后须开始新阅读。"));
  const history=await api("GET","/v1/workspace/messages?latest=true&limit=100&source_context_id="+encodeURIComponent(identifier));if(!sharedLive(epoch,generation))return;
  const messages=sharedNode("section");messages.className="shared-transcript";host.append(messages);
  for(const row of history.items){if(row.source_blocked)continue;const card=sharedNode("article");card.className="function-card";card.append(sharedNode("strong",row.role==="user"?"我":"助手"),sharedNode("p",row.text));messages.append(card);
   if(row.role==="assistant"&&matter.allowed_actions.includes("publish_material"))sharedTextPublication(card,matter,identifier,row);
  }
  sharedForm(host,"向选定资料提问",(form,card)=>{
   const text=sharedField(form,"问题"),submit=sharedSubmit(form,"发送",async()=>{
    if(submit.disabled||!text.value.trim()||!sharedLive(epoch,generation))return;
    const body={request_id:crypto.randomUUID(),shared_context_id:identifier,text:text.value};
    sharedReview(card,"发送问题","POST","/v1/assistant/route",body,["本次仅使用已确认来源回答。","问题："+text.value],()=>openSharedConversation(identifier,id));
   });text.maxLength=8000;text.required=true;
  });
  host.append(sharedButton("结束本次阅读",async()=>{try{
   const metadata=await api("GET","/v1/shared-contexts/"+encodeURIComponent(identifier));if(!sharedLive(epoch,generation))return;
   sharedReview(host,"结束阅读","POST","/v1/shared-contexts/"+encodeURIComponent(identifier)+"/close",{request_id:crypto.randomUUID(),confirmed:true,expected_version:metadata.version},["本次阅读关闭后不能再次启用。历史保留来源权限保护。"],()=>sharedReadingList());
  }catch(error){if(sharedLive(epoch,generation))sharedNotice(error.message);}}));
  sharedNotice("资料正文按固定版本读取；二进制及过长材料会明确标记未读或节选。");
 }catch(error){if(sharedLive(epoch,generation)){sharedInvalidate(error.message);host.append(sharedButton("重新选择资料",()=>openShared()));}}
}
function sharedTextPublication(host,matter,identifier,row){
 host.append(sharedButton("将此草稿发布为共享材料",()=>{
  sharedForm(host,"核对草稿发布",(form,card)=>{const name=sharedField(form,"材料名称","共享草稿.txt");name.maxLength=255;
   sharedSubmit(form,"核对固定草稿",()=>sharedReview(card,"发布草稿","POST",sharedPath(matter.id)+"/material-texts",{request_id:crypto.randomUUID(),confirmed:true,expected_version:matter.version,source_context_id:identifier,draft_message_id:row.id,name:name.value},["发布目标："+matter.title,"草稿原文："+row.text,"发布固定原文并保留来源。参与者仍需有原来源的当前权限。"],result=>sharedPublication(matter,result)));
  });
 }));
}
async function sharedReadingList(cursor=null,appendPage=false){
 const epoch=conn.epoch,generation=appendPage?sharedState.generation:++sharedState.generation;const host=$("shared-list");if(!appendPage){host.replaceChildren();sharedState.matter=null;sharedState.context=null;}
 try{
  const page=await api("GET","/v1/shared-contexts?limit=50"+(cursor?"&cursor="+encodeURIComponent(cursor):""));if(!sharedLive(epoch,generation))return;
  if(!appendPage)host.append(sharedButton("返回共享事项",()=>openShared()),sharedNode("h3","我的共享阅读（"+page.total+"）"));
  for(const item of page.items){const card=sharedNode("article");card.className="function-card";card.append(sharedNode("p","阅读 "+item.id.slice(-8)+" · "+({active:"进行中",blocked:"来源失效",closed:"已结束"}[item.status])));host.append(card);
   if(item.kind==="assistant_conversation"&&item.status==="active")card.append(sharedButton("继续本次阅读",()=>openSharedConversation(item.id)));
   if(item.status!=="closed")card.append(sharedButton("结束阅读",()=>sharedReview(card,"结束阅读","POST","/v1/shared-contexts/"+encodeURIComponent(item.id)+"/close",{request_id:crypto.randomUUID(),confirmed:true,expected_version:item.version},["结束阅读 "+item.id.slice(-8)+"。"],()=>sharedReadingList())));
  }
  if(page.next_cursor)host.append(sharedButton("查看更多阅读",()=>sharedReadingList(page.next_cursor,true)));
 }catch(error){if(sharedLive(epoch,generation))sharedNotice(error.message);}
}
