"""模型注册表：下拉列表播种、管理员 CRUD、会话级模型切换。"""

from uuid import UUID

from sqlalchemy import update

from app.models import User


async def _promote(db_engine, user_id: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await session.execute(update(User).where(User.id == UUID(user_id)).values(role="admin"))
        await session.commit()


async def test_models_seeded_on_first_get(client, auth_headers):
    resp = await client.get("/api/v1/models", headers=auth_headers)
    assert resp.status_code == 200
    models = resp.json()
    assert len(models) == 3
    assert models[0]["model"] == "gpt-4o-mini"
    assert models[0]["is_default"] is True
    assert all("has_key" in m and "api_key" not in m for m in models)  # 密钥不回显


async def test_admin_models_require_admin(client, auth_headers):
    resp = await client.post(
        "/api/v1/admin/models",
        json={"name": "X", "model": "x-model"},
        headers=auth_headers,
    )
    assert resp.status_code == 403


async def test_admin_model_crud_and_default(client, auth_headers, user_tokens, db_engine):
    await _promote(db_engine, user_tokens["user"]["id"])
    await client.get("/api/v1/models", headers=auth_headers)  # 触发播种（admin 端点自身不播种）

    resp = await client.post(
        "/api/v1/admin/models",
        json={
            "name": "本地 Qwen",
            "model": "openai/qwen-72b",
            "api_base": "http://192.168.1.10:8000/v1",
            "api_key": "local-key-1",
            "sort": -1,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["has_key"] is True
    assert created["api_base"] == "http://192.168.1.10:8000/v1"

    # 设为默认 → 原默认（gpt-4o-mini）被清掉
    resp = await client.patch(
        f"/api/v1/admin/models/{created['id']}",
        json={"is_default": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    listing = (await client.get("/api/v1/models", headers=auth_headers)).json()
    defaults = [m for m in listing if m["is_default"]]
    assert len(defaults) == 1 and defaults[0]["model"] == "openai/qwen-72b"

    # 重复 model 串 → 409
    resp = await client.post(
        "/api/v1/admin/models", json={"name": "dup", "model": "openai/qwen-72b"}, headers=auth_headers
    )
    assert resp.status_code == 409

    # 删除
    resp = await client.delete(f"/api/v1/admin/models/{created['id']}", headers=auth_headers)
    assert resp.status_code == 204
    listing = (await client.get("/api/v1/models", headers=auth_headers)).json()
    assert all(m["model"] != "openai/qwen-72b" for m in listing)


async def test_conversation_model_override(client, auth_headers, persona):
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()

    resp = await client.patch(
        f"/api/v1/conversations/{conv['id']}",
        json={"model": "deepseek-chat"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "deepseek-chat"

    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "你好"},
        headers=auth_headers,
    )
    reply = sent.json()["assistant_message"]
    assert "[mock:deepseek-chat]" in reply["content"]  # mock 回显所切模型
    assert reply["model"] == "deepseek-chat"

    # 清除覆盖（空串）→ 回到人设默认 gpt-4o-mini
    await client.patch(
        f"/api/v1/conversations/{conv['id']}", json={"model": ""}, headers=auth_headers
    )
    sent = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "你好"},
        headers=auth_headers,
    )
    assert sent.json()["assistant_message"]["model"] == "gpt-4o-mini"


async def test_regenerate_replaces_last_reply(client, auth_headers, persona):
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "第一问"},
        headers=auth_headers,
    )
    before = (
        await client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=auth_headers)
    ).json()
    assert len(before) == 3  # 开场白 + user + assistant

    # 重新生成（SSE 流式）
    import json

    async with client.stream(
        "POST", f"/api/v1/conversations/{conv['id']}/regenerate", headers=auth_headers
    ) as resp:
        assert resp.status_code == 200
        events = [line[6:] async for line in resp.aiter_lines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"

    after = (
        await client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=auth_headers)
    ).json()
    assert len(after) == 3  # 替换而非追加
    assert after[-1]["role"] == "assistant"
    assert "".join(
        json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1]
    ) == after[-1]["content"]


async def test_regenerate_on_opening_message_only(client, auth_headers, persona):
    """只有开场白时 regenerate 不应误删开场白。"""
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    async with client.stream(
        "POST", f"/api/v1/conversations/{conv['id']}/regenerate", headers=auth_headers
    ) as resp:
        assert resp.status_code == 200
        events = [line[6:] async for line in resp.aiter_lines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    # 开场白仍在（没有 assistant 被删），新回复追加
    msgs = (
        await client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=auth_headers)
    ).json()
    assert [m["role"] for m in msgs] == ["assistant", "assistant"]
    assert msgs[0]["content"] == persona["opening_message"]


async def _admin_models(client, auth_headers):
    resp = await client.get("/api/v1/admin/models", headers=auth_headers)
    assert resp.status_code == 200
    return resp.json()


async def test_delete_default_model_promotes_sort_min(client, auth_headers, user_tokens, db_engine):
    """需求单：删除默认模型后自动回退 —— enabled 中 sort 最小者顶上，恰有一个默认。"""
    await _promote(db_engine, user_tokens["user"]["id"])
    await client.get("/api/v1/models", headers=auth_headers)  # 触发播种（admin 端点自身不播种）

    # 播种 3 个：默认 gpt-4o-mini(sort 0)。删除它 → 应由 gpt-4o(sort 1) 顶上
    mini = next(m for m in await _admin_models(client, auth_headers) if m["model"] == "gpt-4o-mini")
    assert mini["is_default"] is True

    resp = await client.delete(f"/api/v1/admin/models/{mini['id']}", headers=auth_headers)
    assert resp.status_code == 204

    models = await _admin_models(client, auth_headers)
    defaults = [m for m in models if m["is_default"]]
    assert len(models) == 2
    assert len(defaults) == 1, f"删除默认后应恰有一个默认，实为 {defaults}"
    assert defaults[0]["model"] == "gpt-4o"  # 剩余 enabled 中 sort 最小


async def test_delete_default_model_tie_break_created_at(client, auth_headers, user_tokens, db_engine):
    """sort 并列时取创建更早者为新默认。"""
    await _promote(db_engine, user_tokens["user"]["id"])
    await client.get("/api/v1/models", headers=auth_headers)  # 触发播种（admin 端点自身不播种）

    a = (await client.post("/api/v1/admin/models", json={"name": "A", "model": "tie-a", "sort": 9}, headers=auth_headers)).json()
    b = (await client.post("/api/v1/admin/models", json={"name": "B", "model": "tie-b", "sort": 9}, headers=auth_headers)).json()
    # 禁用其他启用模型，只留默认 + A/B（sort 并列）
    for m in await _admin_models(client, auth_headers):
        if m["model"] in ("gpt-4o", "deepseek-chat"):
            await client.patch(f"/api/v1/admin/models/{m['id']}", json={"enabled": False}, headers=auth_headers)

    mini = next(m for m in await _admin_models(client, auth_headers) if m["is_default"])
    assert (await client.delete(f"/api/v1/admin/models/{mini['id']}", headers=auth_headers)).status_code == 204

    defaults = [m for m in await _admin_models(client, auth_headers) if m["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["model"] == "tie-a"  # A 先创建


async def test_delete_non_default_keeps_default(client, auth_headers, user_tokens, db_engine):
    """删非默认不动默认。"""
    await _promote(db_engine, user_tokens["user"]["id"])
    await client.get("/api/v1/models", headers=auth_headers)  # 触发播种（admin 端点自身不播种）
    deepseek = next(m for m in await _admin_models(client, auth_headers) if m["model"] == "deepseek-chat")
    assert (await client.delete(f"/api/v1/admin/models/{deepseek['id']}", headers=auth_headers)).status_code == 204

    defaults = [m for m in await _admin_models(client, auth_headers) if m["is_default"]]
    assert len(defaults) == 1 and defaults[0]["model"] == "gpt-4o-mini"


async def test_delete_default_with_no_enabled_left(client, auth_headers, user_tokens, db_engine):
    """删除默认后无启用模型可顶上 → 无默认（空列表语义，不报错）。"""
    await _promote(db_engine, user_tokens["user"]["id"])
    await client.get("/api/v1/models", headers=auth_headers)  # 触发播种（admin 端点自身不播种）
    # 全部禁用后只剩默认自己？——先禁用其余，再删默认
    for m in await _admin_models(client, auth_headers):
        if not m["is_default"]:
            await client.patch(f"/api/v1/admin/models/{m['id']}", json={"enabled": False}, headers=auth_headers)
    mini = next(m for m in await _admin_models(client, auth_headers) if m["is_default"])
    assert (await client.delete(f"/api/v1/admin/models/{mini['id']}", headers=auth_headers)).status_code == 204

    models = await _admin_models(client, auth_headers)
    assert all(not m["is_default"] for m in models)  # 无启用可顶上 → 无默认
