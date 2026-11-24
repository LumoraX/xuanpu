"""人物与关系的内置提案约定：由分流与管家共用，写入始终经员工本人确认。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from server.domains.people.models import PeopleBundleInput
from server.engine.assistant.reminders import RouteOutcome

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.platform import Platform

PEOPLE_PACKAGE = "@people"
PEOPLE_PROMPT = """个人人物与关系是平台内置个人空间能力（仅本人可见），专门用于记录联系人名片、生日、联系方式、地址备注、人际关系与项目参与人。
不要把「记录联系人/名片/人际关系」推脱为“无法创建联系人”或改建成普通事项。
查看或管理本人人物档案选择 @people/manage（不带 people_draft）。
登记名片、记录联系人、更新人物资料、记录人物关系或项目参与人时（有 read_personal 工具时可先用 kind="people_search" 查重），
选择 package="@people"、entry="record"、message=null，并填写 people_draft 字段：
{"persons":[{"action":"create"或"update","temp_ref":"p1","person_id":更新时必填,"expected_version":更新时可选,
"profile":{"name":姓名,"aliases":[别名],"gender":"male|female|other|unknown",
"birth":{"year":年,"month":月,"day":日,"calendar":"solar|lunar"},
"organization":规范公司全称,"department":部门,"title":职位,"city":城市,
"contacts":[{"kind":"phone|wechat|email|feishu|qq|other","value":号码,"label":标签,"primary":true|false}],
"tags":[标签],"summary":简述,"attachment_file_ids":[个人文件ID],"extra":{"address":完整详细地址, ...}}}],
"relations":[{"from_ref":person_id或temp_ref,"target_kind":"person|matter","target_ref":目标ID或temp_ref,
"relation_type":关系词,"symmetric":true|false,"role":角色,"note":备注,"valid_from":YYYY-MM,"valid_to":YYYY-MM}],
"note":{"content":原始摘录,"person_refs":[person_id或temp_ref],"matter_id":可选事项ID,"attachment_file_ids":[个人文件ID]}}。
字段规范要求：
1. 公司名称必须规范补全：不能只写“电信”“移动”“联通”“阿里”“腾讯”等孤立简称；须结合城市/园区/上下文补全为规范企业或分支名称（例如“电信”+“上海电信信息园”应补全为“中国电信股份有限公司上海分公司（上海电信）”）。
2. 地址必须完整补全：将上下文里分散的城市、园区、楼栋、楼层合并写入 extra.address，并补全已知明确的省市/行政区划（例如“上海电信信息园B4栋3楼”补全为“上海市浦东新区上海电信信息园B4栋3楼”，同时 city 填“上海”）；不确定的精确门牌号不要编造，并在回复中提示用户在名片卡片核对。
3. 省略未提供的可选字段（如 birth 未提到则填 null 或不填，不要填全空对象）；界面下方会直接展示可视化可编辑联系人名片卡，文字回复只需 1–2 句简短说明补全内容并请用户确认，不要重复长列表，不说已保存。"""

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
    try:
        bundle = PeopleBundleInput.model_validate(draft_raw)
    except ValidationError as exc:
        raise ValueError(f"人物草稿无效：{exc.errors()[0]['msg']}") from exc

    preview = bundle.model_dump()
    # 为新建人物补充疑似重名/同联系方式候选，供确认卡一键切换为更新
    for item in preview["persons"]:
        if item["action"] == "create":
            item["similar_candidates"] = await platform.people.find_candidates(who, item["profile"])
    return RouteOutcome(message="请核对人物名片与关系草稿，确认后保存。", people_draft=preview)
