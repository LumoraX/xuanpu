"""工作站能力与对话共用真实个人数据，不让模型获得额外写权限。"""
from __future__ import annotations

import json
from typing import Any

from xuanpu_server.errors import RuntimeUnavailableError


async def test_personal_draft_history_and_settings(identity_world: Any, step: Any) -> None:
    """自然语言生成待确认草稿；个性指令进入模型，刷新后历史仍可读取。"""
    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        settings = await ident.http.put("/v1/workspace/settings", headers=headers, json={
            "assistant_name": "小玄", "user_title": "陈老师", "style": "先给结论", "timezone": "Asia/Shanghai", "version": 1,
        })
        assert settings.status_code == 200, settings.text
        skill = await ident.http.post("/v1/workspace/items", headers=headers, json={
            "kind": "skill", "request_id": "skill-one", "data": {
                "title": "周报", "instructions": "周报按进展、风险、下周安排整理", "enabled": True,
            },
        })
        assert skill.status_code in {200, 201}, skill.text
        proposal = {"package": "@workspace", "entry": "create", "message": None,
                    "workspace_draft": {"kind": "todo", "data": {"title": "提交周报"}}}
        ident.world.models.script(step(text=json.dumps(proposal)))
        response = await ident.http.post("/v1/assistant/route", headers=headers, json={"text": "记一下提交周报", "request_id": "chat-one"})
        assert response.status_code == 200, response.text
        assert response.json()["workspace_draft"]["data"]["title"] == "提交周报"
        records = (await ident.http.get("/v1/workspace/items?kind=todo", headers=headers)).json()["items"]
        assert records == []
        prompt = ident.world.models.last_prompt()
        assert "先给结论" in prompt and "周报按进展" in prompt
        history = (await ident.http.get("/v1/workspace/messages", headers=headers)).json()["items"]
        assert [x["role"] for x in history] == ["user", "assistant"]
        assert history[0]["text"] == "记一下提交周报"


async def test_attachment_content_and_owner_checked_before_sampling(identity_world: Any, step: Any) -> None:
    """附件正文供本人模型读取；另一个员工即使知道文件标识也不能读。"""
    async with identity_world() as ident:
        alice = await ident.user(name="alice")
        headers = {**ident.cookie(await ident.login(alice)), "X-Requested-By": "xuanpu"}
        upload = await ident.http.post("/v1/workspace/files?name=notes.txt&request_id=file-one", content="本周完成订单二十单".encode(), headers={**headers, "Content-Type": "text/plain"})
        assert upload.status_code in {200, 201}, upload.text
        file_id = upload.json()["id"]
        ident.world.models.script(step(text=json.dumps({"package": None, "entry": None, "message": "本周完成了二十单。"})))
        response = await ident.http.post("/v1/assistant/route", headers=headers, json={"text": "总结附件", "attachments": [file_id], "request_id": "summarize"})
        assert response.status_code == 200, response.text
        assert "本周完成订单二十单" in ident.world.models.last_prompt()
        bob = await ident.user(name="bob")
        other = {**ident.cookie(await ident.login(bob)), "X-Requested-By": "xuanpu"}
        response = await ident.http.post("/v1/assistant/route", headers=other, json={"text": "总结附件", "attachments": [file_id]})
        assert response.status_code == 404
        assert len(ident.world.models.requests) == 1


async def test_native_modules_available_without_business_package(identity_world: Any, step: Any) -> None:
    """原生模块不受企业业务包或外部平台同名能力限制。"""
    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        ident.world.models.script(step(text=json.dumps({"package": "@workspace", "entry": "file", "message": None})))
        response = await ident.http.post("/v1/assistant/route", headers=headers, json={"text": "打开我的文件"})
        assert response.status_code == 200, response.text
        assert response.json()["open_workspace"] == "file"


async def test_proposal_retry_confirm_cancel_and_owner(identity_world: Any, step: Any) -> None:
    """刷新可找回草稿；重试只保存一次，取消与跨员工确认不能改变原决定。"""
    async with identity_world() as ident:
        alice = await ident.user(name="alice")
        headers = {**ident.cookie(await ident.login(alice)), "X-Requested-By": "xuanpu"}
        choice = {"package": "@workspace", "entry": "create", "message": None,
                  "workspace_draft": {"kind": "todo", "data": {"title": "唯一待办"}}}
        ident.world.models.script(step(text=json.dumps(choice)))
        body = {"text": "记录唯一待办", "request_id": "proposal-one"}
        first = await ident.http.post("/v1/assistant/route", headers=headers, json=body)
        again = await ident.http.post("/v1/assistant/route", headers=headers, json=body)
        assert first.status_code == again.status_code == 200
        assert first.json() == again.json()
        assert len(ident.world.models.requests) == 1
        path = "/v1/assistant/proposals/proposal-one"
        assert (await ident.http.get(path, headers=headers)).json()["state"] == "pending"
        assert len((await ident.http.get("/v1/assistant/proposals", headers=headers)).json()["items"]) == 1
        bob = await ident.user(name="bob")
        other = {**ident.cookie(await ident.login(bob)), "X-Requested-By": "xuanpu"}
        assert (await ident.http.post(path + "/confirm", headers=other, json={"data": {"title": "唯一待办"}})).status_code == 404
        for _ in range(2):
            saved = await ident.http.post(path + "/confirm", headers=headers, json={"data": {"title": "唯一待办"}})
            assert saved.status_code == 200, saved.text
        assert len((await ident.http.get("/v1/workspace/items?kind=todo", headers=headers)).json()["items"]) == 1
        assert (await ident.http.get("/v1/assistant/proposals", headers=headers)).json()["items"] == []
        assert (await ident.http.post(path + "/cancel", headers=headers)).status_code == 409
        history = (await ident.http.get("/v1/workspace/messages", headers=headers)).json()["items"]
        assert history[-1]["text"] == "已保存：唯一待办"
        ident.world.models.script(step(text=json.dumps(choice)))
        await ident.http.post("/v1/assistant/route", headers=headers, json={"text": "再记一项", "request_id": "proposal-two"})
        assert (await ident.http.post("/v1/assistant/proposals/proposal-two/cancel", headers=headers)).status_code == 200
        assert (await ident.http.get("/v1/assistant/proposals", headers=headers)).json()["items"] == []


async def test_failed_proposal_can_edit_or_cancel(identity_world: Any, step: Any) -> None:
    """创建失败不冻结操作：文件夹重名后可以改名，过期提醒可以取消。"""
    import time
    from datetime import UTC, datetime

    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        await ident.http.post("/v1/workspace/items", headers=headers, json={"kind": "folder", "data": {"name": "已有目录"}, "request_id": "existing"})
        ident.world.models.script(step(text=json.dumps({"package": "@workspace", "entry": "create", "message": None, "workspace_draft": {"kind": "folder", "data": {"name": "已有目录"}}})))
        await ident.http.post("/v1/assistant/route", headers=headers, json={"text": "新建已有目录", "request_id": "same-folder"})
        path = "/v1/assistant/proposals/same-folder/confirm"
        assert (await ident.http.post(path, headers=headers, json={"data": {"name": "已有目录"}})).status_code == 409
        changed = await ident.http.post(path, headers=headers, json={"data": {"name": "新目录"}})
        assert changed.status_code == 200, changed.text
        due = time.time() + 3600
        ident.world.models.script(step(text=json.dumps({"package": "@reminders", "entry": "create", "message": None, "reminder": {"title": "过期测试", "due_at": datetime.fromtimestamp(due, UTC).isoformat(), "timezone": "UTC"}})))
        await ident.http.post("/v1/assistant/route", headers=headers, json={"text": "提醒", "request_id": "expired"})
        response = await ident.http.post("/v1/assistant/proposals/expired/confirm", headers=headers, json={"data": {"title": "过期测试", "due_at": time.time()-1, "timezone": "UTC"}})
        assert response.status_code == 400, response.text
        response = await ident.http.post("/v1/assistant/proposals/expired/cancel", headers=headers)
        assert response.status_code == 200, response.text


async def test_confirm_receipt_survives_missing_terminal_message(identity_world: Any, step: Any, monkeypatch: Any) -> None:
    """领域已提交而终态聊天失败时，刷新及原草稿重试仍返回实际确认结果。"""
    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        choice = {"package": "@workspace", "entry": "create", "message": None,
                  "workspace_draft": {"kind": "todo", "data": {"title": "原草稿"}}}
        ident.world.models.script(step(text=json.dumps(choice)))
        await ident.http.post("/v1/assistant/route", headers=headers, json={"text": "记一下", "request_id": "receipt"})
        service = ident.world.platform.workspace
        original = service.append_message

        async def fail_terminal(*args: Any, **kwargs: Any) -> Any:
            """只模拟领域已提交后的聊天存储不可用。"""
            if args[3] == "receipt:confirmed":
                raise RuntimeUnavailableError("模拟终态消息存储中断")
            return await original(*args, **kwargs)

        monkeypatch.setattr(service, "append_message", fail_terminal)
        path = "/v1/assistant/proposals/receipt"
        failed = await ident.http.post(path + "/confirm", headers=headers, json={"data": {"title": "修改后标题"}})
        assert failed.status_code == 503, failed.text
        status = (await ident.http.get(path, headers=headers)).json()
        assert status["state"] == "confirmed"
        assert status["result"]["data"]["title"] == "修改后标题"
        retry = await ident.http.post(path + "/confirm", headers=headers, json={"data": {"title": "原草稿"}})
        assert retry.status_code == 200, retry.text
        assert retry.json() == status
        assert (await ident.http.get("/v1/assistant/proposals", headers=headers)).json()["items"] == []
        assert len((await ident.http.get("/v1/workspace/items?kind=todo", headers=headers)).json()["items"]) == 1
