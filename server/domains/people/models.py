"""个人空间「人物与关系」的严格输入模型、校验与事务快照。"""

from __future__ import annotations

import calendar
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from server.core.errors import InvalidRequestError

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
Timestamp = Annotated[float, Field(ge=0, le=253402300799, allow_inf_nan=False)]
DateText = Annotated[
    str, Field(min_length=4, max_length=10, pattern=r"^\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$")
]
ExtraScalar = str | int | float | bool
ExtraList = Annotated[list[Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]], Field(max_length=20)]
ExtraValue = ExtraScalar | ExtraList

MAX_PEOPLE = 5000
MAX_RELATIONS = 20000
MAX_NOTES = 20000
MAX_AVATAR_BYTES = 5 * 1024 * 1024
ALLOWED_AVATAR_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
SYMMETRIC_TYPES = frozenset({
    "colleague",
    "former_colleague",
    "friend",
    "partner",
    "spouse",
    "classmate",
    "acquaintance",
    "同事",
    "前同事",
    "朋友",
    "合伙人",
    "同学",
    "配偶",
    "熟人",
})


_RECORD_REF_KEYS = ("person_refs", "org_refs", "matter_refs")


def _blank_to_none(value: Any) -> Any:
    """模型在可选日期或标识字段里传入空字符串时，统一按未填（None）处理。"""
    if isinstance(value, str) and not value.strip():
        return None
    return value


class StrictModel(BaseModel):
    """严格领域模型：拒绝未知字段与隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class BirthInfo(StrictModel):
    """出生信息：支持只知年份、只知月日，或完整公历/农历日期。"""

    year: int | None = Field(default=None, ge=1900, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    calendar: Literal["solar", "lunar"] = "solar"

    @model_validator(mode="after")
    def _validate_date(self) -> BirthInfo:
        """校验出生日期完整性与公历真实天数。"""
        if self.year is None and self.month is None and self.day is None:
            raise ValueError("出生信息至少需要提供年份或月份")
        if self.day is not None and self.month is None:
            raise ValueError("提供出生日时必须同时提供出生月")
        if self.calendar == "solar" and self.month is not None and self.day is not None:
            check_year = self.year if self.year is not None else 2000
            max_day = calendar.monthrange(check_year, self.month)[1]
            if self.day > max_day:
                raise ValueError("公历日期超出该月有效天数")
        if self.calendar == "lunar" and self.day is not None and self.day > 30:
            raise ValueError("农历日期不能超过 30 日")
        return self


class ContactEntry(StrictModel):
    """单条联系方式，同一人允许保留多条。"""

    kind: Literal["phone", "wechat", "email", "feishu", "qq", "other"]
    value: Annotated[str, Field(min_length=1, max_length=255, pattern=r"\S")]
    label: Annotated[str, Field(max_length=64)] = ""
    primary: bool = False


class AvatarMeta(StrictModel):
    """头像内部元数据；object_key 仅服务端保留，不对外返回。"""

    object_key: Identifier
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    size: int = Field(ge=1, le=MAX_AVATAR_BYTES)
    sha256: Annotated[str, Field(min_length=64, max_length=64)]
    updated_at: Timestamp


class PersonProfile(StrictModel):
    """人物名片核心资料与有界扩展字段。"""

    name: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]
    aliases: list[Annotated[str, Field(min_length=1, max_length=80, pattern=r"\S")]] = Field(
        default_factory=list, max_length=20
    )
    gender: Literal["male", "female", "other", "unknown"] = "unknown"
    birth: BirthInfo | None = None
    organization: Annotated[str, Field(max_length=200)] = ""
    department: Annotated[str, Field(max_length=200)] = ""
    title: Annotated[str, Field(max_length=200)] = ""
    city: Annotated[str, Field(max_length=100)] = ""
    contacts: list[ContactEntry] = Field(default_factory=list, max_length=20)
    tags: list[Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")]] = Field(
        default_factory=list, max_length=30
    )
    summary: Annotated[str, Field(max_length=2000)] = ""
    attachment_file_ids: list[Identifier] = Field(default_factory=list, max_length=10)
    extra: dict[Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")], ExtraValue] = Field(
        default_factory=dict, max_length=30
    )

    @field_validator("extra")
    @classmethod
    def _bound_extra_strings(cls, value: dict[str, ExtraValue]) -> dict[str, ExtraValue]:
        """限制扩展字典里的字符串长度，大段正文应写入原始记录。"""
        for entry in value.values():
            if isinstance(entry, str) and len(entry) > 500:
                raise ValueError("扩展字段单个字符串不能超过 500 字")
        return value


EntityKind = Literal["person", "org", "matter"]
RecordKind = Literal["note", "meeting", "card", "call", "other"]
MAX_ORGS = 500


class OrgProfile(StrictModel):
    """纯个人视角的公司/组织轻档案（与企业后台组织架构解耦）。"""

    name: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    aliases: list[Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]] = Field(
        default_factory=list, max_length=20
    )
    org_type: Annotated[str, Field(max_length=64)] = ""
    industry: Annotated[str, Field(max_length=120)] = ""
    city: Annotated[str, Field(max_length=100)] = ""
    address: Annotated[str, Field(max_length=500)] = ""
    website: Annotated[str, Field(max_length=300)] = ""
    tags: list[Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")]] = Field(
        default_factory=list, max_length=30
    )
    summary: Annotated[str, Field(max_length=2000)] = ""
    extra: dict[Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")], ExtraValue] = Field(
        default_factory=dict, max_length=30
    )

    @field_validator("extra")
    @classmethod
    def _bound_extra_strings(cls, value: dict[str, ExtraValue]) -> dict[str, ExtraValue]:
        """限制扩展字典里的字符串长度。"""
        for entry in value.values():
            if isinstance(entry, str) and len(entry) > 500:
                raise ValueError("扩展字段单个字符串不能超过 500 字")
        return value


class RelationData(StrictModel):
    """跨实体轻量关系边：支持 person / org / matter 任意合法端点。"""

    from_kind: EntityKind = "person"
    from_person_id: Identifier
    target_kind: EntityKind
    target_id: Identifier
    relation_type: Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")]
    symmetric: bool = False
    role: Annotated[str, Field(max_length=120)] = ""
    note: Annotated[str, Field(max_length=1000)] = ""
    valid_from: DateText | None = None
    valid_to: DateText | None = None
    source_note_id: Identifier | None = None

    @field_validator("valid_from", "valid_to", "source_note_id", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)

    @model_validator(mode="before")
    @classmethod
    def _alias_from_id(cls, value: Any) -> Any:
        """兼容新字段名 from_id 与既有字段名 from_person_id。"""
        if isinstance(value, dict) and "from_id" in value and "from_person_id" not in value:
            copied = dict(value)
            copied["from_person_id"] = copied.pop("from_id")
            return copied
        return value

    @model_validator(mode="after")
    def _validate_relation(self) -> RelationData:
        """拒绝自环、事项对称边与倒置时间区间。"""
        if self.from_kind == self.target_kind and self.from_person_id == self.target_id:
            raise ValueError("不能建立实体指向自身的关系")
        if (self.from_kind == "matter" or self.target_kind == "matter") and self.symmetric:
            raise ValueError("涉及事项的关系不能标记为对称关系")
        if self.valid_from is not None and self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("关系结束时间不能早于开始时间")
        return self


class RelationPatch(StrictModel):
    """更新已有关系边的可编辑属性。"""

    relation_type: Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")]
    symmetric: bool = False
    role: Annotated[str, Field(max_length=120)] = ""
    note: Annotated[str, Field(max_length=1000)] = ""
    valid_from: DateText | None = None
    valid_to: DateText | None = None

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _validate_range(self) -> RelationPatch:
        """检查起止时间顺序。"""
        if self.valid_from is not None and self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("关系结束时间不能早于开始时间")
        return self


class NoteData(StrictModel):
    """通用原始记录（随手记、会议纪要、名片、通话），作为个人图谱的事实源头。"""

    record_kind: RecordKind = "note"
    title: Annotated[str, Field(max_length=300)] = ""
    location: Annotated[str, Field(max_length=300)] = ""
    content: Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
    occurred_at: Timestamp | None = None
    end_at: Timestamp | None = None
    summary: Annotated[str, Field(max_length=4000)] = ""
    decisions: list[Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]] = Field(
        default_factory=list, max_length=30
    )
    open_questions: list[Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]] = Field(
        default_factory=list, max_length=30
    )
    person_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    org_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    matter_id: Identifier | None = None
    matter_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    todo_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    event_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    reminder_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    attachment_file_ids: list[Identifier] = Field(default_factory=list, max_length=10)
    source_message_id: int | None = Field(default=None, ge=1)


class MergeInput(StrictModel):
    """将源人物软合并到目标人物。"""

    target_id: Identifier
    source_version: int = Field(ge=1)
    target_version: int = Field(ge=1)
    merged_profile: PersonProfile | None = None
    request_id: Identifier


class UnmergeInput(StrictModel):
    """撤销一次已执行的软合并。"""

    source_version: int = Field(ge=1)
    target_version: int = Field(ge=1)
    request_id: Identifier


class BundleOrgAction(StrictModel):
    """复合确认卡里的公司/组织新建、增量更新或关联已有。"""

    action: Literal["create", "update", "link"] = "create"
    temp_ref: Identifier | None = None
    org_id: Identifier | None = None
    expected_version: int | None = Field(default=None, ge=1)
    profile: OrgProfile

    @field_validator("temp_ref", "org_id", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _shape(self) -> BundleOrgAction:
        """新建不能指定 org_id，更新或关联必须指定 org_id。"""
        if self.action == "create" and self.org_id is not None:
            raise ValueError("新建公司不能同时指定 org_id")
        if self.action in ("update", "link") and self.org_id is None:
            raise ValueError("更新或关联已有公司必须指定 org_id")
        return self


class BundlePersonAction(StrictModel):
    """复合确认卡里的单个人物新建或增量更新。"""

    action: Literal["create", "update"] = "create"
    temp_ref: Identifier | None = None
    person_id: Identifier | None = None
    expected_version: int | None = Field(default=None, ge=1)
    profile: PersonProfile

    @field_validator("temp_ref", "person_id", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _shape(self) -> BundlePersonAction:
        """新建需要 temp_ref，更新需要明确 person_id。"""
        if self.action == "create" and self.person_id is not None:
            raise ValueError("新建人物不能同时指定 person_id")
        if self.action == "update" and self.person_id is None:
            raise ValueError("更新已有人物必须指定 person_id")
        return self


class BundleMatterAction(StrictModel):
    """复合确认卡里的个人事项（项目）新建或关联已有事项。"""

    action: Literal["create", "link"] = "create"
    temp_ref: Identifier | None = None
    matter_id: Identifier | None = None
    title: Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
    description: Annotated[str, Field(max_length=20000)] = ""

    @field_validator("temp_ref", "matter_id", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _shape(self) -> BundleMatterAction:
        """新建事项不能指定 matter_id，关联已有事项必须指定 matter_id。"""
        if self.action == "create" and self.matter_id is not None:
            raise ValueError("新建事项不能同时指定 matter_id")
        if self.action == "link" and self.matter_id is None:
            raise ValueError("关联已有事项必须指定 matter_id")
        return self


class BundleTodoAction(StrictModel):
    """复合确认卡里的行动待办项（可引用本批新建的 matter temp_ref）。"""

    title: Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
    description: Annotated[str, Field(max_length=4000)] = ""
    due_at: Timestamp | None = None
    matter_ref: Identifier | None = None

    @field_validator("matter_ref", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)


class BundleEventAction(StrictModel):
    """复合确认卡里的日程项（如后续约定会议）。"""

    title: Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
    description: Annotated[str, Field(max_length=4000)] = ""
    start_at: Timestamp
    end_at: Timestamp
    timezone: Annotated[str, Field(max_length=64)] = "Asia/Shanghai"
    matter_ref: Identifier | None = None

    @field_validator("matter_ref", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _range(self) -> BundleEventAction:
        """结束时间不能早于开始时间。"""
        if self.end_at < self.start_at:
            raise ValueError("日程结束时间不能早于开始时间")
        return self


class BundleReminderAction(StrictModel):
    """复合确认卡里的跟进提醒（如卡点催办或复盘）。"""

    title: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    body: Annotated[str, Field(max_length=2000)] = ""
    due_at: Timestamp
    timezone: Annotated[str, Field(max_length=64)] = "Asia/Shanghai"


class BundleRelationAction(StrictModel):
    """复合确认卡里的跨实体关系边，允许引用本批新建公司/人物/事项的 temp_ref。"""

    from_kind: EntityKind = "person"
    from_ref: Identifier
    target_kind: EntityKind
    target_ref: Identifier
    relation_type: Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")]
    symmetric: bool = False
    role: Annotated[str, Field(max_length=120)] = ""
    note: Annotated[str, Field(max_length=1000)] = ""
    valid_from: DateText | None = None
    valid_to: DateText | None = None

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)


class BundleNoteAction(StrictModel):
    """复合确认卡里的原始记录源头（随手记 / 会议纪要 / 名片 / 通话）。"""

    record_kind: RecordKind = "note"
    title: Annotated[str, Field(max_length=300)] = ""
    location: Annotated[str, Field(max_length=300)] = ""
    content: Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
    occurred_at: Timestamp | None = None
    end_at: Timestamp | None = None
    summary: Annotated[str, Field(max_length=4000)] = ""
    decisions: list[Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]] = Field(
        default_factory=list, max_length=30
    )
    open_questions: list[Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]] = Field(
        default_factory=list, max_length=30
    )
    person_refs: list[Identifier] = Field(default_factory=list, max_length=30)
    org_refs: list[Identifier] = Field(default_factory=list, max_length=20)
    matter_id: Identifier | None = None
    matter_refs: list[Identifier] = Field(default_factory=list, max_length=20)
    attachment_file_ids: list[Identifier] = Field(default_factory=list, max_length=10)
    source_message_id: int | None = Field(default=None, ge=1)

    @field_validator("matter_id", mode="before")
    @classmethod
    def _empty_optional_to_none(cls, value: Any) -> Any:
        """空字符串按未填处理。"""
        return _blank_to_none(value)


class PeopleBundleInput(StrictModel):
    """一次复合确认提交：原始记录 + 公司 + 人物 + 项目 + 待办/日程/提醒 + 跨实体关系。"""

    record: BundleNoteAction | None = None
    note: BundleNoteAction | None = None
    orgs: list[BundleOrgAction] = Field(default_factory=list, max_length=8)
    persons: list[BundlePersonAction] = Field(default_factory=list, max_length=10)
    matters: list[BundleMatterAction] = Field(default_factory=list, max_length=8)
    todos: list[BundleTodoAction] = Field(default_factory=list, max_length=15)
    events: list[BundleEventAction] = Field(default_factory=list, max_length=8)
    reminders: list[BundleReminderAction] = Field(default_factory=list, max_length=8)
    relations: list[BundleRelationAction] = Field(default_factory=list, max_length=30)

    @model_validator(mode="before")
    @classmethod
    def _hoist_top_level_record_refs(cls, value: Any) -> Any:
        """容错：模型把 person_refs/org_refs/matter_refs 误放到顶层时，自动归并进 record。"""
        if not isinstance(value, dict) or not any(k in value for k in _RECORD_REF_KEYS):
            return value
        copied = dict(value)
        record_key = "record" if isinstance(copied.get("record"), dict) else (
            "note" if isinstance(copied.get("note"), dict) else None
        )
        if record_key is None:
            for key in _RECORD_REF_KEYS:
                if key in copied and (copied[key] is None or copied[key] == []):
                    copied.pop(key)
            return copied
        target = dict(copied[record_key])
        for key in _RECORD_REF_KEYS:
            if key not in copied:
                continue
            extra = copied.pop(key)
            if isinstance(extra, list):
                existing = target.get(key)
                base = list(existing) if isinstance(existing, list) else []
                merged = [*base, *[item for item in extra if item not in base]]
                target[key] = merged
            elif extra is not None:
                target[key] = extra
        copied[record_key] = target
        return copied

    @model_validator(mode="after")
    def _non_empty(self) -> PeopleBundleInput:
        """统一 record/note 别名并拒绝空包。"""
        if self.record is None and self.note is not None:
            self.record = self.note
        elif self.record is not None and self.note is None:
            self.note = self.record
        if not any(
            (
                self.record,
                self.orgs,
                self.persons,
                self.matters,
                self.todos,
                self.events,
                self.reminders,
                self.relations,
            )
        ):
            raise ValueError("确认内容不能为空")
        return self


@dataclass
class PeopleState:
    """单员工个人知识图谱事务快照，内存与 PostgreSQL 共享同一结构。"""

    orgs: dict[str, dict[str, Any]] = field(default_factory=dict)
    persons: dict[str, dict[str, Any]] = field(default_factory=dict)
    relations: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)


def fingerprint(payload: Any) -> str:
    """稳定序列化请求体，用于幂等键冲突检查。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def detect_avatar_mime(data: bytes) -> Literal["image/jpeg", "image/png", "image/webp"]:
    """通过文件头魔数识别安全位图格式，拒绝 SVG 及非图像载荷。"""
    if not data:
        raise InvalidRequestError("头像内容不能为空")
    if len(data) > MAX_AVATAR_BYTES:
        raise InvalidRequestError("头像不能超过 5 MiB")
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise InvalidRequestError("头像仅支持 JPEG、PNG 或 WebP 图片")


def validate_profile(data: dict[str, Any]) -> dict[str, Any]:
    """用严格模型校验人物档案并导出规范化字典。"""
    try:
        return PersonProfile.model_validate(data).model_dump()
    except ValidationError as exc:
        raise InvalidRequestError(f"人物资料无效：{exc.errors()[0]['msg']}") from exc


def validate_org_profile(data: dict[str, Any]) -> dict[str, Any]:
    """用严格模型校验公司/组织档案并导出规范化字典。"""
    try:
        return OrgProfile.model_validate(data).model_dump()
    except ValidationError as exc:
        raise InvalidRequestError(f"公司资料无效：{exc.errors()[0]['msg']}") from exc


def public_org(record: dict[str, Any]) -> dict[str, Any]:
    """对外输出公司/组织档案，隐藏内部合并快照与回收批次。"""
    return deepcopy(
        {k: v for k, v in record.items() if k != "merge_snapshot" and not k.startswith("_")}
    )


def public_person(record: dict[str, Any]) -> dict[str, Any]:
    """对外输出人物档案，隐藏内部对象路径、合并快照与回收批次。"""
    out = deepcopy({k: v for k, v in record.items() if k not in ("merge_snapshot", "avatar") and not k.startswith("_")})
    avatar = record.get("avatar")
    out["has_avatar"] = avatar is not None
    if avatar is not None:
        out["avatar_updated_at"] = avatar["updated_at"]
    return out


def public_relation(record: dict[str, Any]) -> dict[str, Any]:
    """对外输出跨实体关系记录，同时提供 from_kind、from_id 与兼容字段 from_person_id。"""
    out = deepcopy({k: v for k, v in record.items() if not k.startswith("_")})
    out.setdefault("from_kind", "person")
    out["from_id"] = out["from_person_id"]
    return out


def public_note(record: dict[str, Any]) -> dict[str, Any]:
    """对外输出通用原始记录（兼容既有 note 字段）。"""
    out = deepcopy({k: v for k, v in record.items() if not k.startswith("_")})
    out.setdefault("record_kind", "note")
    out.setdefault("title", "")
    out.setdefault("location", "")
    out.setdefault("end_at", None)
    out.setdefault("summary", "")
    out.setdefault("decisions", [])
    out.setdefault("open_questions", [])
    out.setdefault("org_ids", [])
    out.setdefault("matter_ids", [out["matter_id"]] if out.get("matter_id") else [])
    out.setdefault("todo_ids", [])
    out.setdefault("event_ids", [])
    out.setdefault("reminder_ids", [])
    return out
