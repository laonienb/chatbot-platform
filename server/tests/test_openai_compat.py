"""OpenAI 兼容层 /v1/chat/completions 与 API Key 管理。"""


async def _create_key(client, auth_headers, **kwargs) -> str:
    resp = await client.post("/api/v1/me/keys", json={"name": "astrbot", **kwargs}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


async def test_compat_requires_api_key(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "persona:libai", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401
    # 错误为 OpenAI 格式
    assert "error" in resp.json()
    assert resp.json()["error"]["message"]


async def test_compat_rejects_jwt_token(client, auth_headers):
    """JWT 是第一方凭证，不能用于兼容层。"""
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "persona:libai", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers,
    )
    assert resp.status_code == 401


async def test_compat_persona_model(client, auth_headers, persona):
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "persona:libai",
            "messages": [{"role": "user", "content": "用李白的口吻写一首诗"}],
            "user": "qq:12345",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "persona:libai"
    content = body["choices"][0]["message"]["content"]
    assert "[persona:libai]" in content  # 人设已注入
    assert "用李白的口吻写一首诗" in content  # mock 回显
    assert body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]


async def test_compat_plain_model_passthrough(client, auth_headers):
    """非 persona: 前缀的模型名原样透传给网关。"""
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "deepseek-chat"
    assert "[mock:deepseek-chat]" in resp.json()["choices"][0]["message"]["content"]


async def test_compat_persona_not_found(client, auth_headers):
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "persona:no-such-persona", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_compat_model_whitelist(client, auth_headers, persona):
    key = await _create_key(client, auth_headers, model_whitelist=["persona:libai"])
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_not_allowed"
    # 白名单内的模型可用
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "persona:libai", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200


async def test_compat_stream_persona(client, auth_headers, persona):
    """M1：兼容层流式输出 OpenAI chunk 格式。"""
    import json

    key = await _create_key(client, auth_headers)
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "persona:libai",
            "messages": [{"role": "user", "content": "流式问候"}],
            "stream": True,
        },
        headers={"Authorization": f"Bearer {key}"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = [
            line[len("data: "):] async for line in resp.aiter_lines() if line.startswith("data: ")
        ]

    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["model"] == "persona:libai"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[1:])
    assert "[persona:libai]" in content
    assert "流式问候" in content


async def test_compat_stream_persona_not_found_returns_404(client, auth_headers):
    """persona 不存在必须在开流前报 404，而不是流中报错。"""
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "persona:ghost", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_compat_validation_error_openai_format(client, auth_headers):
    key = await _create_key(client, auth_headers)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": []},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 422
    assert "error" in resp.json()


async def test_compat_records_usage(client, auth_headers, persona, db_engine):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import UsageLog

    key = await _create_key(client, auth_headers)
    await client.post(
        "/v1/chat/completions",
        json={"model": "persona:libai", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        logs = (await session.execute(select(UsageLog))).scalars().all()
    assert len(logs) == 1
    log = logs[0]
    assert log.persona_id is not None
    assert log.conversation_id is None  # 无状态链路
    assert log.api_key_id is not None
    assert log.prompt_tokens > 0


# ---------- 用量统计 ----------


async def test_me_usage_aggregates_by_model(client, auth_headers, persona):
    key = await _create_key(client, auth_headers)
    # 有状态链路一条（persona 默认模型 gpt-4o-mini）+ 兼容层一条（gpt-4o）
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
    await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.get("/api/v1/me/usage", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 30
    assert body["total_requests"] == 2
    assert body["total_prompt_tokens"] > 0
    models = {m["model"]: m for m in body["by_model"]}
    assert set(models) == {"gpt-4o-mini", "gpt-4o"}
    assert models["gpt-4o"]["requests"] == 1
    assert models["gpt-4o"]["completion_tokens"] > 0


async def test_me_usage_requires_auth(client):
    assert (await client.get("/api/v1/me/usage")).status_code == 401


async def test_me_usage_window_clamped(client, auth_headers):
    resp = await client.get("/api/v1/me/usage", params={"days": 9999}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["days"] == 365


# ---------- API Key 管理 ----------


async def test_api_key_plaintext_returned_once(client, auth_headers):
    resp = await client.post("/api/v1/me/keys", json={"name": "k1"}, headers=auth_headers)
    assert resp.status_code == 201
    key = resp.json()["key"]
    assert key.startswith("sk-")

    lst = (await client.get("/api/v1/me/keys", headers=auth_headers)).json()
    assert len(lst) == 1
    assert "key" not in lst[0]
    assert lst[0]["key_prefix"].startswith("sk-")


async def test_revoked_key_rejected(client, auth_headers):
    resp = await client.post("/api/v1/me/keys", json={"name": "k2"}, headers=auth_headers)
    key_id, key = resp.json()["id"], resp.json()["key"]

    assert (
        await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
    ).status_code == 200

    assert (
        await client.delete(f"/api/v1/me/keys/{key_id}", headers=auth_headers)
    ).status_code == 204

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 401
