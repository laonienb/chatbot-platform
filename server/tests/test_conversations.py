"""会话链路：创建（开场白注入）→ 发消息 → 翻历史 → 管理与删除。"""


async def test_create_conversation_with_opening_message(client, auth_headers, persona):
    resp = await client.post(
        "/api/v1/conversations",
        json={"persona_id": persona["id"]},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    conv = resp.json()
    assert conv["title"] == persona["name"]  # 默认标题取人设名

    messages = (
        await client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=auth_headers)
    ).json()
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == persona["opening_message"]


async def test_create_conversation_unknown_persona(client, auth_headers):
    resp = await client.post(
        "/api/v1/conversations",
        json={"persona_id": "00000000-0000-0000-0000-000000000000"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_send_message_full_loop(client, auth_headers, persona):
    """M0 验收核心链路：建会话 → 发消息 → 拿到带人设标识的回复。"""
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()

    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "写一首关于秋天的诗"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_message"]["content"] == "写一首关于秋天的诗"
    assert body["user_message"]["role"] == "user"
    assert body["assistant_message"]["role"] == "assistant"
    # mock 后端：回复带 persona 标识 + 回显用户输入
    assert "[persona:libai]" in body["assistant_message"]["content"]
    assert "写一首关于秋天的诗" in body["assistant_message"]["content"]
    assert body["assistant_message"]["completion_tokens"] > 0

    # 历史包含：开场白 + user + assistant
    messages = (
        await client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=auth_headers)
    ).json()
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant"]


async def read_sse_events(resp) -> list[str]:
    """收集 SSE 流中所有 data: 负载。"""
    events = []
    async for line in resp.aiter_lines():
        if line.startswith("data: "):
            events.append(line[len("data: "):])
    return events


async def test_send_message_stream(client, auth_headers, persona, db_engine):
    """M1 核心：SSE 流式返回 OpenAI chunk 格式，结束后落库消息与用量。"""
    import json
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import UsageLog

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()

    async with client.stream(
        "POST",
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "流式写诗", "stream": True},
        headers=auth_headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = await read_sse_events(resp)

    assert events[-1] == "[DONE]"
    import json

    contents = [json.loads(e)["choices"][0]["delta"]["content"] for e in events[:-1]]
    full = "".join(contents)
    assert "[persona:libai]" in full
    assert "流式写诗" in full
    assert len(contents) > 1  # 确实是分块到达

    # 流结束后消息已落库：开场白 + user + assistant
    messages = (
        await client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=auth_headers)
    ).json()
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant"]
    assert messages[-1]["content"] == full  # 分块拼接 == 落库全文
    assert messages[-1]["completion_tokens"] > 0

    # 用量账本记录了会话归属
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        logs = (
            await session.execute(
                select(UsageLog).where(UsageLog.conversation_id == UUID(conv["id"]))
            )
        ).scalars().all()
    assert len(logs) == 1
    assert logs[0].persona_id is not None


async def test_send_message_stream_requires_owner(client, auth_headers, persona):
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    other = await client.post(
        "/api/v1/auth/register", json={"email": "stream-thief@test.dev", "password": "password123"}
    )
    async with client.stream(
        "POST",
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "hi", "stream": True},
        headers={"Authorization": f"Bearer {other.json()['access_token']}"},
    ) as resp:
        assert resp.status_code == 404


async def test_message_history_pagination(client, auth_headers, persona):
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    for i in range(5):
        await client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"content": f"msg-{i}"},
            headers=auth_headers,
        )
    # 全量 11 条：开场白 + 5×(user+assistant)；limit=4 → 最新 4 条，时间正序
    messages = (
        await client.get(
            f"/api/v1/conversations/{conv['id']}/messages", params={"limit": 4}, headers=auth_headers
        )
    ).json()
    assert [m["content"] for m in messages if m["role"] == "user"] == ["msg-3", "msg-4"]

    # before_id 翻页：取当前页第一条之前的历史，再拿 4 条
    older = (
        await client.get(
            f"/api/v1/conversations/{conv['id']}/messages",
            params={"limit": 4, "before_id": messages[0]["id"]},
            headers=auth_headers,
        )
    ).json()
    assert [m["content"] for m in older if m["role"] == "user"] == ["msg-1", "msg-2"]


async def test_conversation_isolated_between_users(client, auth_headers, persona):
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    other = await client.post(
        "/api/v1/auth/register",
        json={"email": "intruder@test.dev", "password": "password123"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    # 别人看不到也发不进
    assert (
        await client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=other_headers)
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"content": "hi"},
            headers=other_headers,
        )
    ).status_code == 404


async def test_update_and_delete_conversation(client, auth_headers, persona):
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.patch(
        f"/api/v1/conversations/{conv['id']}",
        json={"title": "诗歌创作", "pinned": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True

    # 置顶会话排在列表最前
    lst = (await client.get("/api/v1/conversations", headers=auth_headers)).json()
    assert lst[0]["id"] == conv["id"]

    resp = await client.delete(f"/api/v1/conversations/{conv['id']}", headers=auth_headers)
    assert resp.status_code == 204
    assert (
        await client.get("/api/v1/conversations", headers=auth_headers)
    ).json() == []
