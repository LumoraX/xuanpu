// 组织配置采用显式确认卡；没有模型时同样可以完成配置和撤销。
"use strict";
function organizationField(form,label,options={}){
  const wrapper=organizationNode("label",label),input=document.createElement(options.choices?"select":"input");
  if(options.choices)for(const [value,text] of options.choices){
    const option=organizationNode("option",text);option.value=value;input.append(option);
  }
  input.required=!!options.required;input.value=options.value??options.choices?.[0]?.[0]??"";input.maxLength=120;
  wrapper.append(input);form.append(wrapper);return input;
}
function organizationForm(host,title,setup){
  const card=organizationNode("article","");card.className="function-card";
  const form=document.createElement("form");form.className="organization-form";
  card.append(organizationNode("h3",title),form);host.append(card);setup(form,card);
}
function organizationSubmit(form,text,action){
  const submit=organizationNode("button",text);submit.type="submit";form.append(submit);
  form.addEventListener("submit",event=>{event.preventDefault();action();});
}
function organizationChoices(data,kind,scope=null){
  if(kind==="people")return data.people.map(user=>[user.user_id,user.display_name+" · 员工编号 "+user.user_id.slice(-6)]);
  return data[kind].items.filter(record=>record.status==="active"&&(!scope||record.unit_id===scope.value)).map(record=>[record.id,record.name||"员工任职"]);
}
function organizationSelect(form,label,data,kind,optional=false,scope=null){
  const choices=organizationChoices(data,kind,scope);
  if(optional)choices.unshift(["","无"]);
  const input=organizationField(form,label,{choices,required:!optional});
  (data.selects||(data.selects=[])).push({input,kind,optional,scope});return input;
}
function organizationUpdateSelects(data,kind){
  for(const control of data.selects||[]){
    if(control.kind!==kind)continue;
    const value=control.input.value;control.input.replaceChildren();
    const choices=organizationChoices(data,kind,control.scope);if(control.optional)choices.unshift(["","无"]);
    for(const [key,text] of choices){const option=organizationNode("option",text);option.value=key;control.input.append(option);}
    control.input.value=value;
  }
}
function organizationPeople(host,data){
  const card=organizationNode("article","");card.className="function-card";
  card.append(organizationNode("h3","可选择员工（"+data.peoplePage.total+"）"),
    organizationNode("p","员工可以直接协作，部门和岗位是可选配置。请用稳定员工编号区分同名员工。"));host.append(card);
  function more(){
    if(!data.peoplePage.next_cursor)return;
    const button=organizationButton("加载更多可选员工",async()=>{
      const epoch=conn.epoch,generation=organizationState.generation;button.disabled=true;
      try{
        const next=await api("GET","/v1/organization/people?limit=100&cursor="+encodeURIComponent(data.peoplePage.next_cursor));
        if(!organizationLive(epoch,generation))return;
        data.people.push(...next.items);data.peoplePage=next;
        organizationUpdateSelects(data,"people");button.remove();more();
      }catch(error){if(organizationLive(epoch,generation)){organizationNotice(error.message);button.disabled=false;}}
    });card.append(button);
  }
  more();
}
function organizationManagementForms(host,data){
  const all=isAdmin()||data.me.roles.some(role=>role.role==="org_manager"&&role.scope_id===null);
  if(data.me.enabled!==false){
    organizationForm(host,"新增部门",(form,card)=>{
      const name=organizationField(form,"部门名称",{required:true});
      const parent=organizationSelect(form,"上级部门",data,"units",all);
      organizationSubmit(form,"核对新部门",()=>organizationReview(card,"保存部门","POST","/v1/admin/organization/units",
        {request_id:crypto.randomUUID(),name:name.value.trim(),parent_id:parent.value||null},[name.value.trim(),"上级部门："+(parent.value?"已明确选择":"无")]));
    });
    organizationForm(host,"新增岗位",(form,card)=>{
      const unit=organizationSelect(form,"所属部门",data,"units"),name=organizationField(form,"岗位名称",{required:true});
      organizationSubmit(form,"核对新岗位",()=>organizationReview(card,"保存岗位","POST","/v1/admin/organization/positions",
        {request_id:crypto.randomUUID(),unit_id:unit.value,name:name.value.trim()},[name.value.trim(),"岗位不会自动授予承办或审批职责。"]));
    });
    organizationForm(host,"配置员工任职",(form,card)=>{
      const person=organizationSelect(form,"员工（同名时请核对员工编号）",data,"people");
      const unit=organizationSelect(form,"所属部门",data,"units"),position=organizationSelect(form,"岗位（可选）",data,"positions",true,unit);
      unit.addEventListener("change",()=>organizationUpdateSelects(data,"positions"));
      organizationSubmit(form,"核对员工任职",()=>organizationReview(card,"保存任职","POST","/v1/admin/organization/memberships",
        {request_id:crypto.randomUUID(),user_id:person.value,unit_id:unit.value,position_id:position.value||null},
        ["任职不自动授予管理员、承办或审批权限。"]));
    });
    organizationForm(host,"配置业务职责",(form,card)=>{
      const person=organizationSelect(form,"员工（同名时请核对员工编号）",data,"people");
      const unit=organizationSelect(form,"职责部门",data,"units",all);
      const role=organizationField(form,"职责",{choices:[["unit_operator","部门承办"],["unit_approver","部门审批"],...(all?[["org_manager","组织管理"]]:[])]});
      organizationSubmit(form,"核对业务职责",()=>{
        if(!unit.value&&role.value!=="org_manager"){organizationNotice("承办和审批职责必须明确选择部门。");return;}
        organizationReview(card,"保存职责","POST","/v1/admin/organization/roles",
          {request_id:crypto.randomUUID(),user_id:person.value,scope_id:unit.value||null,role:role.value},
          ["职责仅作用于明确选择的部门范围；不会更改员工身份角色。"]);
      });
    });
  }
  for(const kind of ["units","positions","memberships","roles"]){
    const titles={units:"部门",positions:"岗位",memberships:"任职",roles:"业务职责"};
    const list=organizationNode("article","");list.className="function-card";
    list.append(organizationNode("h3",titles[kind]+"（"+data[kind].total+"）"));host.append(list);
    organizationEntityRows(list,data,kind,data[kind].items);
    organizationMoreRows(list,data,kind);
  }
}
function organizationEntityRows(host,data,kind,records){
  const roles={org_manager:"组织管理",unit_operator:"部门承办",unit_approver:"部门审批"};
  for(const record of records){
    const person=data.people.find(p=>p.user_id===record.user_id),unit=data.units.items.find(u=>u.id===(record.scope_id||record.unit_id));
    const title=record.name||(person?.display_name||"员工")+" · "+(roles[record.role]||"任职")+" · "+(unit?.name||"企业范围");
    const row=organizationNode("section","");row.append(organizationNode("p",title+" · "+(record.status==="active"?"启用":"已停用／撤销")));host.append(row);
    if(record.status!=="active")continue;
    if((kind==="units"||kind==="positions")&&data.me.enabled!==false){
      const name=organizationField(row,"新名称",{required:true,value:record.name});
      row.append(organizationButton("核对改名",()=>organizationReview(row,"保存名称","PATCH",
        "/v1/admin/organization/"+kind+"/"+encodeURIComponent(record.id),
        {request_id:crypto.randomUUID(),expected_version:record.version,name:name.value.trim()},[title+" → "+name.value.trim()])));
    }
    if(kind==="memberships"&&data.me.enabled!==false){
      const position=organizationSelect(row,"调整岗位（可选）",data,"positions",true,{value:record.unit_id});
      position.value=record.position_id||"";
      row.append(organizationButton("核对岗位调整",()=>organizationReview(row,"调整岗位","PATCH",
        "/v1/admin/organization/memberships/"+encodeURIComponent(record.id),
        {request_id:crypto.randomUUID(),expected_version:record.version,position_id:position.value||null},
        [title,"仅调整岗位，不自动赋予业务职责。"])));
    }
    const status=kind==="units"||kind==="positions"?"disabled":"revoked";
    row.append(organizationButton(status==="disabled"?"核对停用":"核对撤销",()=>organizationReview(row,
      status==="disabled"?"停用":"撤销","PATCH","/v1/admin/organization/"+kind+"/"+encodeURIComponent(record.id),
      {request_id:crypto.randomUUID(),expected_version:record.version,status},
      [title,"历史责任记录保留；新操作立即按当前有效权限检查。"])));
  }
}
function organizationMoreRows(host,data,kind){
  if(!data[kind].next_cursor)return;
  const button=organizationButton("查看更多",async()=>{
    const epoch=conn.epoch,generation=organizationState.generation;button.disabled=true;
    try{
      const next=await api("GET","/v1/admin/organization/"+kind+"?limit=100&cursor="+encodeURIComponent(data[kind].next_cursor));
      if(!organizationLive(epoch,generation))return;
      data[kind].items.push(...next.items);data[kind].next_cursor=next.next_cursor;
      organizationEntityRows(host,data,kind,next.items);organizationUpdateSelects(data,kind);button.remove();organizationMoreRows(host,data,kind);
    }catch(error){if(organizationLive(epoch,generation)){organizationNotice(error.message);button.disabled=false;}}
  });host.append(button);
}
function organizationSourceForms(host,connections){
  const available=connections.filter(c=>c.status==="active"&&c.operations.includes("directory_preview.read"));
  const choices=[["local","玄圃本地维护"],...available.map(c=>[c.id,c.name])];
  organizationForm(host,"字段来源",(form,card)=>{
    const object=organizationField(form,"资料类别",{choices:[["unit","部门"],["membership","员工任职"],["user","员工姓名"]]});
    const field=organizationField(form,"字段",{choices:[["name","部门名称"],["parent_id","部门上级"],["membership","员工任职"],["display_name","员工姓名"],["status","启用状态"]]});
    const fields={unit:[["name","部门名称"],["parent_id","部门上级"],["status","启用状态"]],
      membership:[["membership","员工任职"],["status","启用状态"]],user:[["display_name","员工姓名"],["status","启用状态"]]};
    function refreshFields(){
      field.replaceChildren();for(const [value,text] of fields[object.value]){
        const option=organizationNode("option",text);option.value=value;field.append(option);
      }field.value=fields[object.value][0][0];
    }
    object.addEventListener("change",refreshFields);refreshFields();
    const source=organizationField(form,"权威来源",{choices});
    organizationSubmit(form,"预览来源变更",async()=>{
      const epoch=conn.epoch,generation=organizationState.generation;
      try{
        const preview=await api("POST","/v1/admin/organization/authority/previews",
          {json:{request_id:crypto.randomUUID(),object_type:object.value,field:field.value,source:source.value}});
        if(!organizationLive(epoch,generation))return;
        organizationReview(card,"切换字段来源","POST","/v1/admin/organization/authority/previews/"+encodeURIComponent(preview.id)+"/apply",
          {request_id:crypto.randomUUID(),expected_version:preview.version},["将影响 "+preview.affected_count+" 条记录的字段归属。","本次切换不会自动应用通讯录差异。"]);
      }catch(error){if(organizationLive(epoch,generation))organizationNotice(error.message);}
    });
  });
  organizationForm(host,"企微目录差异",(form,card)=>{
    if(!available.length){card.append(organizationNode("p","尚无启用的通讯录只读来源。直接到人协作不需要先导入部门。"));return;}
    const connection=organizationField(form,"已配置的目录来源",{choices:available.map(c=>[c.id,c.name]),required:true});
    card.append(organizationNode("p","请先在企业连接确认本人通讯录只读授权。只有明确选中的差异才会进入玄圃。"));
    organizationSubmit(form,"采集并预览目录",async()=>{
      const epoch=conn.epoch,generation=organizationState.generation;
      try{
        const preview=await api("POST","/v1/admin/organization/import-previews",
          {json:{request_id:crypto.randomUUID(),connection_id:connection.value}});
        if(organizationLive(epoch,generation))organizationImportPreview(card,preview);
      }catch(error){if(organizationLive(epoch,generation))organizationNotice(error.message);}
    });
  });
}
