"""人物与关系的内置提案约定：由分流与管家共用，写入始终经员工本人确认。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.domains.people.graph_rules import merge_org_profile_patch
from server.domains.people.models import PeopleBundleInput
from server.domains.people.rules import merge_profile_patch
from server.engine.assistant.reminders import RouteOutcome

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.platform import Platform

PEOPLE_PACKAGE = "@people"
PEOPLE_PROMPT = """个人知识图谱与关系是平台内置个人空间能力（仅本人可见），以「原始记录（随手记/会议记录/名片/通话）」为事实源头，拆解沉淀公司/组织、人物、项目/事项、待办/日程/提醒及跨实体关系网。
不要把「记录联系人/公司/会议纪要/人际与项目关系」推脱为“无法创建”或退化成孤立的一条纯文本事项。
查看或管理本人图谱档案选择 @people/manage（不带 people_draft）。

【五类诉求路由】
- 简单单点记录（如单条提醒/单条待办/只记一人手机号）：保持轻量，不强行展开多余分支。
- 复合事实/随手记（提到多位人物、公司隶属、已有联系人补充、多个项目与关系）：选择 package="@people"、entry="record"、message=null，填写 people_draft。
- 会议记录/纪要复盘：同样走 @people/record，将原始记录 record_kind 设为 "meeting"，并按四层拆解：① 源头与场景（title/occurred_at/location/content）；② 纪要结论（summary/decisions/open_questions）；③ 静态图谱增量（orgs/persons/matters/relations）；④ 动态行动项（todos/events/reminders）。
- 多跳查询/溯源/项目全景：先并行调用 read_personal（orgs_search/people_search/matters/records_search/entity_graph），直接基于图谱事实回答并标注出处日期，不要调用 propose。

【people_draft 结构】（顶层仅允许 record、orgs、persons、matters、todos、events、reminders、relations）
{"record":{"record_kind":"note|meeting|card|call|other","title":简短标题,"location":地点,"occurred_at":UNIX秒时间戳(可选),
  "content":原始全文,"summary":纪要摘要(会议填),"decisions":[关键结论],"open_questions":[待确认问题],
  "person_refs":[person_id或temp_ref(放record内)],"org_refs":[org_id或temp_ref(放record内)],"matter_refs":[matter_id或temp_ref(放record内)]},
"orgs":[{"action":"create|update|link"(查不到已有公司用create且不填org_id；update/link必填已有org_id),"temp_ref":"o1","org_id":已有org_id(可选),"expected_version":可选,
  "profile":{"name":规范公司全称,"aliases":[简称],"org_type":类型,"industry":行业,"city":城市,"address":公司地址,"tags":[标签],"summary":说明}}],
"persons":[{"action":"create|update"(新人物用create；update必填已有person_id),"temp_ref":"p1","person_id":已有person_id(可选),"expected_version":可选,
  "profile":{"name":姓名,"aliases":[别名],"gender":"male|female|other|unknown",
  "birth":{"year":年,"month":月,"day":日,"calendar":"solar|lunar"},
  "organization":规范公司全称,"department":部门,"title":职位,"city":城市,
  "contacts":[{"kind":"phone|wechat|email|feishu|qq|other","value":号码,"label":标签,"primary":true|false}],
  "tags":[标签],"summary":简述,"attachment_file_ids":[个人文件ID],"extra":{"address":完整详细地址, ...}}}],
"matters":[{"action":"create|link"(新项目用create；link必填已有matter_id),"temp_ref":"m1","matter_id":已有matter_id(可选),"title":项目或事项标题,"description":背景说明}],
"todos":[{"title":行动待办,"description":说明,"due_at":可选UNIX秒,"matter_ref":可选matter_id或m1}],
"events":[{"title":约定日程,"description":说明,"start_at":UNIX秒,"end_at":UNIX秒,"timezone":"Asia/Shanghai","matter_ref":可选}],
"reminders":[{"title":跟进提醒,"body":说明,"due_at":UNIX秒,"timezone":"Asia/Shanghai"}],
"relations":[{"from_kind":"person|org|matter","from_ref":实体ID或temp_ref(如per_xxx/p1/o1),"target_kind":"person|org|matter"(只填类型名,不填ID或ref),"target_ref":目标ID或temp_ref(如p1/o1/m1),
  "relation_type":关系词,"symmetric":true|false,"role":角色,"note":备注,"valid_from":YYYY-MM或null,"valid_to":YYYY-MM或null}]}。

【强制拆解与规范要求】
1. 先查再提：凡输入涉及已有联系人（如“刚刚的朱佳”“何总”）、公司名或项目名，必须先并行调用 read_personal（people_search + orgs_search + matters）查出已有 person_id / org_id / matter_id 再生成草稿！
2. 原始记录为根、全量拆解所有分支：
   - 公司是一等实体！提到母子公司或工作单位（如“上海理想信息产业(集团)有限公司，上海电信下属公司”），把两家公司都放进 orgs（o1/o2），并用 relations 建立 org→org 的 subsidiary_of 关系，再建立 person→org 的 works_at 关系（同时在 person.profile.organization 填主公司全称）。
   - 已有联系人需要补充公司/职位/地址时用 action="update"（填 person_id 与增量字段，平台会自动保留其原有手机/地址等资料）。
   - 提到多个项目（如壁仞项目、tokenlink平台项目、教委项目）逐条放进 matters（m1/m2/m3），并建立 person→matter（matter_role）及 org→matter（org_matter_role）关系。
   - 会议记录里的待办行动项、下次会议日程、卡点跟进提醒分别写入 todos / events / reminders，并尽量通过 matter_ref 挂到对应项目。
3. 公司与地址补全：禁止把公司简写为孤立的“电信/移动/腾讯”，须结合上下文补全规范名称（如“中国电信股份有限公司上海分公司”，aliases 填 ["上海电信"]）；地址补全已知省市区与楼栋房号写入对应公司 address 或人物 extra.address，不确定的精确门牌号不编造。
4. 省略未提供的可选字段（如 birth 未提到则填 null 或不填）；界面下方会展示可视化的「原始记录拆解确认卡」，文字回复只需 1–2 句简述拆解出的实体与分支并请用户核对确认，不重复长列表，不说已保存。"""

PEOPLE_ENTRIES = [
    {
        "package": PEOPLE_PACKAGE,
        "version": "1",
        "entry": "record",
        "description": "玄圃内置个人人物与关系",
        "entry_description": "登记或更新本人联系人名片、人际关系与事项参与人（生成待确认草稿）",
    },
    {
        "package": PEOPLE_PACKAGE,
        "version": "1",
        "entry": "manage",
        "description": "玄圃内置个人人物与关系",
        "entry_description": "打开本人人物与关系管理列表",
    },
]


async def hydrate_bundle_updates(
    platform: Platform, who: Principal, raw: dict[str, Any]
) -> dict[str, Any]:
    """校验前为增量 update/link 的已有公司、人物、事项补齐缺失的必填 name/title 与版本。"""
    data = dict(raw)
    if isinstance(data.get("orgs"), list):
        orgs: list[Any] = []
        for item in data["orgs"]:
            if (
                isinstance(item, dict)
                and item.get("action") in ("update", "link")
                and isinstance(item.get("org_id"), str)
                and item["org_id"].strip()
            ):
                prof = item.get("profile")
                if not isinstance(prof, dict) or not str(prof.get("name") or "").strip():
                    existing_org = (await platform.people.get_org(who, item["org_id"].strip()))["org"]
                    merged_prof = dict(prof) if isinstance(prof, dict) else {}
                    merged_prof["name"] = existing_org["profile"]["name"]
                    item = {**item, "profile": merged_prof}
                    if item.get("expected_version") is None:
                        item["expected_version"] = existing_org["version"]
            orgs.append(item)
        data["orgs"] = orgs
    if isinstance(data.get("persons"), list):
        persons: list[Any] = []
        for item in data["persons"]:
            if (
                isinstance(item, dict)
                and item.get("action") == "update"
                and isinstance(item.get("person_id"), str)
                and item["person_id"].strip()
            ):
                prof = item.get("profile")
                if not isinstance(prof, dict) or not str(prof.get("name") or "").strip():
                    existing_p = (await platform.people.get_person(who, item["person_id"].strip()))["person"]
                    merged_prof = dict(prof) if isinstance(prof, dict) else {}
                    merged_prof["name"] = existing_p["profile"]["name"]
                    item = {**item, "profile": merged_prof}
                    if item.get("expected_version") is None:
                        item["expected_version"] = existing_p["version"]
            persons.append(item)
        data["persons"] = persons
    if isinstance(data.get("matters"), list):
        matters: list[Any] = []
        for item in data["matters"]:
            if (
                isinstance(item, dict)
                and item.get("action") == "link"
                and isinstance(item.get("matter_id"), str)
                and item["matter_id"].strip()
                and not str(item.get("title") or "").strip()
            ):
                existing_m = await platform.workspace.get(who, item["matter_id"].strip())
                if existing_m.get("kind") == "matter" and existing_m.get("trashed_at") is None:
                    item = {**item, "title": str(existing_m["data"].get("title") or "")}
            matters.append(item)
        data["matters"] = matters
    return data


async def resolve_people(
    platform: Platform, who: Principal, entry: str | None, draft_raw: dict[str, Any] | None
) -> RouteOutcome:
    """校验人物提案并附带重名候选提示，不执行任何写入。"""
    if not who.user_id:
        raise ValueError("人物与关系仅支持真实员工身份")
    if entry == "manage" and draft_raw is None:
        return RouteOutcome(message="已打开你的人物与关系。", open_people=True)
    if entry != "record" or draft_raw is None:
        raise ValueError("人物建议不完整")
    hydrated = await hydrate_bundle_updates(platform, who, draft_raw)
    bundle = PeopleBundleInput.model_validate(hydrated)

    preview = bundle.model_dump()
    # 为新建公司补充疑似重名/简称候选；为更新公司安全合并已有档案
    for org in preview.get("orgs", []):
        if org["action"] == "create":
            org["similar_orgs"] = await platform.people.find_org_candidates(who, org["profile"])
        elif org["action"] == "update" and org.get("org_id"):
            existing_org = (await platform.people.get_org(who, org["org_id"]))["org"]
            org["profile"] = merge_org_profile_patch(existing_org["profile"], org["profile"])
            if org.get("expected_version") is None:
                org["expected_version"] = existing_org["version"]
    # 为新建人物补充疑似重名/同联系方式候选；为更新人物安全合并已有档案以保留原联系方式与地址
    for item in preview["persons"]:
        if item["action"] == "create":
            item["similar_candidates"] = await platform.people.find_candidates(who, item["profile"])
        elif item["action"] == "update" and item.get("person_id"):
            existing = (await platform.people.get_person(who, item["person_id"]))["person"]
            item["profile"] = merge_profile_patch(existing["profile"], item["profile"])
            if item.get("expected_version") is None:
                item["expected_version"] = existing["version"]
            item["previous_organization"] = existing.get("organization") or ""
    # 为新建事项补充已有同名/相近个人事项候选，供确认卡一键切换为关联已有事项
    if preview.get("matters"):
        existing_matters = await platform.workspace.list(who, kind="matter")
        for m in preview["matters"]:
            if m["action"] != "create":
                continue
            needle = m["title"].strip().casefold()
            m["similar_matters"] = [
                {
                    "matter_id": em["id"],
                    "title": str(em["data"].get("title") or ""),
                    "status": str(em["data"].get("status") or "active"),
                }
                for em in existing_matters
                if needle
                and (
                    needle in str(em["data"].get("title") or "").casefold()
                    or str(em["data"].get("title") or "").casefold() in needle
                )
            ][:3]
    return RouteOutcome(message="请核对原始记录及拆解出的实体、项目与关系草稿，确认后保存。", people_draft=preview)
