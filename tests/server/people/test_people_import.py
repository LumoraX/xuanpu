"""纯个人空间通讯录文件导入（vCard .vcf / Google & Outlook & 通用 CSV）测试。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from server.api import create_app
from server.core.controlplane import AuditQuery, InMemoryControlPlane
from server.core.errors import InvalidRequestError
from server.core.tenancy import Principal
from server.domains.identity import LoginSession, UserRecord
from server.domains.identity.config import IdentityConfig
from server.domains.identity.cookies import login_cookie_name, new_login_token
from server.domains.identity.service import IdentitySettings
from server.domains.people.importer import (
    MAX_IMPORT_ITEMS,
    parse_contacts_file,
)


@pytest.fixture
async def env(world_opener, local_sandboxes):
    """准备两名真实登录员工与 HTTP 测试客户端。"""
    settings = IdentitySettings.from_config("https://xuanpu.example.com", IdentityConfig())
    store = InMemoryControlPlane()
    world = await world_opener(
        store=store, sandboxes=local_sandboxes(), identity_settings=settings
    )
    await world.platform.reminders.close()
    users: dict[str, dict[str, str]] = {}
    principals: dict[str, Principal] = {}
    for user_id in ("usr_a", "usr_b"):
        await world.platform.identity.store.create_user(
            UserRecord(
                tenant_id="acme",
                user_id=user_id,
                display_name=user_id,
                created_at=time.time(),
            )
        )
        token, hashed = new_login_token()
        now = time.time()
        await world.platform.identity.store.create_login_session(
            LoginSession(
                token_hash=hashed,
                tenant_id="acme",
                user_id=user_id,
                created_at=now,
                expires_at=now + 3600,
                last_seen_at=now,
            )
        )
        users[user_id] = {"Cookie": f"{login_cookie_name(settings.cookie_secure)}={token}"}
        principals[user_id] = Principal(
            key_id=None, role="member", tenant_id="acme", user_id=user_id
        )
    app = create_app(world.platform)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://xuanpu.example.com"
    ) as client:
        try:
            yield SimpleNamespace(
                world=world, store=store, client=client, users=users, principals=principals
            )
        finally:
            await world.platform.close()


def hdr(env, user: str = "usr_a") -> dict[str, str]:
    """附带真实同源 CSRF 头的员工请求头。"""
    return {**env.users[user], "X-Requested-By": "xuanpu", "Origin": "https://xuanpu.example.com"}


def test_parse_vcard_v3_v4_apple_and_google_features() -> None:
    """解析 vCard 3.0/4.0（折行、Apple X-ABLabel、标签过滤、生日、IMPP 与忽略 PHOTO）。"""
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:王建国\r\n"
        "N:王;建国;;;\r\n"
        "NICKNAME:王总,Kevin\r\n"
        "ORG:星辰科技;供应链中心\r\n"
        "TITLE:副总裁\r\n"
        "GENDER:M\r\n"
        "TEL;TYPE=CELL,PREF:+86 138-0013-8000\r\n"
        "item1.EMAIL;TYPE=INTERNET:wang@star.example\r\n"
        "item1.X-ABLabel:_$!<Work>!$_\r\n"
        "IMPP;X-SERVICE-TYPE=WeChat:wechat:wx_wang88\r\n"
        "BDAY:1986-05-12\r\n"
        "ADR;TYPE=WORK:;;张江高科园区88号;上海;上海;201203;中国\r\n"
        "CATEGORIES:* myContacts,供应链,核心客户\r\n"
        "NOTE:第一行备注\\n第二行\r\n"
        " 续行补充\r\n"
        "URL:https://star.example\r\n"
        "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD/\r\n"
        " AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:4.0\r\n"
        "N:Doe;Jane;;;\r\n"
        "BDAY:--08-19\r\n"
        "TEL;VALUE=uri:tel:+1-415-555-0100\r\n"
        "EMAIL:jane@acme.example\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "NOTE:空名片应被跳过\r\n"
        "END:VCARD\r\n"
    ).encode()

    parsed = parse_contacts_file(vcf, filename="google.vcf")
    assert parsed.fmt == "vcf"
    assert parsed.total_rows == 3
    assert parsed.skipped_count == 1
    assert len(parsed.items) == 2

    p1 = parsed.items[0].profile
    assert p1["name"] == "王建国"
    assert p1["aliases"] == ["王总", "Kevin"]
    assert p1["organization"] == "星辰科技"
    assert p1["department"] == "供应链中心"
    assert p1["title"] == "副总裁"
    assert p1["gender"] == "male"
    assert p1["birth"] == {"year": 1986, "month": 5, "day": 12, "calendar": "solar"}
    assert p1["city"] == "上海"
    assert "张江高科园区88号" in p1["extra"]["address"]
    assert p1["extra"]["website"] == "https://star.example"
    assert p1["tags"] == ["供应链", "核心客户"]
    assert p1["summary"] == "第一行备注\n第二行续行补充"
    contacts_by_kind = {(c["kind"], c["value"]): c for c in p1["contacts"]}
    assert contacts_by_kind[("phone", "+86 138-0013-8000")]["primary"] is True
    assert contacts_by_kind[("email", "wang@star.example")]["label"] == "工作"
    assert ("wechat", "wx_wang88") in contacts_by_kind

    p2 = parsed.items[1].profile
    assert p2["name"] == "Jane Doe"
    assert p2["birth"] == {"year": None, "month": 8, "day": 19, "calendar": "solar"}
    assert p2["contacts"][0]["value"] == "+1-415-555-0100"


def test_parse_vcard_v21_quoted_printable_chinese() -> None:
    """兼容安卓/旧版导出的 vCard 2.1 QUOTED-PRINTABLE + 软换行。"""
    # "李明" UTF-8 QP = =E6=9D=8E=E6=98=8E；"华东分部" = =E5=8D=8E=E4=B8=9C=E5=88=86=E9=83=A8
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:2.1\r\n"
        "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=E6=9D=8E=\r\n"
        "=E6=98=8E\r\n"
        "ORG;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=E5=8D=8E=E4=B8=9C=E5=88=86=E9=83=A8\r\n"
        "TEL;CELL:13912345678\r\n"
        "END:VCARD\r\n"
    ).encode("ascii")

    parsed = parse_contacts_file(vcf, filename="android.vcf")
    assert len(parsed.items) == 1
    assert parsed.items[0].profile["name"] == "李明"
    assert parsed.items[0].profile["organization"] == "华东分部"
    assert parsed.items[0].profile["contacts"][0]["value"] == "13912345678"


def test_parse_google_contacts_csv_and_gb18030_chinese_csv() -> None:
    """解析 Google Contacts CSV（含 ::: 多值与系统标签过滤）与 GB18030 中文 CSV。"""
    google_csv = (
        "\ufeffFirst Name,Last Name,Nickname,Organization Name,Organization Title,"
        "Organization Department,E-mail 1 - Label,E-mail 1 - Value,Phone 1 - Label,"
        "Phone 1 - Value,Address 1 - City,Address 1 - Formatted,Birthday,Labels,Notes,"
        "Website 1 - Value\n"
        "Alice,Chen,Ali,Lumorax,CTO,AI Lab,Work,alice@lumorax.example,Mobile,"
        "138-0000-1111 ::: 138-0000-2222,北京,北京市海淀区中关村1号,1990-02-30,"
        "* myContacts ::: 投资人 ::: AI,重点跟进,https://lumorax.example\n"
    ).encode()

    parsed = parse_contacts_file(google_csv, filename="contacts.csv")
    assert parsed.fmt == "csv"
    assert len(parsed.items) == 1
    p = parsed.items[0].profile
    assert p["name"] == "Alice Chen"
    assert p["aliases"] == ["Ali"]
    assert p["organization"] == "Lumorax"
    assert p["department"] == "AI Lab"
    assert p["title"] == "CTO"
    assert p["city"] == "北京"
    assert p["birth"] is None  # 2月30日非法应给出告警而不阻断整条导入
    assert any("无效" in w for w in parsed.items[0].warnings)
    assert p["tags"] == ["投资人", "AI"]
    assert len([c for c in p["contacts"] if c["kind"] == "phone"]) == 2
    assert p["extra"]["website"] == "https://lumorax.example"

    cn_csv = (
        "姓名,公司,职位,手机,微信,邮箱,城市,标签,备注\n"
        "张伟,远景算力,架构师,13600001234,wx_zhang,zw@yj.example,深圳,客户，算力,年度大会认识\n"
    ).encode("gb18030")
    parsed_cn = parse_contacts_file(cn_csv, filename="中文通讯录.csv")
    assert len(parsed_cn.items) == 1
    p_cn = parsed_cn.items[0].profile
    assert p_cn["name"] == "张伟"
    assert p_cn["organization"] == "远景算力"
    assert p_cn["tags"] == ["客户", "算力"]
    kinds = {c["kind"] for c in p_cn["contacts"]}
    assert kinds == {"phone", "wechat", "email"}


def test_parse_rejects_binary_empty_and_over_limit() -> None:
    """空文件、二进制文件与超出单批 1000 条上限均应明确拒绝。"""
    with pytest.raises(InvalidRequestError, match="为空"):
        parse_contacts_file(b"")
    with pytest.raises(InvalidRequestError, match="仅支持"):
        parse_contacts_file(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, filename="card.png")
    with pytest.raises(InvalidRequestError, match="仅支持"):
        parse_contacts_file(b"PK\x03\x04fake-xlsx", filename="contacts.xlsx")

    many_rows = "姓名,手机\n" + "".join(
        f"联系人{i},138{i:08d}\n" for i in range(MAX_IMPORT_ITEMS + 1)
    )
    with pytest.raises(InvalidRequestError, match=str(MAX_IMPORT_ITEMS)):
        parse_contacts_file(many_rows.encode(), filename="huge.csv")


async def test_import_preview_commit_merge_idempotency_isolation_and_audit(env) -> None:
    """端到端验收：查重、文件内重复、安全合并、幂等重放、版本冲突回滚、隔离与脱敏审计。"""
    client = env.client

    # 机器 key 拒绝访问导入预览与提交
    machine = await client.post(
        "/v1/people/import/preview?filename=a.vcf",
        headers={"Authorization": "Bearer " + env.world.admin_key},
        content=b"BEGIN:VCARD\nVERSION:3.0\nFN:Test\nEND:VCARD\n",
    )
    assert machine.status_code == 403

    # 先由 usr_a 创建一个已有联系人「老王」（手机号无区号、无邮箱/公司）
    existing_resp = await client.post(
        "/v1/people",
        headers=hdr(env, "usr_a"),
        json={
            "request_id": "seed-wang",
            "profile": {
                "name": "老王",
                "contacts": [
                    {"kind": "phone", "value": "13800138000", "label": "手机", "primary": True}
                ],
                "summary": "老朋友",
            },
        },
    )
    assert existing_resp.status_code == 201
    existing = existing_resp.json()

    # 上传含 3 条记录的 Google vCard：
    # 1) 手机号 +86 138-0013-8000 -> 应命中「老王」为疑似重复
    # 2) 新联系人「陈琳」
    # 3) 文件内重复的「陈琳」 -> duplicate_in_file=True, suggested_action="skip"
    vcf_payload = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:王建国\r\n"
        "ORG:星辰科技\r\n"
        "TITLE:供应链副总裁\r\n"
        "TEL;TYPE=CELL:+86 138-0013-8000\r\n"
        "EMAIL:wang@star.example\r\n"
        "CATEGORIES:* myContacts,供应链\r\n"
        "NOTE:来自Google通讯录\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:陈琳\r\n"
        "ORG:明镜资本\r\n"
        "TEL;TYPE=CELL:13900139000\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:陈琳\r\n"
        "TEL;TYPE=CELL:139-0013-9000\r\n"
        "END:VCARD\r\n"
    ).encode()

    pv_resp = await client.post(
        "/v1/people/import/preview?filename=google-contacts.vcf",
        headers={**hdr(env, "usr_a"), "Content-Type": "text/vcard"},
        content=vcf_payload,
    )
    assert pv_resp.status_code == 200, pv_resp.text
    pv = pv_resp.json()
    assert pv["format"] == "vcf"
    assert pv["valid_count"] == 3
    assert pv["new_count"] == 1
    assert pv["duplicate_count"] == 2
    assert pv["items"][0]["status"] == "duplicate"
    assert pv["items"][0]["suggested_action"] == "merge"
    assert pv["items"][0]["candidates"][0]["person_id"] == existing["id"]
    assert pv["items"][1]["status"] == "new"
    assert pv["items"][1]["suggested_action"] == "create"
    assert pv["items"][2]["status"] == "duplicate_in_file"
    assert pv["items"][2]["duplicate_in_file"] is True
    assert pv["items"][2]["suggested_action"] == "skip"

    # 错误版本合并应整体回滚（新联系人也不会写入）
    stale_commit = await client.post(
        "/v1/people/import",
        headers=hdr(env, "usr_a"),
        json={
            "request_id": "imp-stale",
            "items": [
                {"action": "create", "profile": pv["items"][1]["profile"]},
                {
                    "action": "merge",
                    "target_person_id": existing["id"],
                    "target_version": existing["version"] + 99,
                    "profile": pv["items"][0]["profile"],
                },
            ],
        },
    )
    assert stale_commit.status_code == 409
    list_after_fail = await client.get("/v1/people", headers=hdr(env, "usr_a"))
    assert len(list_after_fail.json()["items"]) == 1

    # usr_b 不能把自己的导入合并到 usr_a 的人物上
    cross_user = await client.post(
        "/v1/people/import",
        headers=hdr(env, "usr_b"),
        json={
            "request_id": "imp-cross",
            "items": [
                {
                    "action": "merge",
                    "target_person_id": existing["id"],
                    "target_version": existing["version"],
                    "profile": pv["items"][0]["profile"],
                },
            ],
        },
    )
    assert cross_user.status_code == 404

    # 正常提交：第 1 条合并进「老王」，第 2 条新建「陈琳」，第 3 条跳过
    commit_body = {
        "request_id": "imp-batch-1",
        "items": [
            {
                "action": "merge",
                "target_person_id": existing["id"],
                "target_version": existing["version"],
                "profile": pv["items"][0]["profile"],
            },
            {"action": "create", "profile": pv["items"][1]["profile"]},
            {"action": "skip", "profile": pv["items"][2]["profile"]},
        ],
    }
    committed = await client.post("/v1/people/import", headers=hdr(env, "usr_a"), json=commit_body)
    assert committed.status_code == 201, committed.text
    res = committed.json()
    assert res["created_count"] == 1
    assert res["merged_count"] == 1
    assert res["skipped_count"] == 1

    # 合并应保留本地主姓名「老王」，把「王建国」收录为别名，并补齐公司/职位/邮箱/标签/备注
    merged_wang = res["merged"][0]
    assert merged_wang["name"] == "老王"
    assert merged_wang["version"] == existing["version"] + 1
    assert "王建国" in merged_wang["profile"]["aliases"]
    assert merged_wang["organization"] == "星辰科技"
    assert merged_wang["profile"]["title"] == "供应链副总裁"
    assert any(
        c["kind"] == "email" and c["value"] == "wang@star.example"
        for c in merged_wang["profile"]["contacts"]
    )
    assert "供应链" in merged_wang["profile"]["tags"]
    assert "老朋友" in merged_wang["profile"]["summary"]
    assert "来自Google通讯录" in merged_wang["profile"]["summary"]

    # 幂等重放：同 request_id 同体返回原结果；同 request_id 异体返回 409
    replayed = await client.post("/v1/people/import", headers=hdr(env, "usr_a"), json=commit_body)
    assert replayed.status_code == 201
    assert replayed.json() == res

    conflict_replay = await client.post(
        "/v1/people/import",
        headers=hdr(env, "usr_a"),
        json={
            "request_id": "imp-batch-1",
            "items": [{"action": "create", "profile": pv["items"][1]["profile"]}],
        },
    )
    assert conflict_replay.status_code == 409

    # usr_b 看不到 usr_a 导入的任何联系人
    b_list = await client.get("/v1/people", headers=hdr(env, "usr_b"))
    assert b_list.json()["items"] == []

    # 审计脱敏检查：只记录动作与脱敏元数据，严禁泄露姓名、手机号、邮箱
    audits = await env.store.query_audit("acme", AuditQuery())
    import_events = [ev for ev in audits if ev.kind == "people.import.committed"]
    assert len(import_events) == 1
    raw_audit = str([ev.data for ev in audits])
    for secret_pii in ("王建国", "陈琳", "13800138000", "wang@star.example"):
        assert secret_pii not in raw_audit
