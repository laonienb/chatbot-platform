"""长期记忆：手动 CRUD、上下文注入、开关、对话后自动提取。"""

import asyncio
import json
from uuid import uuid4

from app.llm.gateway import LLMResult


async def _add_memory(client, auth_headers, persona, content):
    return await client.post(
        f"/api/v1/personas/{persona['id']}/memories", json={"content": content}, headers=auth_headers
    )


async def test_memory_crud_and_dedup(client, auth_headers, persona):
    resp = await _add_memory(client, auth_headers, persona, "用户养了只猫叫橘子")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["content"] == "用户养了只猫叫橘子"
    assert body["source"] == "manual"

    # 完全相同 → 409
    assert (await _add_memory(client, auth_headers, persona, "用户养了只猫叫橘子")).status_code == 409
    # 高度相似（新内容包含旧内容）→ 409
    assert (
        await _add_memory(client, auth_headers, persona, "用户养了只猫叫橘子，很可爱")
    ).status_code == 409

    # 列表
    resp = await client.get(f"/api/v1/personas/{persona['id']}/memories", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 编辑
    resp = await client.patch(
        f"/api/v1/memories/{body['id']}", json={"content": "用户养了只猫叫橘子，三岁"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "用户养了只猫叫橘子，三岁"

    # 删除
    assert (
        await client.delete(f"/api/v1/memories/{body['id']}", headers=auth_headers)
    ).status_code == 204
    assert (await client.get(f"/api/v1/personas/{persona['id']}/memories", headers=auth_headers)).json() == []


async def test_memory_owner_isolation(client, auth_headers, persona):
    other = await client.post(
        "/api/v1/auth/register", json={"email": "mem-thief@test.dev", "password": "password123"}
    )
    other_h = {"Authorization": f"Bearer {other.json()['access_token']}"}

    # 别人的人设看不到也加不了记忆
    assert (await client.get(f"/api/v1/personas/{persona['id']}/memories", headers=other_h)).status_code == 404
    assert (
        await client.post(
            f"/api/v1/personas/{persona['id']}/memories", json={"content": "x"}, headers=other_h
        )
    ).status_code == 404

    # 自己人设的记忆，别人改不了删不了
    created = (await _add_memory(client, auth_headers, persona, "秘密记忆")).json()
    assert (
        await client.patch(f"/api/v1/memories/{created['id']}", json={"content": "改"}, headers=other_h)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/memories/{created['id']}", headers=other_h)
    ).status_code == 404


async def test_memory_injected_into_context(client, auth_headers, persona):
    await _add_memory(client, auth_headers, persona, "用户喜欢巧克力冰淇淋")
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "随便聊点甜的"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    # mock 后端会把注入的第一条记忆回显出来
    assert "用户喜欢巧克力冰淇淋" in resp.json()["assistant_message"]["content"]


async def test_memory_disabled_no_injection(client, auth_headers, persona):
    await client.patch(
        f"/api/v1/personas/{persona['id']}", json={"memory_enabled": False}, headers=auth_headers
    )
    await _add_memory(client, auth_headers, persona, "用户喜欢巧克力冰淇淋")
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "甜的"},
        headers=auth_headers,
    )
    assert "已知:" not in resp.json()["assistant_message"]["content"]


async def test_auto_extraction_from_conversation(client, auth_headers, persona, monkeypatch):
    """对话完成后后台任务自动提取事实入库（stub LLM 返回 JSON 事实）。"""

    class StubBackend:
        async def chat(self, messages, model, **kwargs):
            return LLMResult(content='["用户是一名后端工程师"]', model=model, prompt_tokens=1, completion_tokens=1)

        async def chat_stream(self, *a, **k):
            yield ""
            yield LLMResult("", model="", prompt_tokens=0, completion_tokens=0)

    import app.services.memory as memory_service

    monkeypatch.setattr(memory_service, "get_llm_backend", lambda: StubBackend())

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "跟你说了我是做后端开发的"},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    # 后台任务异步执行，轮询等待入库
    for _ in range(30):
        mems = (
            await client.get(f"/api/v1/personas/{persona['id']}/memories", headers=auth_headers)
        ).json()
        if any("后端工程师" in m["content"] for m in mems):
            break
        await asyncio.sleep(0.1)
    assert any("后端工程师" in m["content"] for m in mems)
    assert mems[0]["source"] == "chat"


async def test_extraction_skipped_when_disabled(client, auth_headers, persona, monkeypatch):
    class StubBackend:
        async def chat(self, messages, model, **kwargs):
            return LLMResult(content='["不该被提取的记忆"]', model=model, prompt_tokens=1, completion_tokens=1)

    import app.services.memory as memory_service

    monkeypatch.setattr(memory_service, "get_llm_backend", lambda: StubBackend())
    await client.patch(
        f"/api/v1/personas/{persona['id']}", json={"memory_enabled": False}, headers=auth_headers
    )
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "hi"},
        headers=auth_headers,
    )
    await asyncio.sleep(0.5)  # 给后台任务留时间（若被错误执行）
    mems = (
        await client.get(f"/api/v1/personas/{persona['id']}/memories", headers=auth_headers)
    ).json()
    assert all("不该被提取" not in m["content"] for m in mems)
