// 本人知识与记忆（R4-D）：登记来源、检索、依据选定片段提问、确认/拒绝/删除记忆。
// 不依赖模型也能检索、看来源、管理记忆；依据失效时先清空问答再提示，迟到响应不回填旧身份。
"use strict";
const knowledgeState={generation:0,pins:new Map(),context:null,createRequest:null,askRequest:null};
function knowledgeLive(epoch,generation){return epoch===conn.epoch&&generation===knowledgeState.generation;}
function knowledgeNotice(text){$("knowledge-feedback").textContent=text;}
function knowledgeClearAnswer(){$("knowledge-answer").replaceChildren();knowledgeState.context=null;knowledgeState.askRequest=null;}
function resetKnowledge(){
  knowledgeState.generation++;knowledgeState.pins.clear();knowledgeState.createRequest=null;knowledgeClearAnswer();
  for(const id of ["knowledge-sources","knowledge-hits","knowledge-memories"])$(id).replaceChildren();
  knowledgeNotice("");$("knowledge-panel").classList.add("hidden");
}
function knowledgeRevoked(error){
  if(error.code!=="knowledge_source_revoked")return false;
  knowledgeClearAnswer();knowledgeNotice("依据的来源已删除、更新或不再可读，旧问答已清除。请重新检索并选择依据。");
  return true;
}
async function openKnowledge(){
  const epoch=conn.epoch,generation=++knowledgeState.generation;
  $("knowledge-panel").classList.remove("hidden");$("knowledge-panel").scrollIntoView?.({block:"start"});
  knowledgeNotice("正在读取知识来源…");
  try{
    await knowledgeSources(epoch,generation);
    if(!knowledgeLive(epoch,generation))return;
    await knowledgeMemories(epoch,generation);
    if(knowledgeLive(epoch,generation))knowledgeNotice("检索只在你此刻能读到的来源里进行；提问只依据你选的片段。");
  }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.status===403?"企业尚未开启知识检索，或你没有权限。":error.message);}
}
async function knowledgeSources(epoch,generation){
  const body=await api("GET","/v1/me/knowledge/sources");if(!knowledgeLive(epoch,generation))return;
  if(!Array.isArray(body.sources))throw new Error("来源列表响应不完整，请重新读取。");
  const host=$("knowledge-sources");host.replaceChildren(connectionText("h3","我的来源（"+body.sources.length+"）"));
  const labels={ready:"可检索",stale:"原文件已不可读"};
  for(const source of body.sources){
    const card=connectionText("article","");card.className="function-card";
    card.append(connectionText("h4",source.title+" · 第 "+source.revision+" 版"+(source.kind==="shared_material"?" · 共享资料":"")),
      connectionText("p",(labels[source.status]||source.status)+" · "+source.chunks+" 段"+(source.has_update?" · 原文件有新版本":"")));
    if(source.has_update)card.append(connectionButton("换成新版本",()=>knowledgeChange(source,"refresh",epoch,generation)));
    card.append(connectionButton("删除来源",()=>knowledgeChange(source,"delete",epoch,generation)));
    host.append(card);
  }
  host.append(connectionButton("从我的文件登记来源",()=>knowledgePickFile(host,epoch,generation)),
    connectionButton("从共享资料登记来源",()=>knowledgePickShared(host,epoch,generation)));
}
async function knowledgeChange(source,action,epoch,generation){
  try{
    await api("POST","/v1/me/knowledge/sources/"+encodeURIComponent(source.id)+"/"+action,{json:{expected_version:source.version}});
    if(!knowledgeLive(epoch,generation))return;
    knowledgeNotice(action==="delete"?"已删除来源，依据它的问答已失效。":"已换成新版本，旧问答已失效。");
    await knowledgeSources(epoch,generation);
  }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.status===409?"来源已变化，请刷新后重试。":error.message);}
}
async function knowledgePickFile(host,epoch,generation){
  try{
    const {items}=await api("GET","/v1/workspace/items?kind=file");if(!knowledgeLive(epoch,generation))return;
    const label=connectionText("label","选择文件 "),select=connectionText("select","");
    const empty=connectionText("option","请选择");empty.value="";select.append(empty);
    for(const item of items||[]){const option=connectionText("option",String(item.data?.name||item.id));option.value=item.id;select.append(option);}
    label.append(select);host.append(label);
    select.addEventListener("change",async()=>{
      const item=(items||[]).find(i=>i.id===select.value);if(!item)return;
      const revision=Number(item.data?.current_revision);
      if(!Number.isInteger(revision)||revision<1){knowledgeNotice("无法确定文件版本，请在文件页查看。");return;}
      try{
        await api("POST","/v1/me/knowledge/sources",{json:{item_id:item.id,revision}});
        if(knowledgeLive(epoch,generation)){knowledgeNotice("已登记。");await knowledgeSources(epoch,generation);}
      }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.status===400?"只支持 UTF-8 的 TXT、Markdown、CSV、JSON 文件。":error.message);}
    });
  }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.message);}
}
// 共享事项里本人此刻能读的材料；登记不复制分享权，被移出事项或材料撤回后来源失效
async function knowledgePickShared(host,epoch,generation){
  try{
    const page=await api("GET","/v1/shared-matters?limit=50");if(!knowledgeLive(epoch,generation))return;
    const label=connectionText("label","选择共享事项 "),select=connectionText("select","");
    const empty=connectionText("option","请选择");empty.value="";select.append(empty);
    for(const item of page.items||[]){const option=connectionText("option",String(item.title));option.value=item.id;select.append(option);}
    label.append(select);host.append(label);
    select.addEventListener("change",async()=>{
      if(!select.value)return;
      try{
        const listed=await api("GET","/v1/shared-matters/"+encodeURIComponent(select.value)+"/materials");if(!knowledgeLive(epoch,generation))return;
        for(const material of listed.items||[]){
          const latest=(material.revisions||[]).reduce((a,b)=>b.revision>a.revision?b:a,material.revisions[0]);if(!latest)continue;
          host.append(connectionButton("登记「"+material.name+"」第 "+latest.revision+" 版",async()=>{
            try{
              await api("POST","/v1/me/knowledge/sources",{json:{kind:"shared_material",matter_id:select.value,material_id:material.id,revision:latest.revision}});
              if(knowledgeLive(epoch,generation)){knowledgeNotice("已登记。被移出事项或材料撤回后，这个来源会自动失效。");await knowledgeSources(epoch,generation);}
            }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.status===400?"只支持 UTF-8 的 TXT、Markdown、CSV、JSON 文件。":error.message);}
          }));
        }
      }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.message);}
    });
  }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.message);}
}
async function knowledgeSearch(){
  const epoch=conn.epoch,generation=knowledgeState.generation,query=$("knowledge-query").value.trim();
  if(!query){knowledgeNotice("请输入要找的词。");return;}
  try{
    const body=await api("POST","/v1/me/knowledge/search",{json:{query,limit:10}});if(!knowledgeLive(epoch,generation))return;
    if(!Array.isArray(body.hits))throw new Error("检索响应不完整，请重试。");
    knowledgeState.pins.clear();knowledgeState.createRequest=null;knowledgeState.context=null;
    const host=$("knowledge-hits");host.replaceChildren(connectionText("p","共 "+body.total+" 处，显示 "+body.hits.length+" 处。"));
    for(const hit of body.hits){
      const card=connectionText("article","");card.className="function-card";
      const label=connectionText("label",""),box=connectionText("input","");box.type="checkbox";
      box.addEventListener("change",()=>{const key=hit.source_id+":"+hit.ordinal;
        if(box.checked)knowledgeState.pins.set(key,{source_id:hit.source_id,ordinal:hit.ordinal});else knowledgeState.pins.delete(key);
        knowledgeState.createRequest=null;knowledgeState.context=null;});
      label.append(box,connectionText("span"," 选作依据"));
      card.append(connectionText("h4",hit.title+" · 第 "+hit.revision+" 版 第 "+(hit.ordinal+1)+" 段"),connectionText("p",hit.excerpt),label);
      host.append(card);
    }
  }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.message);}
}
async function knowledgeAsk(){
  const epoch=conn.epoch,generation=knowledgeState.generation,question=$("knowledge-question").value.trim();
  if(!question){knowledgeNotice("请输入问题。");return;}
  if(!knowledgeState.context&&!knowledgeState.pins.size){knowledgeNotice("请先在检索结果里勾选依据。");return;}
  const button=$("knowledge-ask");if(button.disabled)return;button.disabled=true;
  try{
    if(!knowledgeState.context){
      knowledgeState.createRequest=knowledgeState.createRequest||crypto.randomUUID();
      const created=await api("POST","/v1/me/knowledge/contexts",{json:{request_id:knowledgeState.createRequest,pins:[...knowledgeState.pins.values()]}});
      if(!knowledgeLive(epoch,generation))return;
      knowledgeState.context=created.id;
    }
    knowledgeState.askRequest=knowledgeState.askRequest||crypto.randomUUID();
    knowledgeNotice("正在依据所选片段回答…");
    const answer=await api("POST","/v1/me/knowledge/contexts/"+encodeURIComponent(knowledgeState.context)+"/ask",
      {json:{request_id:knowledgeState.askRequest,question}});
    if(!knowledgeLive(epoch,generation))return;
    knowledgeState.askRequest=null;knowledgeRender(answer);
    knowledgeNotice(answer.grounded?"回答已标出依据。":"所选材料里没有找到依据，回答仅供参考。");
  }catch(error){
    if(!knowledgeLive(epoch,generation))return;
    if(!knowledgeRevoked(error))knowledgeNotice(error.status===409?"问答已变化，请刷新后重试。":error.message+"（重试会沿用同一次提问）");
  }finally{button.disabled=false;}
}
function knowledgeRender(answer){
  const card=connectionText("article","");card.className="function-card";
  card.append(connectionText("h4","问："+answer.question),connectionText("p",answer.text));
  for(const c of answer.citations||[])card.append(connectionText("p","["+c.index+"] "+c.title+" · 第 "+c.revision+" 版 第 "+(c.ordinal+1)+" 段"));
  $("knowledge-answer").append(card);
}
async function knowledgeMemories(epoch,generation){
  const body=await api("GET","/v1/me/work-memory");if(!knowledgeLive(epoch,generation))return;
  if(!Array.isArray(body.memories))throw new Error("记忆列表响应不完整，请重新读取。");
  const host=$("knowledge-memories");host.replaceChildren(connectionText("h3","我的工作记忆（"+body.memories.length+"）"));
  for(const memory of body.memories){
    const card=connectionText("article","");card.className="function-card";
    const kind=memory.kind==="user_assertion"?"本人陈述（未经材料核实）":"有材料依据";
    card.append(connectionText("p",memory.text),connectionText("p",kind+" · "+(memory.status==="proposed"?"待你确认":"生效中")));
    const act=(action,label)=>connectionButton(label,async()=>{
      try{await api("POST","/v1/me/work-memory/"+encodeURIComponent(memory.id)+"/"+action,{json:{expected_version:memory.version}});
        if(knowledgeLive(epoch,generation))await knowledgeMemories(epoch,generation);}
      catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.status===409?"记忆已变化，请刷新后重试。":error.message);}
    });
    if(memory.status==="proposed")card.append(act("confirm","确认"),act("reject","不采纳"));
    card.append(act("delete","删除"));host.append(card);
  }
}
async function knowledgeAssert(){
  const epoch=conn.epoch,generation=knowledgeState.generation,text=$("knowledge-assertion").value.trim();
  if(!text){knowledgeNotice("请输入要记住的内容。");return;}
  try{
    await api("POST","/v1/me/work-memory/assertions",{json:{text}});
    if(!knowledgeLive(epoch,generation))return;
    $("knowledge-assertion").value="";knowledgeNotice("已记下，标为你本人陈述。");await knowledgeMemories(epoch,generation);
  }catch(error){if(knowledgeLive(epoch,generation))knowledgeNotice(error.message);}
}
if(typeof document!=="undefined"){
  $("open-knowledge").addEventListener("click",()=>openKnowledge());
  $("close-knowledge").addEventListener("click",()=>resetKnowledge());
  $("knowledge-search").addEventListener("click",()=>knowledgeSearch());
  $("knowledge-ask").addEventListener("click",()=>knowledgeAsk());
  $("knowledge-remember").addEventListener("click",()=>knowledgeAssert());
}
