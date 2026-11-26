"""通讯录文件导入引擎：支持 vCard（.vcf）与 Google / Outlook / 通用 CSV 解析、查重与增量合并。"""

from __future__ import annotations

import csv
import io
import quopri
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError, model_validator

from server.core.errors import ConflictError, InvalidRequestError
from server.domains.people.models import (
    MAX_PEOPLE,
    BirthInfo,
    ContactEntry,
    Identifier,
    PeopleState,
    PersonProfile,
    StrictModel,
    public_person,
    validate_profile,
)
from server.domains.people.rules import (
    _merge_contacts,
    _merge_unique_strings,
    append_audit,
    check_version,
    get_person,
    new_person_record,
    remember,
    replay,
    request_key,
    sync_person_profile,
)

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.domains.people.service import PeopleService

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ITEMS = 1000

ImportFormat = Literal["auto", "vcf", "csv"]
ResolvedFormat = Literal["vcf", "csv"]

_BINARY_SIGNATURES = (
    b"PK\x03\x04",  # zip / xlsx / docx
    b"%PDF-",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
    b"\x1f\x8b",  # gzip
)

_SKIPPED_VCARD_PROPS = frozenset({"PHOTO", "LOGO", "SOUND", "KEY"})
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_GOOGLE_PHONE_COL_RE = re.compile(r"^phone\s*(\d+)\s*-\s*(value|type|label)$")
_GOOGLE_EMAIL_COL_RE = re.compile(r"^e-?mail\s*(\d+)\s*-\s*(value|type|label)$")
_GOOGLE_ADDR_COL_RE = re.compile(
    r"^address\s*(\d+)\s*-\s*(formatted|street|city|region|postal code|country|extended address)$"
)
_GOOGLE_WEB_COL_RE = re.compile(r"^website\s*(\d+)\s*-\s*(value|type|label)$")
_GOOGLE_IM_COL_RE = re.compile(r"^im\s*(\d+)\s*-\s*(value|service|type|label)$")
_GOOGLE_CUSTOM_COL_RE = re.compile(r"^custom field\s*(\d+)\s*-\s*(value|label)$")

_GOOGLE_SYSTEM_LABELS = frozenset({
    "* mycontacts",
    "mycontacts",
    "my contacts",
    "* starred",
    "starred",
    "system group: my contacts",
    "system group: friends",
    "system group: family",
    "system group: coworkers",
})

_TYPE_LABEL_MAP = {
    "CELL": "手机",
    "MOBILE": "手机",
    "IPHONE": "手机",
    "WORK": "工作",
    "HOME": "家庭",
    "MAIN": "总机",
    "FAX": "传真",
    "WORK FAX": "工作传真",
    "HOME FAX": "家庭传真",
    "OTHER": "其他",
    "PERSONAL": "个人",
    "VOICE": "电话",
}


@dataclass(slots=True)
class ParsedContact:
    """单条解析后的联系人资料与非致命告警。"""

    profile: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParsedFileResult:
    """整个文件解析后的汇总结果。"""

    fmt: ResolvedFormat
    total_rows: int
    skipped_count: int
    items: list[ParsedContact]
    warnings: list[str]


def _clean_text(value: str, limit: int) -> str:
    """清理首尾空白与不可见控制字符并按上限截断。"""
    cleaned = value.replace("\x00", "").strip()
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip()
    return cleaned


def _clean_single_line(value: str, limit: int) -> str:
    """将多行字段折叠为单行并截断。"""
    collapsed = re.sub(r"\s+", " ", value.replace("\x00", "")).strip()
    if len(collapsed) > limit:
        return collapsed[:limit].rstrip()
    return collapsed


def _normalize_phone_key(raw: str) -> str:
    """归一化电话号码以便跨格式查重（去除空格/横杠/括号及常见中国区号前缀）。"""
    digits = re.sub(r"[^\d+]", "", raw.strip())
    if digits.startswith("0086") and len(digits) == 15:
        digits = digits[4:]
    elif digits.startswith("+86") and len(digits) == 14:
        digits = digits[3:]
    elif digits.startswith("86") and len(digits) == 13 and digits[2] == "1":
        digits = digits[2:]
    return digits.casefold()


def contact_match_keys(contacts: list[dict[str, Any]]) -> set[str]:
    """提取联系方式的归一化匹配键集合。"""
    keys: set[str] = set()
    for entry in contacts:
        val = str(entry.get("value") or "").strip()
        if not val:
            continue
        keys.add(val.casefold())
        if entry.get("kind") == "phone":
            norm = _normalize_phone_key(val)
            if len(norm) >= 6:
                keys.add(norm)
    return keys


def decode_import_bytes(data: bytes) -> str:
    """按常见编码顺序安全解码导入文本，拒绝空文件与二进制文件。"""
    if not data:
        raise InvalidRequestError("导入文件为空")
    if len(data) > MAX_IMPORT_BYTES:
        raise InvalidRequestError("导入文件不能超过 5 MiB")
    for sig in _BINARY_SIGNATURES:
        if data.startswith(sig):
            raise InvalidRequestError("仅支持 .vcf（vCard）或 .csv 文本格式通讯录文件")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise InvalidRequestError("UTF-16 编码解析失败，请重新导出文件") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidRequestError("UTF-8 文件包含非法字节，请检查导出文件") from exc
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise InvalidRequestError("文件编码无法识别，请使用 UTF-8 或 GB18030 编码的 .vcf / .csv 文件")


def detect_import_format(
    text: str, filename: str = "", fmt: ImportFormat = "auto"
) -> ResolvedFormat:
    """结合显式参数、文件名后缀与内容特征判定 vcf 或 csv。"""
    if fmt == "vcf":
        return "vcf"
    if fmt == "csv":
        return "csv"
    head_lines = [line.strip().upper() for line in text.splitlines()[:20] if line.strip()]
    if any(line == "BEGIN:VCARD" for line in head_lines):
        return "vcf"
    lower_name = filename.strip().lower()
    if lower_name.endswith((".vcf", ".vcard")):
        return "vcf"
    if lower_name.endswith((".csv", ".tsv")):
        return "csv"
    if len(head_lines) >= 2 and any(sep in head_lines[0] for sep in (",", "\t", ";")):
        return "csv"
    raise InvalidRequestError("无法识别文件格式，请上传 .vcf（vCard）或 .csv 文件")


def _parse_birth(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """解析常见生日格式（YYYY-MM-DD / YYYYMMDD / --MM-DD / MM/DD/YYYY / YYYY年M月D日）。"""
    text = raw.strip()
    if not text:
        return None, None
    # 去掉 ISO 时间后缀（如 1986-05-12T00:00:00Z）
    text = re.split(r"[T\s]", text, maxsplit=1)[0]
    year: int | None = None
    month: int | None = None
    day: int | None = None

    if (m := re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", text)) or (
        m := re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    ):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := re.match(r"^--(\d{2})-?(\d{2})$", text):
        month, day = int(m.group(1)), int(m.group(2))
    elif m := re.match(r"^(\d{4})[-/.](\d{1,2})$", text):
        year, month = int(m.group(1)), int(m.group(2))
    elif m := re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text):
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := re.match(r"^(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?$", text):
        year = int(m.group(1)) if m.group(1) else None
        month, day = int(m.group(2)), int(m.group(3))
    elif m := re.match(r"^(\d{4})$", text):
        year = int(m.group(1))
    else:
        return None, f"无法解析生日「{text[:32]}」，已忽略"

    # Google / Apple 常用 1604 或 0000 作为无年份占位
    if year is not None and (year < 1900 or year > 2100):
        year = None
    try:
        info = BirthInfo(year=year, month=month, day=day, calendar="solar")
    except Exception:
        return None, f"生日日期「{text[:32]}」无效，已忽略"
    return info.model_dump(), None


def _parse_gender(raw: str) -> Literal["male", "female", "other", "unknown"]:
    """规范化性别字段。"""
    val = raw.strip().split(";", 1)[0].strip().casefold()
    if val in {"m", "male", "男", "man"}:
        return "male"
    if val in {"f", "female", "女", "woman"}:
        return "female"
    if val in {"o", "other", "其他", "n", "u"} and val in {"o", "other", "其他"}:
        return "other"
    return "unknown"


def _strip_apple_label(raw: str) -> str:
    """清理 Apple X-ABLabel 的 _$!<...>!$_ 包装。"""
    cleaned = raw.strip()
    m = re.match(r"^_\$!<(.+?)>!\$_$", cleaned)
    if m:
        cleaned = m.group(1).strip()
    return _TYPE_LABEL_MAP.get(cleaned.upper(), cleaned)


def _infer_im_kind(
    service: str, value: str
) -> tuple[Literal["wechat", "qq", "feishu", "other"], str]:
    """根据 IM 协议名或前缀识别微信 / QQ / 飞书。"""
    marker = f"{service} {value}".casefold()
    cleaned_val = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", "", value).strip()
    if any(k in marker for k in ("wechat", "weixin", "微信")):
        return "wechat", cleaned_val or value.strip()
    if "qq" in marker:
        return "qq", cleaned_val or value.strip()
    if any(k in marker for k in ("feishu", "lark", "飞书")):
        return "feishu", cleaned_val or value.strip()
    return "other", cleaned_val or value.strip()


def _dedup_contacts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按类型与归一化值去重联系方式，最多保留 20 条并确保手机号有主号码。"""
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for raw in entries:
        val = _clean_single_line(str(raw.get("value") or ""), 255)
        if not val:
            continue
        kind = raw.get("kind", "other")
        norm = _normalize_phone_key(val) if kind == "phone" else val.casefold()
        key = (kind, norm or val.casefold())
        if key in seen:
            continue
        seen.add(key)
        label = _clean_single_line(str(raw.get("label") or ""), 64)
        try:
            validated = ContactEntry(
                kind=kind,
                value=val,
                label=label,
                primary=bool(raw.get("primary", False)),
            ).model_dump()
        except ValidationError:
            continue
        result.append(validated)
        if len(result) >= 20:
            break
    phones = [c for c in result if c["kind"] == "phone"]
    if phones and not any(c["primary"] for c in phones):
        phones[0]["primary"] = True
    return result


def _clean_tags(raw_tags: list[str]) -> list[str]:
    """过滤 Google 系统标签并按上限 30 条去重。"""
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw_tags:
        cleaned = _clean_single_line(item, 64)
        if not cleaned or cleaned.casefold() in _GOOGLE_SYSTEM_LABELS:
            continue
        marker = cleaned.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        tags.append(cleaned)
        if len(tags) >= 30:
            break
    return tags


def _clean_aliases(raw_aliases: list[str], primary_name: str) -> list[str]:
    """清洗并去重别名，过滤与主姓名重复项，上限 20 条。"""
    seen: set[str] = {primary_name.casefold()} if primary_name else set()
    aliases: list[str] = []
    for item in raw_aliases:
        cleaned = _clean_single_line(item, 80)
        if not cleaned:
            continue
        marker = cleaned.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        aliases.append(cleaned)
        if len(aliases) >= 20:
            break
    return aliases


def _assemble_name(family: str, given: str, middle: str = "") -> str:
    """按中文无空格、英文空格规则拼接结构化姓名。"""
    parts = [p.strip() for p in (family, middle, given) if p and p.strip()]
    if not parts:
        return ""
    joined = "".join(parts)
    if _CJK_RE.search(joined):
        return f"{family.strip()}{middle.strip()}{given.strip()}".strip()
    return " ".join(p.strip() for p in (given, middle, family) if p and p.strip())


def _finalize_profile(raw: dict[str, Any], warnings: list[str]) -> ParsedContact | None:
    """将归一化后的中间字典严格校验为 PersonProfile。"""
    name = _clean_single_line(str(raw.get("name") or ""), 120)
    contacts = _dedup_contacts(raw.get("contacts") or [])
    org = _clean_single_line(str(raw.get("organization") or ""), 200)
    if not name:
        if org:
            name = _clean_single_line(org, 120)
        elif contacts:
            name = _clean_single_line(contacts[0]["value"], 120)
        else:
            return None
    extra_clean: dict[str, str] = {}
    for k, v in (raw.get("extra") or {}).items():
        ck = _clean_single_line(str(k), 64)
        cv = _clean_text(str(v), 500)
        if ck and cv:
            extra_clean[ck] = cv
        if len(extra_clean) >= 30:
            break
    candidate: dict[str, Any] = {
        "name": name,
        "aliases": _clean_aliases(raw.get("aliases") or [], name),
        "gender": raw.get("gender") or "unknown",
        "birth": raw.get("birth"),
        "organization": org,
        "department": _clean_single_line(str(raw.get("department") or ""), 200),
        "title": _clean_single_line(str(raw.get("title") or ""), 200),
        "city": _clean_single_line(str(raw.get("city") or ""), 100),
        "contacts": contacts,
        "tags": _clean_tags(raw.get("tags") or []),
        "summary": _clean_text(str(raw.get("summary") or ""), 2000),
        "attachment_file_ids": [],
        "extra": extra_clean,
    }
    try:
        validated = validate_profile(candidate)
    except InvalidRequestError as exc:
        warnings.append(str(exc))
        return None
    return ParsedContact(profile=validated, warnings=warnings)


# ---------- vCard (.vcf) 解析 ----------


def _unfold_vcard_lines(text: str) -> list[str]:
    """按 RFC 2426/6350 与 vCard 2.1 QP 软换行规则展开折行。"""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    qp_continuing = False
    for line in raw_lines:
        if qp_continuing and unfolded:
            unfolded[-1] = unfolded[-1][:-1] + line
            qp_continuing = line.endswith("=")
            continue
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
            continue
        unfolded.append(line)
        upper = line.upper()
        header = upper.split(":", 1)[0] if ":" in upper else ""
        qp_continuing = ("QUOTED-PRINTABLE" in header) and line.endswith("=")
    return unfolded


def _split_vcard_line(line: str) -> tuple[str, str] | None:
    """在参数引号之外找到首个冒号，拆出属性头与属性值。"""
    in_quote = False
    for idx, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            return line[:idx], line[idx + 1 :]
    return None


def _split_unescaped(value: str, sep: str) -> list[str]:
    """按未转义的分隔符拆分 vCard 结构化字段（如 N / ORG / ADR）。"""
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in value:
        if escaped:
            buf.append("\\" + ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == sep:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    parts.append("".join(buf))
    return parts


def _unescape_vcard(value: str) -> str:
    """反转义 vCard 转义序列（\\n \\, \\; \\\\）。"""
    out: list[str] = []
    idx = 0
    length = len(value)
    while idx < length:
        ch = value[idx]
        if ch == "\\" and idx + 1 < length:
            nxt = value[idx + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in (",", ";", "\\", ":"):
                out.append(nxt)
            else:
                out.append(nxt)
            idx += 2
        else:
            out.append(ch)
            idx += 1
    return "".join(out)


def _decode_vcard_value(raw_value: str, params: dict[str, list[str]]) -> str:
    """解码 QUOTED-PRINTABLE（含指定 CHARSET）的 vCard 属性值。"""
    encodings = [e.upper() for e in params.get("ENCODING", [])]
    if "QUOTED-PRINTABLE" not in encodings:
        return raw_value
    charset = (params.get("CHARSET", ["utf-8"])[0] or "utf-8").strip()
    decoded_bytes = quopri.decodestring(raw_value.encode("ascii", errors="ignore"))
    for enc in (charset, "utf-8", "gb18030"):
        try:
            return decoded_bytes.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return decoded_bytes.decode("utf-8", errors="replace")


def _parse_vcard_header(header: str) -> tuple[str, str, dict[str, list[str]]]:
    """解析属性头为 (group, prop_name, params)。"""
    segments = _split_unescaped(header, ";")
    first = segments[0].strip()
    if "." in first:
        group, prop = first.split(".", 1)
    else:
        group, prop = "", first
    params: dict[str, list[str]] = {}
    for seg in segments[1:]:
        seg = seg.strip()
        if not seg:
            continue
        if "=" in seg:
            k, v = seg.split("=", 1)
            key = k.strip().upper()
            vals = [item.strip().strip('"') for item in v.split(",") if item.strip()]
            params.setdefault(key, []).extend(vals)
        else:
            upper = seg.upper()
            if upper in ("QUOTED-PRINTABLE", "BASE64", "B", "8BIT", "7BIT"):
                params.setdefault("ENCODING", []).append(upper)
            elif upper == "PREF":
                params.setdefault("PREF", []).append("1")
            else:
                params.setdefault("TYPE", []).append(seg.strip('"'))
    return group.strip().lower(), prop.strip().upper(), params


def _vcard_type_label(
    params: dict[str, list[str]], custom_label: str, default: str
) -> tuple[str, bool]:
    """从 TYPE / PREF 与 X-ABLabel 提取中文标签与是否首选标记。"""
    types = [t.upper() for t in params.get("TYPE", [])]
    is_pref = bool(params.get("PREF")) or ("PREF" in types)
    if custom_label:
        return _strip_apple_label(custom_label), is_pref
    for t in types:
        if t in ("PREF", "INTERNET", "VOICE", "MSG"):
            continue
        if t in _TYPE_LABEL_MAP:
            return _TYPE_LABEL_MAP[t], is_pref
    return default, is_pref


def _parse_single_vcard(lines: list[str], card_index: int) -> ParsedContact | None:
    """将单个 VCARD 块解析为 ParsedContact。"""
    parsed_props: list[tuple[str, str, dict[str, list[str]], str]] = []
    ab_labels: dict[str, str] = {}

    for line in lines:
        split = _split_vcard_line(line)
        if split is None:
            continue
        header, raw_val = split
        group, prop, params = _parse_vcard_header(header)
        if prop in _SKIPPED_VCARD_PROPS:
            continue
        decoded = _decode_vcard_value(raw_val, params)
        if prop == "X-ABLABEL" and group:
            ab_labels[group] = _unescape_vcard(decoded).strip()
            continue
        parsed_props.append((group, prop, params, decoded))

    fn = ""
    structured_name = ""
    aliases: list[str] = []
    org = ""
    department = ""
    title = ""
    gender: Literal["male", "female", "other", "unknown"] = "unknown"
    birth: dict[str, Any] | None = None
    city = ""
    addresses: list[str] = []
    contacts: list[dict[str, Any]] = []
    tags: list[str] = []
    notes: list[str] = []
    extra: dict[str, str] = {}
    warnings: list[str] = []

    for group, prop, params, decoded in parsed_props:
        custom_label = ab_labels.get(group, "")
        if prop == "FN":
            val = _unescape_vcard(decoded).strip()
            if val and not fn:
                fn = val
        elif prop == "N":
            comps = [_unescape_vcard(c).strip() for c in _split_unescaped(decoded, ";")]
            family = comps[0] if len(comps) > 0 else ""
            given = comps[1] if len(comps) > 1 else ""
            middle = comps[2] if len(comps) > 2 else ""
            assembled = _assemble_name(family, given, middle)
            if assembled and not structured_name:
                structured_name = assembled
        elif prop == "NICKNAME":
            for part in _split_unescaped(decoded, ","):
                cleaned = _unescape_vcard(part).strip()
                if cleaned:
                    aliases.append(cleaned)
        elif prop == "ORG":
            comps = [
                _unescape_vcard(c).strip()
                for c in _split_unescaped(decoded, ";")
                if c.strip()
            ]
            if comps and not org:
                org = comps[0]
            if len(comps) > 1 and not department:
                department = " / ".join(comps[1:])
        elif prop in ("TITLE", "ROLE"):
            val = _unescape_vcard(decoded).strip()
            if val and not title:
                title = val
        elif prop in ("GENDER", "X-GENDER"):
            if gender == "unknown":
                gender = _parse_gender(_unescape_vcard(decoded))
        elif prop == "BDAY":
            if birth is None:
                parsed_birth, warn = _parse_birth(_unescape_vcard(decoded))
                if parsed_birth is not None:
                    birth = parsed_birth
                elif warn:
                    warnings.append(f"第 {card_index} 条：{warn}")
        elif prop == "TEL":
            raw_tel = _unescape_vcard(decoded).strip()
            val = re.sub(r"^tel:", "", raw_tel, flags=re.IGNORECASE).strip()
            if val:
                label, is_pref = _vcard_type_label(params, custom_label, "电话")
                contacts.append(
                    {"kind": "phone", "value": val, "label": label, "primary": is_pref}
                )
        elif prop == "EMAIL":
            raw_mail = _unescape_vcard(decoded).strip()
            val = re.sub(r"^mailto:", "", raw_mail, flags=re.IGNORECASE).strip()
            if val:
                label, is_pref = _vcard_type_label(params, custom_label, "邮箱")
                contacts.append(
                    {"kind": "email", "value": val, "label": label, "primary": is_pref}
                )
        elif prop in ("IMPP", "X-SOCIALPROFILE", "X-WECHAT", "X-QQ", "X-FEISHU"):
            raw_im = _unescape_vcard(decoded).strip()
            if raw_im:
                service_hint = " ".join([
                    prop,
                    custom_label,
                    *params.get("X-SERVICE-TYPE", []),
                    *params.get("TYPE", []),
                ])
                kind, val = _infer_im_kind(service_hint, raw_im)
                label = _strip_apple_label(custom_label) if custom_label else (
                    {"wechat": "微信", "qq": "QQ", "feishu": "飞书", "other": "社交"}[kind]
                )
                contacts.append({"kind": kind, "value": val, "label": label, "primary": False})
        elif prop == "ADR":
            comps = [_unescape_vcard(c).strip() for c in _split_unescaped(decoded, ";")]
            # RFC: 0=po_box, 1=extended, 2=street, 3=locality(city), 4=region, 5=postal, 6=country
            locality = comps[3] if len(comps) > 3 else ""
            region = comps[4] if len(comps) > 4 else ""
            if locality and not city:
                city = locality
            elif region and not city:
                city = region
            # 按中英文习惯拼接地址
            non_empty = [c for c in comps if c]
            if non_empty:
                joined_test = "".join(non_empty)
                if _CJK_RE.search(joined_test):
                    ordered = [
                        comps[idx] for idx in (6, 4, 3, 2, 1, 0, 5)
                        if idx < len(comps) and comps[idx]
                    ]
                    addresses.append(" ".join(ordered))
                else:
                    ordered = [
                        comps[idx] for idx in (2, 1, 3, 4, 5, 6)
                        if idx < len(comps) and comps[idx]
                    ]
                    addresses.append(", ".join(ordered))
        elif prop == "CATEGORIES":
            for part in _split_unescaped(decoded, ","):
                cleaned = _unescape_vcard(part).strip()
                if cleaned:
                    tags.append(cleaned)
        elif prop == "NOTE":
            val = _unescape_vcard(decoded).strip()
            if val:
                notes.append(val)
        elif prop == "URL":
            val = _unescape_vcard(decoded).strip()
            if val and "website" not in extra:
                extra["website"] = val

    if addresses:
        extra["address"] = addresses[0]

    return _finalize_profile(
        {
            "name": fn or structured_name,
            "aliases": aliases,
            "gender": gender,
            "birth": birth,
            "organization": org,
            "department": department,
            "title": title,
            "city": city,
            "contacts": contacts,
            "tags": tags,
            "summary": "\n".join(notes),
            "extra": extra,
        },
        warnings,
    )


def parse_vcard(text: str) -> ParsedFileResult:
    """解析完整 .vcf 文件中的所有 VCARD 记录。"""
    lines = _unfold_vcard_lines(text)
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VCARD":
            current = []
        elif upper == "END:VCARD":
            if current is not None:
                blocks.append(current)
                current = None
        elif current is not None and line.strip():
            current.append(line)

    if not blocks:
        raise InvalidRequestError("未在 .vcf 文件中找到有效的联系人（BEGIN:VCARD）")
    if len(blocks) > MAX_IMPORT_ITEMS:
        raise InvalidRequestError(
            f"单次最多导入 {MAX_IMPORT_ITEMS} 位联系人，"
            f"当前文件包含 {len(blocks)} 条，请分批导入"
        )

    items: list[ParsedContact] = []
    warnings: list[str] = []
    skipped = 0
    for idx, block in enumerate(blocks, start=1):
        parsed = _parse_single_vcard(block, idx)
        if parsed is None:
            skipped += 1
            warnings.append(f"第 {idx} 条名片缺少姓名与联系方式，已跳过")
        else:
            items.append(parsed)
            warnings.extend(parsed.warnings)
    return ParsedFileResult(
        fmt="vcf",
        total_rows=len(blocks),
        skipped_count=skipped,
        items=items,
        warnings=warnings[:50],
    )


# ---------- CSV 解析（Google Contacts / Outlook / 通用中英文） ----------


def _detect_csv_delimiter(sample: str) -> str:
    """安全嗅探 CSV 分隔符（逗号 / 制表符 / 分号），默认逗号。"""
    first_line = next((ln for ln in sample.splitlines() if ln.strip()), "")
    if not first_line:
        return ","
    try:
        dialect = csv.Sniffer().sniff(first_line, delimiters=",\t;")
        if dialect.delimiter in (",", "\t", ";"):
            return dialect.delimiter
    except csv.Error:
        pass
    counts = {sep: first_line.count(sep) for sep in (",", "\t", ";")}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else ","


def _norm_header(name: str) -> str:
    """规范化 CSV 列头便于跨工具匹配。"""
    return re.sub(r"\s+", " ", name.replace("\ufeff", "").strip()).casefold()


def _split_multi_values(cell: str) -> list[str]:
    """拆分 Google Contacts 的 ' ::: ' 多值单元格。"""
    if ":::" in cell:
        return [part.strip() for part in cell.split(":::") if part.strip()]
    return [cell.strip()] if cell.strip() else []


def _parse_csv_row(headers: list[str], row: list[str], row_index: int) -> ParsedContact | None:
    """将单行 CSV 映射为 ParsedContact，兼容 Google Contacts、Outlook 与通用中文表头。"""
    cells: dict[str, str] = {}
    indexed_phones: dict[int, dict[str, str]] = {}
    indexed_emails: dict[int, dict[str, str]] = {}
    indexed_addrs: dict[int, dict[str, str]] = {}
    indexed_webs: dict[int, dict[str, str]] = {}
    indexed_ims: dict[int, dict[str, str]] = {}
    indexed_customs: dict[int, dict[str, str]] = {}

    contacts: list[dict[str, Any]] = []
    aliases: list[str] = []
    tags: list[str] = []
    notes: list[str] = []
    extra: dict[str, str] = {}
    warnings: list[str] = []

    for col_idx, header in enumerate(headers):
        if col_idx >= len(row):
            continue
        val = row[col_idx].strip()
        if not val or not header:
            continue

        if m := _GOOGLE_PHONE_COL_RE.match(header):
            slot = indexed_phones.setdefault(int(m.group(1)), {})
            slot[m.group(2)] = val
            continue
        if m := _GOOGLE_EMAIL_COL_RE.match(header):
            slot = indexed_emails.setdefault(int(m.group(1)), {})
            slot[m.group(2)] = val
            continue
        if m := _GOOGLE_ADDR_COL_RE.match(header):
            slot = indexed_addrs.setdefault(int(m.group(1)), {})
            slot[m.group(2)] = val
            continue
        if m := _GOOGLE_WEB_COL_RE.match(header):
            slot = indexed_webs.setdefault(int(m.group(1)), {})
            slot[m.group(2)] = val
            continue
        if m := _GOOGLE_IM_COL_RE.match(header):
            slot = indexed_ims.setdefault(int(m.group(1)), {})
            slot[m.group(2)] = val
            continue
        if m := _GOOGLE_CUSTOM_COL_RE.match(header):
            slot = indexed_customs.setdefault(int(m.group(1)), {})
            slot[m.group(2)] = val
            continue

        # 扁平电话列
        if header in {
            "phone", "mobile", "mobile phone", "business phone", "business phone 2",
            "home phone", "home phone 2", "work phone", "other phone", "primary phone",
            "tel", "telephone", "手机", "手机号", "手机号码", "移动电话", "电话",
            "联系电话", "办公电话", "工作电话", "家庭电话", "座机", "联系方式",
        }:
            if any(k in header for k in ("work", "business", "办公", "工作")):
                lbl = "工作"
            elif any(k in header for k in ("home", "家庭")):
                lbl = "家庭"
            else:
                lbl = "手机"
            for item in _split_multi_values(val):
                contacts.append(
                    {"kind": "phone", "value": item, "label": lbl, "primary": False}
                )
            continue

        # 扁平邮箱列
        if header in {
            "email", "e-mail", "e-mail address", "e-mail 2 address", "e-mail 3 address",
            "email address", "work email", "personal email", "home email",
            "邮箱", "电子邮箱", "电子邮件", "工作邮箱", "个人邮箱",
        }:
            lbl = "工作" if any(k in header for k in ("work", "工作")) else "邮箱"
            for item in _split_multi_values(val):
                contacts.append(
                    {"kind": "email", "value": item, "label": lbl, "primary": False}
                )
            continue

        # 微信 / QQ / 飞书列
        if header in {"wechat", "weixin", "微信", "微信号"}:
            for item in _split_multi_values(val):
                contacts.append(
                    {"kind": "wechat", "value": item, "label": "微信", "primary": False}
                )
            continue
        if header in {"qq", "qq号"}:
            for item in _split_multi_values(val):
                contacts.append(
                    {"kind": "qq", "value": item, "label": "QQ", "primary": False}
                )
            continue
        if header in {"feishu", "lark", "飞书"}:
            for item in _split_multi_values(val):
                contacts.append(
                    {"kind": "feishu", "value": item, "label": "飞书", "primary": False}
                )
            continue

        # 标签 / 分组
        if header in {
            "labels", "group membership", "categories", "tags", "tag", "标签", "分组", "分类",
        }:
            for chunk in _split_multi_values(val):
                for part in re.split(r"[,;，、]+", chunk):
                    if part.strip():
                        tags.append(part.strip())
            continue

        # 备注
        if header in {
            "notes", "note", "summary", "remark", "remarks", "memo", "备注", "说明", "简介",
        }:
            notes.append(val)
            continue

        cells.setdefault(header, val)

    def pick(*keys: str) -> str:
        for k in keys:
            if k in cells and cells[k]:
                return cells[k]
        return ""

    # 收集 Google 编号列
    for slot_id in sorted(indexed_phones):
        slot = indexed_phones[slot_id]
        raw_val = slot.get("value", "")
        raw_lbl = slot.get("label") or slot.get("type") or "手机"
        lbl = _TYPE_LABEL_MAP.get(raw_lbl.upper(), raw_lbl)
        for item in _split_multi_values(raw_val):
            contacts.append({"kind": "phone", "value": item, "label": lbl, "primary": slot_id == 1})

    for slot_id in sorted(indexed_emails):
        slot = indexed_emails[slot_id]
        raw_val = slot.get("value", "")
        raw_lbl = slot.get("label") or slot.get("type") or "邮箱"
        lbl = _TYPE_LABEL_MAP.get(raw_lbl.upper(), raw_lbl)
        for item in _split_multi_values(raw_val):
            contacts.append({"kind": "email", "value": item, "label": lbl, "primary": False})

    for slot_id in sorted(indexed_ims):
        slot = indexed_ims[slot_id]
        raw_val = slot.get("value", "")
        service = " ".join(
            filter(None, (slot.get("service"), slot.get("label"), slot.get("type")))
        )
        im_defaults = {"wechat": "微信", "qq": "QQ", "feishu": "飞书", "other": "社交"}
        for item in _split_multi_values(raw_val):
            kind, cleaned = _infer_im_kind(service, item)
            lbl = slot.get("label") or slot.get("service") or im_defaults[kind]
            contacts.append({"kind": kind, "value": cleaned, "label": lbl, "primary": False})

    city = pick("city", "business city", "home city", "城市", "常驻城市", "所在地")
    address = pick(
        "address", "business street", "home street", "地址", "联系地址", "办公地址", "公司地址"
    )
    for slot_id in sorted(indexed_addrs):
        slot = indexed_addrs[slot_id]
        if not city and slot.get("city"):
            city = slot["city"]
        if not address:
            addr_parts = (
                slot.get("country"),
                slot.get("region"),
                slot.get("city"),
                slot.get("street"),
            )
            formatted = slot.get("formatted") or " ".join(p for p in addr_parts if p)
            if formatted:
                address = formatted
    if address:
        extra["address"] = address

    website = pick("website", "web page", "url", "网址", "主页")
    if not website:
        for slot_id in sorted(indexed_webs):
            if indexed_webs[slot_id].get("value"):
                website = indexed_webs[slot_id]["value"]
                break
    if website:
        extra["website"] = website

    for slot_id in sorted(indexed_customs):
        slot = indexed_customs[slot_id]
        ck, cv = slot.get("label", "").strip(), slot.get("value", "").strip()
        if ck and cv:
            extra[ck] = cv

    # 姓名与别名
    full_name = pick("name", "full name", "display name", "姓名", "名字全称", "联系人")
    first = pick("first name", "given name", "名字", "名")
    middle = pick("middle name", "additional name")
    last = pick("last name", "family name", "姓氏", "姓")
    assembled = _assemble_name(last, first, middle)
    primary_name = full_name or assembled or pick("file as")

    nick = pick("nickname", "alias", "aliases", "昵称", "别名", "称呼")
    if nick:
        aliases.extend(p.strip() for p in re.split(r"[,;，、]+", nick) if p.strip())

    org = pick(
        "organization name", "organization 1 - name", "company", "organization",
        "公司", "单位", "组织", "工作单位",
    )
    dept = pick("organization department", "organization 1 - department", "department", "部门")
    title = pick(
        "organization title", "organization 1 - title", "job title", "title",
        "职位", "职务", "头衔",
    )
    gender = _parse_gender(pick("gender", "sex", "性别"))

    birth: dict[str, Any] | None = None
    raw_bday = pick("birthday", "birth", "bday", "生日", "出生日期")
    if raw_bday:
        parsed_birth, warn = _parse_birth(raw_bday)
        if parsed_birth is not None:
            birth = parsed_birth
        elif warn:
            warnings.append(f"第 {row_index} 行：{warn}")

    return _finalize_profile(
        {
            "name": primary_name,
            "aliases": aliases,
            "gender": gender,
            "birth": birth,
            "organization": org,
            "department": dept,
            "title": title,
            "city": city,
            "contacts": contacts,
            "tags": tags,
            "summary": "\n".join(notes),
            "extra": extra,
        },
        warnings,
    )


def parse_csv(text: str) -> ParsedFileResult:
    """解析 Google Contacts / Outlook / 通用中英文 CSV 文件。"""
    delimiter = _detect_csv_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise InvalidRequestError("CSV 文件至少需要包含表头和一条联系人数据")
    headers = [_norm_header(cell) for cell in rows[0]]
    if not any(headers):
        raise InvalidRequestError("CSV 表头为空，无法解析联系人")
    data_rows = rows[1:]
    if len(data_rows) > MAX_IMPORT_ITEMS:
        raise InvalidRequestError(
            f"单次最多导入 {MAX_IMPORT_ITEMS} 位联系人，"
            f"当前文件包含 {len(data_rows)} 行，请分批导入"
        )

    items: list[ParsedContact] = []
    warnings: list[str] = []
    skipped = 0
    for idx, row in enumerate(data_rows, start=2):
        parsed = _parse_csv_row(headers, row, idx)
        if parsed is None:
            skipped += 1
            warnings.append(f"第 {idx} 行缺少姓名与联系方式，已跳过")
        else:
            items.append(parsed)
            warnings.extend(parsed.warnings)
    return ParsedFileResult(
        fmt="csv",
        total_rows=len(data_rows),
        skipped_count=skipped,
        items=items,
        warnings=warnings[:50],
    )


def parse_contacts_file(
    data: bytes, *, filename: str = "", fmt: ImportFormat = "auto"
) -> ParsedFileResult:
    """统一入口：解码字节、识别格式并解析为标准 PersonProfile 列表。"""
    text = decode_import_bytes(data)
    resolved = detect_import_format(text, filename=filename, fmt=fmt)
    if resolved == "vcf":
        return parse_vcard(text)
    return parse_csv(text)


# ---------- 查重与安全合并 ----------


def find_import_candidates(
    state: PeopleState, profile: dict[str, Any], *, limit: int = 5
) -> list[dict[str, Any]]:
    """按姓名、别名与归一化联系方式（含手机号去横杠/去+86）查找已存在候选。"""
    names = {profile["name"].casefold(), *(a.casefold() for a in profile.get("aliases", []))}
    keys = contact_match_keys(profile.get("contacts", []))
    hits: list[dict[str, Any]] = []
    for rec in state.persons.values():
        if rec["trashed_at"] is not None or rec["merged_into_id"] is not None:
            continue
        rp = rec["profile"]
        rec_names = {rp["name"].casefold(), *(a.casefold() for a in rp.get("aliases", []))}
        rec_keys = contact_match_keys(rp.get("contacts", []))
        if (names & rec_names) or (keys and (keys & rec_keys)):
            hits.append({
                "person_id": rec["id"],
                "name": rp["name"],
                "organization": rp.get("organization", ""),
                "title": rp.get("title", ""),
                "version": rec["version"],
            })
            if len(hits) >= limit:
                break
    return hits


def merge_imported_profile(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """将导入联系人安全增量合并到已有档案：保留本地姓名与已填字段，补齐空缺并求并联系方式/别名/标签。"""
    merged: dict[str, Any] = deepcopy(existing)
    # 若导入名与本地主姓名不同，收纳进别名而不覆盖用户原有称呼
    incoming_aliases = list(incoming.get("aliases", []))
    in_name = str(incoming.get("name") or "").strip()
    ex_name = str(existing.get("name") or "").strip()
    if in_name and in_name.casefold() != ex_name.casefold():
        incoming_aliases.insert(0, in_name)
    merged["aliases"] = _merge_unique_strings(
        existing.get("aliases", []),
        [a for a in incoming_aliases if a.casefold() != ex_name.casefold()],
        20,
    )
    for field_name in ("organization", "department", "title", "city"):
        if not str(merged.get(field_name) or "").strip():
            val = str(incoming.get(field_name) or "").strip()
            if val:
                merged[field_name] = val
    if (
        merged.get("gender", "unknown") == "unknown"
        and incoming.get("gender", "unknown") != "unknown"
    ):
        merged["gender"] = incoming["gender"]
    if merged.get("birth") is None and incoming.get("birth") is not None:
        merged["birth"] = deepcopy(incoming["birth"])
    merged["contacts"] = _merge_contacts(existing.get("contacts", []), incoming.get("contacts", []))
    merged["tags"] = _merge_unique_strings(existing.get("tags", []), incoming.get("tags", []), 30)
    in_summary = str(incoming.get("summary") or "").strip()
    ex_summary = str(existing.get("summary") or "").strip()
    if in_summary:
        if not ex_summary:
            merged["summary"] = in_summary[:2000]
        elif in_summary not in ex_summary:
            merged["summary"] = f"{ex_summary}\n{in_summary}"[:2000]
    extra = dict(existing.get("extra", {}))
    for k, v in (incoming.get("extra") or {}).items():
        if k not in extra:
            extra[k] = deepcopy(v)
    merged["extra"] = dict(list(extra.items())[:30])
    return validate_profile(merged)


# ---------- 预览与提交用例 ----------


class ImportCommitItem(StrictModel):
    """单条待提交的导入联系人指令。"""

    action: Literal["create", "merge", "skip"] = "create"
    profile: PersonProfile
    target_person_id: Identifier | None = None
    target_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_target(self) -> ImportCommitItem:
        """合并动作必须显式提供目标人物与版本；其他动作不得附带目标。"""
        if self.action == "merge":
            if self.target_person_id is None or self.target_version is None:
                raise ValueError("合并已有联系人必须提供 target_person_id 与 target_version")
        elif self.target_person_id is not None or self.target_version is not None:
            raise ValueError("仅合并动作允许提供 target_person_id 与 target_version")
        return self


class ImportCommitInput(StrictModel):
    """批量导入提交请求模型。"""

    items: list[ImportCommitItem] = Field(min_length=1, max_length=MAX_IMPORT_ITEMS)


def _active_people_count(state: PeopleState) -> int:
    """统计本人当前活动人物数（不含回收站与已合并源档案）。"""
    return sum(
        1
        for rec in state.persons.values()
        if rec["trashed_at"] is None and rec.get("merged_into_id") is None
    )


async def preview_import_op(
    service: PeopleService,
    who: Principal,
    data: bytes,
    *,
    filename: str = "",
    fmt: ImportFormat = "auto",
) -> dict[str, Any]:
    """只读解析上传的 .vcf / .csv 文件并与本人现有通讯录查重，返回可确认预览清单。"""
    tenant, user = await service._workspace.require_employee(who)
    parsed = parse_contacts_file(data, filename=filename, fmt=fmt)
    state = await service._store.snapshot(tenant, user)
    remaining_quota = max(0, MAX_PEOPLE - _active_people_count(state))

    seen_names: dict[str, int] = {}
    seen_contacts: dict[str, int] = {}
    preview_items: list[dict[str, Any]] = []
    new_count = 0
    duplicate_count = 0

    for idx, entry in enumerate(parsed.items):
        profile = entry.profile
        candidates = find_import_candidates(state, profile, limit=5)
        item_warnings = list(entry.warnings)

        # 检测文件内部重复条目
        dup_in_file_idx: int | None = None
        name_key = profile["name"].casefold()
        if name_key in seen_names:
            dup_in_file_idx = seen_names[name_key]
        c_keys = contact_match_keys(profile.get("contacts", []))
        if dup_in_file_idx is None:
            for ck in c_keys:
                if ck in seen_contacts:
                    dup_in_file_idx = seen_contacts[ck]
                    break
        seen_names.setdefault(name_key, idx)
        for ck in c_keys:
            seen_contacts.setdefault(ck, idx)

        duplicate_in_file = dup_in_file_idx is not None
        if dup_in_file_idx is not None:
            item_warnings.append(f"与文件内第 {dup_in_file_idx + 1} 条联系人疑似重复")

        if candidates:
            status = "duplicate"
            suggested_action = "merge"
            duplicate_count += 1
        elif duplicate_in_file:
            status = "duplicate_in_file"
            suggested_action = "skip"
            duplicate_count += 1
        else:
            status = "new"
            suggested_action = "create"
            new_count += 1

        preview_items.append({
            "index": idx,
            "profile": profile,
            "status": status,
            "duplicate_in_file": duplicate_in_file,
            "candidates": candidates,
            "suggested_action": suggested_action,
            "warnings": item_warnings,
        })

    return {
        "format": parsed.fmt,
        "total_rows": parsed.total_rows,
        "valid_count": len(preview_items),
        "skipped_count": parsed.skipped_count,
        "new_count": new_count,
        "duplicate_count": duplicate_count,
        "remaining_quota": remaining_quota,
        "warnings": parsed.warnings,
        "items": preview_items,
    }


async def commit_import_op(
    service: PeopleService,
    who: Principal,
    raw_items: list[dict[str, Any]],
    request_id: str,
) -> dict[str, Any]:
    """幂等、原子地批量新建或增量合并已确认的导入联系人。"""
    key = request_key(request_id)
    try:
        batch = ImportCommitInput.model_validate({"items": raw_items})
    except ValidationError as exc:
        raise InvalidRequestError(f"导入提交数据无效：{exc.errors()[0]['msg']}") from exc

    tenant, user = await service._workspace.require_employee(who)
    attachment_ids: list[str] = []
    for item in batch.items:
        if item.action != "skip":
            attachment_ids.extend(item.profile.attachment_file_ids)
    if attachment_ids:
        await service._require_files(who, attachment_ids)

    payload = {"op": "import_people", "items": [i.model_dump() for i in batch.items]}

    async def guard() -> None:
        await service._workspace.require_employee(who)

    async with service._store.transaction(tenant, user, guard) as state:
        if (cached := replay(state, key, payload)) is not None:
            return cached

        create_needed = sum(1 for item in batch.items if item.action == "create")
        active_now = _active_people_count(state)
        if active_now + create_needed > MAX_PEOPLE:
            raise ConflictError(
                f"本次新建 {create_needed} 位联系人后总数将达 {active_now + create_needed}，"
                f"超过上限（{MAX_PEOPLE}）"
            )

        now = service._now()
        created: list[dict[str, Any]] = []
        merged_by_id: dict[str, dict[str, Any]] = {}
        initial_versions: dict[str, int] = {}
        skipped_count = 0

        for item in batch.items:
            if item.action == "skip":
                skipped_count += 1
                continue
            profile_dict = item.profile.model_dump()
            if item.action == "create":
                rec = new_person_record(profile_dict, now)
                state.persons[rec["id"]] = rec
                append_audit(
                    state, "people.person.created", "person", rec["id"], rec["version"], now
                )
                created.append(public_person(rec))
            else:
                assert item.target_person_id is not None and item.target_version is not None
                rec = get_person(state, item.target_person_id, active=True)
                if item.target_person_id in initial_versions:
                    if initial_versions[item.target_person_id] != item.target_version:
                        raise ConflictError("同一批次对同一目标联系人提供了不一致的版本号")
                else:
                    check_version(rec, item.target_version)
                    initial_versions[item.target_person_id] = item.target_version
                next_profile = merge_imported_profile(rec["profile"], profile_dict)
                sync_person_profile(rec, next_profile, now)
                append_audit(
                    state, "people.person.updated", "person", rec["id"], rec["version"], now
                )
                merged_by_id[rec["id"]] = public_person(rec)

        merged_list = list(merged_by_id.values())
        append_audit(
            state, "people.import.committed", "import", key, len(created) + len(merged_list), now
        )
        result = {
            "created_count": len(created),
            "merged_count": len(merged_list),
            "skipped_count": skipped_count,
            "created": created,
            "merged": merged_list,
        }
        remember(state, key, payload, result)
        return result
