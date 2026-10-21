"""限制树规模、禁止 DTD/实体，并仅映射明确配置的测试 profile。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from typing import Any
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import iterparse  # type: ignore[import-untyped]

from xuanpu_finance.errors import FinanceError

PROFILE = "urn:xuanpu:finance:synthetic:v1"
MONEY = re.compile(r"^-?(0|[1-9][0-9]{0,15})\.[0-9]{2}$")
RATE = re.compile(r"^(0|[1-9][0-9]{0,2})(\.[0-9]{1,6})?$")


@dataclass(frozen=True)
class ParsedInvoice:
    """携带票面、明细以及逐字段来源的解析结果。"""

    invoice: dict[str, Any]
    lines: list[dict[str, Any]]
    fields: list[dict[str, Any]]


def _tree(data: bytes) -> Element:
    """流式限制 XML 深度、元素总数和外部实体。"""
    if len(data) > 20 * 1024 * 1024:
        raise FinanceError("FILE_TOO_LARGE", 413, "单文件超过 20 MiB")
    depth = count = 0
    try:
        iterator = iterparse(
            BytesIO(data),
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        for event, _ in iterator:
            if event == "start":
                depth += 1
                count += 1
                if depth > 64 or count > 100000:
                    raise FinanceError("XML_UNSAFE", 422, "XML 深度或元素数超过限制")
            else:
                depth -= 1
        root: Element = iterator.root
        return root
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise FinanceError("XML_UNSAFE", 422, "XML 不安全或格式损坏") from exc


def _value(element: Element, name: str, *, money: bool = False, max_length: int = 2000) -> str:
    """读取唯一明确元素，禁止空值、重复值和非标准金额。"""
    found = element.findall(f"{{{PROFILE}}}{name}")
    if len(found) != 1 or list(found[0]) or not found[0].text:
        raise FinanceError("INVOICE_INVALID", 422, f"缺失或重复字段 {name}")
    value = found[0].text.strip()
    if len(value) > max_length:
        raise FinanceError("PARSER_VALUE_INVALID", 422, f"字段 {name} 超出发布契约长度")
    if not value or (money and not MONEY.fullmatch(value)):
        raise FinanceError("INVOICE_INVALID", 422, f"字段 {name} 格式错误")
    return value


def _fields(root: Element) -> list[dict[str, Any]]:
    """逐叶记录原值及可定位的合成 XML 路径。"""
    fields: list[dict[str, Any]] = []
    stack = [(root, "/Invoice")]
    while stack:
        element, path = stack.pop()
        if len(path) > 1000:
            raise FinanceError("PARSER_VALUE_INVALID", 422, "字段来源路径超过1000字符")
        if not list(element):
            raw = element.text or ""
            if len(path) > 256 or len(raw) > 2000:
                raise FinanceError("PARSER_VALUE_INVALID", 422, "字段名称或原值超出发布契约长度")
            fields.append(
                {
                    "name": path,
                    "raw_value": raw,
                    "normalized_value": raw.strip(),
                    "source_path": path,
                    "data_type": "string",
                    "mapping_state": "unverified",
                }
            )
        counts: dict[str, int] = {}
        for child in element:
            name = child.tag.rsplit("}", 1)[-1]
            counts[name] = counts.get(name, 0) + 1
            suffix = f"[{counts[name]}]" if name == "Line" else ""
            stack.append((child, f"{path}/{name}{suffix}"))
    return fields


def _lines(root: Element) -> list[dict[str, Any]]:
    """逐行读取不同税率，不以票头税率覆盖明细。"""
    lines = []
    for index, item in enumerate(root.findall(f"{{{PROFILE}}}Lines/{{{PROFILE}}}Line"), 1):
        rate = _value(item, "TaxRate")
        if not RATE.fullmatch(rate):
            raise FinanceError("INVOICE_INVALID", 422, "明细税率格式错误")
        lines.append(
            {
                "line_no": index,
                "name": _value(item, "Name", max_length=1000),
                "amount": _value(item, "Amount", money=True),
                "tax": _value(item, "TaxAmount", money=True),
                "tax_rate": rate,
                "source_path": f"/Invoice/Lines/Line[{index}]",
            }
        )
    if not lines:
        raise FinanceError("INVOICE_INVALID", 422, "合成 profile 必须有明细")
    return lines


def parse_invoice(data: bytes, *, synthetic: bool = False) -> ParsedInvoice:
    """只解析显式启用的合成 profile；生产不猜测官方电子票字段。"""
    if data.lstrip().startswith(b"%PDF"):
        raise FinanceError("PARSER_PROFILE_UNCONFIGURED", 422, "PDF 保留原件，等待可解析原件")
    root = _tree(data)
    if not synthetic or root.tag != f"{{{PROFILE}}}Invoice":
        raise FinanceError("PARSER_PROFILE_UNCONFIGURED", 422, "未配置对应的官方解析 profile")
    lines = _lines(root)
    issue_date = _value(root, "IssueDate")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", issue_date) is None:
        raise FinanceError("PARSER_VALUE_INVALID", 422, "开票日期必须为YYYY-MM-DD")
    try:
        date.fromisoformat(issue_date)
    except ValueError as exc:
        raise FinanceError("PARSER_VALUE_INVALID", 422, "开票日期格式错误") from exc
    net, tax, gross = (_value(root, name, money=True) for name in ("Amount", "TaxAmount", "Total"))
    if Decimal(net) + Decimal(tax) != Decimal(gross):
        raise FinanceError("INVOICE_INVALID", 422, "票头金额不平")
    if sum(Decimal(line["amount"]) for line in lines) != Decimal(net):
        raise FinanceError("INVOICE_INVALID", 422, "明细金额与票头不符")
    if sum(Decimal(line["tax"]) for line in lines) != Decimal(tax):
        raise FinanceError("INVOICE_INVALID", 422, "明细税额与票头不符")
    if _value(root, "Currency") != "CNY":
        raise FinanceError("INVOICE_INVALID", 422, "只支持 CNY")
    invoice: dict[str, Any] = {
        "version": 1,
        "number": _value(root, "Number", max_length=64),
        "issue_date": issue_date,
        "seller_tax_id": _value(root, "SellerTaxId", max_length=64),
        "buyer_tax_id": _value(root, "BuyerTaxId", max_length=64),
        "currency": "CNY",
        "gross": gross,
        "tax": tax,
        "net": net,
        "available_amount": "0.00",
        "line_count": len(lines),
        "is_red": Decimal(gross) < 0,
        "parser_profile": PROFILE,
        "verification_status": "not_checked",
        "ticket_status": "normal",
        "authority_status": "unverified",
        "posting_status": "not_posted",
        "tax_entry_status": "not_confirmed",
        "usage_status": "not_confirmed",
        "archive_status": "not_archived",
    }
    return ParsedInvoice(invoice, lines, _fields(root))
