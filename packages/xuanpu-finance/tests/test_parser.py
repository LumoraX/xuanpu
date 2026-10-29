"""验证安全解析及明确合成样本边界。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.invoices.parser import parse_invoice
from xuanpu_finance.models import FileRef, InvoiceImport

SAMPLE = b"""<Invoice xmlns="urn:xuanpu:finance:synthetic:v1"><Number>SYN-001</Number><IssueDate>2026-10-21</IssueDate><SellerName>Seller</SellerName><SellerTaxId>SELLER</SellerTaxId><BuyerName>Buyer</BuyerName><BuyerTaxId>BUYER</BuyerTaxId><Currency>CNY</Currency><Amount>100.00</Amount><TaxAmount>6.00</TaxAmount><Total>106.00</Total><Lines><Line><Name>Service</Name><Quantity>1</Quantity><UnitPrice>100.00</UnitPrice><Amount>100.00</Amount><TaxRate>0.06</TaxRate><TaxAmount>6.00</TaxAmount></Line></Lines></Invoice>"""


def test_explicit_profile_and_field_provenance() -> None:
    """合成字段保留原值路径且不自动变成官方验证。"""
    invoice = parse_invoice(SAMPLE, synthetic=True)
    assert invoice.invoice["number"] == "SYN-001"
    assert invoice.invoice["gross"] == "106.00"
    assert invoice.invoice["verification_status"] == "not_checked"
    assert invoice.lines[0]["tax_rate"] == "0.06"
    assert any(
        field["raw_value"] == "100.00" and field["source_path"] == "/Invoice/Amount"
        for field in invoice.fields
    )


@pytest.mark.parametrize(
    "payload", [SAMPLE, b"%PDF-1.4", b"<Invoice><Number>123</Number></Invoice>"]
)
def test_unconfigured_profiles_require_original(payload: bytes) -> None:
    """没有启用官方映射时不得猜字段。"""
    with pytest.raises(FinanceError, match="PARSER_PROFILE_UNCONFIGURED"):
        parse_invoice(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>',
        b"<x>" * 65 + b"</x>" * 65,
        b"<root>" + b"<x/>" * 100001 + b"</root>",
        b"<Invoice>",
    ],
)
def test_hostile_xml_is_rejected(payload: bytes) -> None:
    """DTD、外部实体、超限树和破损 XML 必须隔离。"""
    with pytest.raises(FinanceError, match="XML_UNSAFE"):
        parse_invoice(payload, synthetic=True)


def test_strict_input_and_paired_supplement() -> None:
    """严格输入禁止隐式整数转换并强制补件版本成对。"""
    with pytest.raises(ValidationError):
        FileRef(file_id="x", revision="1", sha256="0" * 64)
    with pytest.raises(ValidationError):
        InvoiceImport(
            legal_entity_id="c",
            files=[FileRef(file_id="x", revision=1, sha256="0" * 64)],
            source_exception_id="e",
        )


def test_mixed_tax_rates_keep_line_sources() -> None:
    """混合税率按明细保留，不推导一个票头税率。"""
    data = SAMPLE.replace(
        b"<Amount>100.00</Amount><TaxRate>0.06</TaxRate><TaxAmount>6.00</TaxAmount>",
        b"<Amount>50.00</Amount><TaxRate>0.03</TaxRate><TaxAmount>1.50</TaxAmount>",
    ).replace(
        b"</Line></Lines>",
        b"</Line><Line><Name>Second</Name><Amount>50.00</Amount><TaxRate>0.09</TaxRate><TaxAmount>4.50</TaxAmount></Line></Lines>",
    )
    parsed = parse_invoice(data, synthetic=True)
    assert [line["tax_rate"] for line in parsed.lines] == ["0.03", "0.09"]
    assert parsed.lines[1]["source_path"] == "/Invoice/Lines/Line[2]"


@pytest.mark.parametrize("value", [b"NaN", b"Infinity", b"1e2", b"100.001", b"+100.00"])
def test_money_has_no_nonfinite_or_coerced_representations(value: bytes) -> None:
    """金额必须是精确十进制文本，禁止浮点及指数解释。"""
    with pytest.raises(FinanceError, match="INVOICE_INVALID"):
        parse_invoice(
            SAMPLE.replace(b"<Amount>100.00</Amount>", b"<Amount>" + value + b"</Amount>"),
            synthetic=True,
        )


@pytest.mark.parametrize(
    "tag,oversize", [(b"Name", 1001), (b"Number", 65), (b"SellerTaxId", 65), (b"BuyerTaxId", 65)]
)
def test_published_field_length_limits_reject_without_truncation(tag: bytes, oversize: int) -> None:
    """票号、税号和行名超出发布契约长度时拒绝，不截断存库。"""
    import re

    data = re.sub(
        b"<" + tag + b">[^<]*</" + tag + b">",
        b"<" + tag + b">" + b"X" * oversize + b"</" + tag + b">",
        SAMPLE,
    )
    with pytest.raises(FinanceError, match="PARSER_VALUE_INVALID"):
        parse_invoice(data, synthetic=True)


@pytest.mark.parametrize(
    "extension",
    [
        b"<" + b"X" * 300 + b">value</" + b"X" * 300 + b">",
        (b"<abcdefghijklmnopq>" * 60) + b"<Value>x</Value>" + (b"</abcdefghijklmnopq>" * 60),
    ],
)
def test_extension_field_name_and_source_path_must_fit_contract(extension: bytes) -> None:
    """扩展字段名称超过256或来源路径超过1000时禁止溢出输出契约。"""
    with pytest.raises(FinanceError, match="PARSER_VALUE_INVALID"):
        parse_invoice(SAMPLE.replace(b"</Invoice>", extension + b"</Invoice>"), synthetic=True)


@pytest.mark.parametrize(
    "tag,size", [(b"Name", 1000), (b"Number", 64), (b"SellerTaxId", 64), (b"BuyerTaxId", 64)]
)
def test_published_field_length_boundary_is_accepted(tag: bytes, size: int) -> None:
    """发布上限本身可解析，证明长度门禁没有提前截断或偏移。"""
    import re

    data = re.sub(
        b"<" + tag + b">[^<]*</" + tag + b">",
        b"<" + tag + b">" + b"X" * size + b"</" + tag + b">",
        SAMPLE,
    )
    parsed = parse_invoice(data, synthetic=True)
    assert any(field["raw_value"] == "X" * size for field in parsed.fields)


@pytest.mark.parametrize("size,valid", [(2000, True), (2001, False)])
def test_extension_raw_value_limit_is_preserved(size: int, valid: bool) -> None:
    """扩展字段原值最多2000字符，禁止截断后伪装成功。"""
    data = SAMPLE.replace(b"</Invoice>", b"<Extra>" + b"X" * size + b"</Extra></Invoice>")
    if valid:
        assert any(
            field["raw_value"] == "X" * size for field in parse_invoice(data, synthetic=True).fields
        )
    else:
        with pytest.raises(FinanceError, match="PARSER_VALUE_INVALID"):
            parse_invoice(data, synthetic=True)


@pytest.mark.parametrize("alias", [b"20261021", b"2026-W43-3"])
def test_issue_date_requires_published_calendar_format(alias: bytes) -> None:
    """日期别名不符合发布date格式，必须拒绝以免绕过主票唯一键。"""
    with pytest.raises(FinanceError, match="PARSER_VALUE_INVALID"):
        parse_invoice(SAMPLE.replace(b"2026-10-21", alias), synthetic=True)
