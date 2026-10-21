"""HTTP只受理真实持久任务，并保护内容、CSRF和请求幂等。"""

from __future__ import annotations


async def test_accept_replay_privacy_and_cancel(task_api):
    """双击得到同一任务；他人无法读状态、进度、取消或决定。"""
    api = task_api
    a, b = api.users["usr_a"], api.users["usr_b"]
    uploaded = await api.client.post("/v1/workspace/files", params={"name": "m.txt", "request_id": "f"},
        content=b"PRIVATE original minutes", headers={**a, "Content-Type": "text/plain"})
    assert uploaded.status_code == 200, uploaded.text
    request = {"request_id": "taskreq", "text": "整理纪要PRIVATE", "input_files": [
        {"item_id": uploaded.json()["id"], "revision": 1}]}
    created = await api.client.post("/v1/work-tasks", json=request, headers=a)
    assert created.status_code == 202, created.text
    task = created.json()
    assert task["status"] == "accepted"
    same = await api.client.post("/v1/work-tasks", json=request, headers=a)
    assert same.status_code == 202 and same.json()["task_id"] == task["task_id"]
    changed = await api.client.post("/v1/work-tasks", json={**request, "text": "another"}, headers=a)
    assert changed.status_code == 409
    prefix = "/v1/work-tasks/" + task["task_id"]
    assert (await api.client.get("/v1/work-tasks", headers=b)).json()["items"] == []
    for path in (prefix, prefix + "/events"):
        response = await api.client.get(path, headers=b)
        assert response.status_code == 404 and "PRIVATE" not in response.text
    assert (await api.client.post(prefix + "/cancel", headers=b)).status_code == 404
    assert (await api.client.get(prefix + "/events", headers=a)).json()["items"][0]["status"] == "accepted"
    cancelled = await api.client.post(prefix + "/cancel", headers=a)
    assert cancelled.status_code == 202, cancelled.text
    assert (await api.client.get(prefix, headers=a)).json()["status"] == "cancelled"
    assert (await api.client.post(prefix + "/cancel", headers=a)).status_code == 202


async def test_input_ownership_and_strict_request(task_api):
    """输入不能跨人引用或指定服务端身份，写入口必须通过CSRF。"""
    api = task_api
    a, b = api.users["usr_a"], api.users["usr_b"]
    file = (await api.client.post("/v1/workspace/files", params={"name": "m.txt", "request_id": "f"},
        content=b"hello", headers={**a, "Content-Type": "text/plain"})).json()
    request = {"request_id": "taskreq", "text": "整理", "input_files": [{"item_id": file["id"], "revision": 1}]}
    assert (await api.client.post("/v1/work-tasks", json=request, headers=b)).status_code == 404
    assert (await api.client.post("/v1/work-tasks", json={**request, "user_id": "usr_b"}, headers=a)).status_code == 400
    assert (await api.client.post("/v1/work-tasks", json=request, headers={"Cookie": a["Cookie"]})).status_code == 403
    assert (await api.client.post("/v1/work-tasks", json=request,
        headers={"Authorization": "Bearer " + api.world.admin_key})).status_code == 403
