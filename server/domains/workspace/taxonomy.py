"""企业文档自动业务归类与自动归档引擎（支持自定义规则、实体抽取与事项匹配）。

- 对任意体量文档（含 14MB+ 超长 Markdown/TXT/CSV/JSON 及二进制文件名）执行有界采样分类，毫秒级完成；
- 内置 10 大企业高频业务分类体系（合同法务、财务报表、会议纪要、方案标书、技术研发、人事制度、客户商机、项目运营、行业研报、综合文档）；
- 支持叠加租户/个人自定义关键词分类规则，并自动创建规范业务文件夹、匹配活跃项目事项、追加业务标签、可选同步登记个人知识库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from server.domains.workspace.document_io import inspect_document_outline, sample_large_text

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.platform import Platform


@dataclass(frozen=True)
class BusinessCategorySpec:
    """单条企业业务文档分类定义。"""

    category_id: str
    display_name: str
    folder_name: str
    default_tags: tuple[str, ...]
    title_keywords: tuple[str, ...]
    body_keywords: tuple[str, ...]


BUILTIN_CATEGORIES: tuple[BusinessCategorySpec, ...] = (
    BusinessCategorySpec(
        category_id="contract_legal",
        display_name="合同法务",
        folder_name="01-合同法务",
        default_tags=("合同法务",),
        title_keywords=("合同", "协议", "保密", "nda", "授权书", "补充协议", "法务", "条款", "违约", "诉讼", "律师函", "contract", "agreement"),
        body_keywords=("甲方", "乙方", "违约责任", "争议解决", "仲裁", "管辖法院", "不可抗力", "保密义务", "知识产权归属", "签字盖章", "生效日期"),
    ),
    BusinessCategorySpec(
        category_id="finance_report",
        display_name="财务报表",
        folder_name="02-财务报表",
        default_tags=("财务报表",),
        title_keywords=("财务", "财报", "预算", "报销", "发票", "对账", "营收", "利润", "成本", "审计", "税务", "结算", "finance", "budget", "invoice"),
        body_keywords=("资产负债表", "利润表", "现金流", "毛利率", "净利润", "增值税", "税率", "应收账款", "应付账款", "回款", "开票", "单价", "合计金额"),
    ),
    BusinessCategorySpec(
        category_id="meeting_minutes",
        display_name="会议纪要",
        folder_name="03-会议纪要",
        default_tags=("会议纪要",),
        title_keywords=("会议纪要", "纪要", "周会", "例会", "复盘会", "评审会", "晨会", "访谈记录", "minutes", "meeting"),
        body_keywords=("参会人", "主持人", "会议时间", "会议议题", "会议决议", "待办事项", "行动项", "action items", "下一步计划", "纪要整理"),
    ),
    BusinessCategorySpec(
        category_id="proposal_bid",
        display_name="方案标书",
        folder_name="04-方案标书",
        default_tags=("方案标书",),
        title_keywords=("方案", "标书", "投标", "招标", "rfp", "企划", "建议书", "报价单", "解决方案", "商业计划", "bp", "proposal"),
        body_keywords=("建设目标", "总体方案", "实施计划", "项目报价", "评分标准", "交付清单", "售后服务", "里程碑", "招投标", "商务条款"),
    ),
    BusinessCategorySpec(
        category_id="tech_arch",
        display_name="技术研发",
        folder_name="05-技术研发",
        default_tags=("技术研发",),
        title_keywords=("架构", "设计文档", "接口", "api", "prd", "需求文档", "spec", "adr", "数据库", "部署", "运维", "故障", "研发", "代码", "readme"),
        body_keywords=("接口定义", "数据模型", "时序图", "微服务", "postgresql", "kubernetes", "docker", "回滚方案", "性能指标", "单元测试", "sql"),
    ),
    BusinessCategorySpec(
        category_id="hr_policy",
        display_name="人事制度",
        folder_name="06-人事制度",
        default_tags=("人事制度",),
        title_keywords=("制度", "规范", "员工手册", "考勤", "绩效", "薪酬", "招聘", "入职", "离职", "jd", "简历", "培训", "hr", "policy"),
        body_keywords=("试用期", "绩效考核", "年假", "调休", "岗位职责", "任职要求", "薪资结构", "晋升机制", "奖惩条例", "规章制度"),
    ),
    BusinessCategorySpec(
        category_id="customer_sales",
        display_name="客户商机",
        folder_name="07-客户商机",
        default_tags=("客户商机",),
        title_keywords=("客户", "商机", "线索", "拜访", "crm", "销售", "渠道", "代理", "名片", "大客户", "跟进", "sales", "client"),
        body_keywords=("客户背景", "决策链", "采购意向", "跟进记录", "拜访纪要", "成交周期", "竞品对比", "关键联系人", "客户痛点", "商机阶段"),
    ),
    BusinessCategorySpec(
        category_id="ops_project",
        display_name="项目运营",
        folder_name="08-项目运营",
        default_tags=("项目运营",),
        title_keywords=("周报", "月报", "季报", "项目进度", "排期", "甘特", "运营", "okrs", "okr", "kpi", "里程碑", "上线清单", "roadmap"),
        body_keywords=("本周完成", "下周计划", "风险与阻塞", "进度追踪", "负责人", "截止日期", "关键结果", "留存率", "转化率", "运营数据"),
    ),
    BusinessCategorySpec(
        category_id="research_intel",
        display_name="行业研报",
        folder_name="09-行业研报",
        default_tags=("行业研报",),
        title_keywords=("研报", "调研", "行业分析", "竞品分析", "白皮书", "市场报告", "趋势", "情报", "综述", "论文", "report", "research"),
        body_keywords=("市场规模", "复合增长率", "cagr", "竞争格局", "产业链", "渗透率", "政策导向", "典型案例", "核心结论", "数据来源"),
    ),
)

_FALLBACK_CATEGORY = BusinessCategorySpec(
    category_id="general_doc",
    display_name="综合文档",
    folder_name="99-综合文档",
    default_tags=("综合文档",),
    title_keywords=(),
    body_keywords=(),
)

_DATE_RE = re.compile(r"\b(?:20\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?)\b")
_AMOUNT_RE = re.compile(r"(?:[¥￥$]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:万元|亿元|元|USD|RMB))")


class CustomTaxonomyRule(BaseModel):
    """租户或个人自定义的业务归类规则（持久化于 PluginCollectionStore）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rule_id: str = Field(min_length=1, max_length=64)
    category_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    folder_name: str = Field(min_length=1, max_length=80)
    keywords: tuple[str, ...] = Field(min_length=1, max_length=40)
    tags: tuple[str, ...] = Field(default=(), max_length=10)


class DocumentClassification(BaseModel):
    """单份文档的业务归类分析与归档建议。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    category_id: str
    category_name: str
    folder_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_tags: tuple[str, ...]
    matched_signals: tuple[str, ...] = ()
    extracted_dates: tuple[str, ...] = ()
    extracted_amounts: tuple[str, ...] = ()
    top_headings: tuple[str, ...] = ()
    matched_matter_id: str | None = None
    matched_matter_title: str | None = None
    document_stats: dict[str, Any] = Field(default_factory=dict)


def classify_document_sample(
    *,
    filename: str,
    mime_type: str,
    text: str | None = None,
    custom_rules: list[CustomTaxonomyRule] | None = None,
    active_matters: list[dict[str, Any]] | None = None,
) -> DocumentClassification:
    """基于文件名、文档骨架与有界采样正文完成快速确定性业务归类与事项匹配。"""
    name_low = filename.lower()
    sample = sample_large_text(text, max_total_chars=18_000) if text else ""
    body_low = sample.lower()
    outline = (
        inspect_document_outline(text, filename=filename, mime_type=mime_type, max_headings=30)
        if text is not None
        else {"name": filename, "mime_type": mime_type, "format": "binary"}
    )
    raw_headings = outline.get("headings")
    headings = (
        [str(h["title"]) for h in raw_headings if isinstance(h, dict) and "title" in h][:12]
        if isinstance(raw_headings, list)
        else []
    )
    headings_low = " ".join(headings).lower()

    best_spec: tuple[str, str, str, tuple[str, ...]] = (
        _FALLBACK_CATEGORY.category_id,
        _FALLBACK_CATEGORY.display_name,
        _FALLBACK_CATEGORY.folder_name,
        _FALLBACK_CATEGORY.default_tags,
    )
    best_score = 0.0
    best_signals: list[str] = []

    # 1. 自定义规则优先评估（权重更高，确保企业自定义业务分类压过通用规则）
    for rule in custom_rules or ():
        score = 0.0
        signals: list[str] = []
        for kw in rule.keywords:
            k = kw.lower()
            if k in name_low:
                score += 5.0
                signals.append(f"自定义标题:{kw}")
            elif k in headings_low:
                score += 3.5
                signals.append(f"自定义章节:{kw}")
            elif k in body_low:
                score += 2.0
                signals.append(f"自定义正文:{kw}")
        if score > best_score:
            best_score = score
            best_signals = signals[:8]
            tags = rule.tags or (rule.display_name,)
            best_spec = (rule.category_id, rule.display_name, rule.folder_name, tags)

    # 2. 内置 9 大高频业务分类打分
    for spec in BUILTIN_CATEGORIES:
        score = 0.0
        signals = []
        for kw in spec.title_keywords:
            if kw in name_low:
                score += 4.0
                signals.append(f"文件名:{kw}")
            elif kw in headings_low:
                score += 2.5
                signals.append(f"标题:{kw}")
        for kw in spec.body_keywords:
            if kw in body_low:
                score += 1.5
                signals.append(f"正文:{kw}")
        if score > best_score:
            best_score = score
            best_signals = signals[:8]
            best_spec = (spec.category_id, spec.display_name, spec.folder_name, spec.default_tags)

    # 3. 实体抽取（日期、金额）与活跃项目事项匹配
    dates = tuple(dict.fromkeys(_DATE_RE.findall(sample)))[:8]
    amounts = tuple(dict.fromkeys(_AMOUNT_RE.findall(sample)))[:8]

    matched_matter_id: str | None = None
    matched_matter_title: str | None = None
    best_matter_score = 0
    combined_text = f"{name_low}\n{headings_low}\n{body_low[:4000]}"
    for m in active_matters or ():
        title = str(m.get("data", {}).get("title") or "").strip()
        if len(title) < 2:
            continue
        t_low = title.lower()
        m_score = 0
        if t_low in name_low:
            m_score += 6
        elif t_low in combined_text:
            m_score += 4
        else:
            tokens = [tok for tok in re.split(r"[\s\-_/·、，,]+", t_low) if len(tok) >= 2]
            hits = sum(1 for tok in tokens if tok in combined_text)
            if hits >= 2:
                m_score += hits
        if m_score > best_matter_score:
            best_matter_score = m_score
            matched_matter_id = str(m["id"])
            matched_matter_title = title

    confidence = round(min(0.98, 0.35 + best_score * 0.08), 2) if best_score > 0 else 0.35
    extra_tags: list[str] = list(best_spec[3])
    if outline.get("format") == "markdown" and "Markdown" not in extra_tags:
        extra_tags.append("Markdown")
    if int(outline.get("total_bytes") or 0) >= 1_000_000:
        extra_tags.append("超长文档")
    if matched_matter_title:
        extra_tags.append(matched_matter_title[:32])

    return DocumentClassification(
        category_id=best_spec[0],
        category_name=best_spec[1],
        folder_name=best_spec[2],
        confidence=confidence,
        suggested_tags=tuple(dict.fromkeys(extra_tags))[:10],
        matched_signals=tuple(best_signals),
        extracted_dates=dates,
        extracted_amounts=amounts,
        top_headings=tuple(headings),
        matched_matter_id=matched_matter_id,
        matched_matter_title=matched_matter_title,
        document_stats={
            "format": outline.get("format"),
            "total_bytes": outline.get("total_bytes"),
            "total_lines": outline.get("total_lines"),
            "word_estimate": outline.get("word_estimate"),
            "heading_count": outline.get("heading_count"),
            "table_count": outline.get("table_count"),
        },
    )


async def auto_file_workspace_document(
    platform: Platform,
    who: Principal,
    item_id: str,
    *,
    apply: bool = True,
    index_knowledge: bool = True,
    custom_rules: list[CustomTaxonomyRule] | None = None,
    request_id: str,
) -> dict[str, Any]:
    """对工作台文件执行业务归类，并在 ``apply=True`` 时自动落盘文件夹/事项/标签与知识索引。"""
    item = await platform.workspace.get(who, item_id)
    if item["kind"] != "file" or item.get("trashed_at") is not None:
        from server.core.errors import NotFoundError

        raise NotFoundError("目标文件不存在或已回收")

    raw_bytes, mime, filename = await platform.workspace.download(who, item_id)
    text: str | None = None
    if b"\x00" not in raw_bytes:
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = None

    matters = [
        m for m in await platform.workspace.list(who, kind="matter")
        if m.get("data", {}).get("status") == "active"
    ]
    classification = classify_document_sample(
        filename=filename,
        mime_type=mime,
        text=text,
        custom_rules=custom_rules,
        active_matters=matters,
    )

    # 仅保留文件可编辑元数据用于撤回（WorkspaceService 禁止在 update 中传入 mime_type/size/current_revision/source_url）
    prev_data = {k: v for k, v in item["data"].items() if k in ("name", "parent_id", "matter_id", "tags")}
    updated_item = item
    folder_id: str | None = prev_data.get("parent_id")
    created_folder_id: str | None = None
    knowledge_indexed = False

    if apply:
        # 1. 查找或创建规范业务归类根目录文件夹
        folders = await platform.workspace.list(who, kind="folder")
        existing_folder = next(
            (
                f for f in folders
                if f["data"].get("name") == classification.folder_name and f["data"].get("parent_id") is None
            ),
            None,
        )
        if existing_folder is not None:
            folder_id = str(existing_folder["id"])
        else:
            created = await platform.workspace.create(
                who,
                "folder",
                {"name": classification.folder_name, "parent_id": None},
                f"{request_id}:fld",
            )
            folder_id = str(created["id"])
            created_folder_id = folder_id

        # 2. 合并标签并更新文件元数据（保留人工已选事项，未选时绑定自动匹配事项）
        merged_tags = list(dict.fromkeys([*(prev_data.get("tags") or ()), *classification.suggested_tags]))[:30]
        target_matter = prev_data.get("matter_id") or classification.matched_matter_id
        patch: dict[str, Any] = {
            "parent_id": folder_id,
            "tags": merged_tags,
        }
        if target_matter is not None:
            patch["matter_id"] = target_matter
        updated_item = await platform.workspace.update(who, item_id, patch, int(item["version"]))

        # 3. 租户已开启知识库且属于支持的文本格式时自动登记索引
        if (
            index_knowledge
            and text is not None
            and platform.knowledge.config.allows(who.require_tenant())
            and mime in ("text/plain", "text/markdown", "text/csv", "application/json")
        ):
            from server.domains.knowledge.service import RegisterSource

            rev = int(updated_item["data"].get("current_revision") or 1)
            try:
                await platform.knowledge.register(
                    who,
                    RegisterSource(kind="workspace_file", item_id=item_id, revision=rev),
                )
                knowledge_indexed = True
            except Exception:
                # 已经登记过同版本或其他非关键冲突时不阻断文件归类主流程
                knowledge_indexed = False

    return {
        "item_id": item_id,
        "filename": filename,
        "applied": apply,
        "folder_id": folder_id,
        "created_folder_id": created_folder_id,
        "knowledge_indexed": knowledge_indexed,
        "classification": classification.model_dump(mode="json"),
        "file": updated_item,
        "prev_data": prev_data,
    }
