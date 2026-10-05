"""人设 CRUD 与可见性。"""


async def test_create_persona_auto_slug(client, auth_headers):
    resp = await client.post(
        "/api/v1/personas",
        json={"name": "Sunny Assistant", "system_prompt": "You are sunny."},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "sunny-assistant"
    assert resp.json()["visibility"] == "private"


async def test_create_persona_explicit_slug_conflict(client, auth_headers, persona):
    resp = await client.post(
        "/api/v1/personas",
        json={"name": "another", "slug": "libai", "system_prompt": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 409


async def test_create_persona_requires_auth(client):
    resp = await client.post("/api/v1/personas", json={"name": "x", "system_prompt": "y"})
    assert resp.status_code == 401


async def test_list_personas_includes_public_of_others(client, auth_headers, persona):
    # 第二个用户建一个公共人设
    other = await client.post(
        "/api/v1/auth/register",
        json={"email": "mallory@test.dev", "password": "password123"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    created = await client.post(
        "/api/v1/personas",
        json={
            "name": "shared helper",
            "slug": "shared-helper",
            "system_prompt": "help everyone",
            "visibility": "public",
        },
        headers=other_headers,
    )
    assert created.status_code == 201

    resp = await client.get("/api/v1/personas", headers=auth_headers)
    assert resp.status_code == 200
    slugs = {p["slug"] for p in resp.json()}
    assert "libai" in slugs  # 自己的
    assert "shared-helper" in slugs  # 他人的公共人设可见


async def test_private_persona_of_others_not_visible(client, auth_headers):
    other = await client.post(
        "/api/v1/auth/register",
        json={"email": "mallory2@test.dev", "password": "password123"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    created = await client.post(
        "/api/v1/personas",
        json={"name": "secret", "slug": "secret-p", "system_prompt": "hidden"},
        headers=other_headers,
    )
    pid = created.json()["id"]

    # 列表里看不到
    resp = await client.get("/api/v1/personas", headers=auth_headers)
    assert "secret-p" not in {p["slug"] for p in resp.json()}
    # 直接访问 404
    resp = await client.get(f"/api/v1/personas/{pid}", headers=auth_headers)
    assert resp.status_code == 404


async def test_update_persona(client, auth_headers, persona):
    resp = await client.patch(
        f"/api/v1/personas/{persona['id']}",
        json={"name": "诗仙", "temperature": 0.9},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "诗仙"
    assert resp.json()["temperature"] == 0.9
    # 未提供的 system_prompt 不变
    assert resp.json()["system_prompt"] == persona["system_prompt"]


async def test_update_persona_by_non_owner_forbidden(client, persona):
    other = await client.post(
        "/api/v1/auth/register",
        json={"email": "eve@test.dev", "password": "password123"},
    )
    resp = await client.patch(
        f"/api/v1/personas/{persona['id']}",
        json={"name": "hacked"},
        headers={"Authorization": f"Bearer {other.json()['access_token']}"},
    )
    assert resp.status_code == 403


async def test_delete_persona_blocked_when_referenced(client, auth_headers, persona):
    conv = await client.post(
        "/api/v1/conversations",
        json={"persona_id": persona["id"]},
        headers=auth_headers,
    )
    assert conv.status_code == 201
    resp = await client.delete(f"/api/v1/personas/{persona['id']}", headers=auth_headers)
    assert resp.status_code == 409


async def test_delete_persona_success(client, auth_headers):
    created = await client.post(
        "/api/v1/personas",
        json={"name": "temp persona", "system_prompt": "temp"},
        headers=auth_headers,
    )
    pid = created.json()["id"]
    resp = await client.delete(f"/api/v1/personas/{pid}", headers=auth_headers)
    assert resp.status_code == 204
    resp = await client.get(f"/api/v1/personas/{pid}", headers=auth_headers)
    assert resp.status_code == 404


async def test_fork_public_persona(client, auth_headers):
    """他人公共人设可 fork 为私有副本。"""
    other = await client.post(
        "/api/v1/auth/register", json={"email": "author@test.dev", "password": "password123"}
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    created = await client.post(
        "/api/v1/personas",
        json={
            "name": "苏轼",
            "slug": "sushi",
            "system_prompt": "你是苏轼。",
            "visibility": "public",
            "temperature": 0.7,
        },
        headers=other_headers,
    )
    original = created.json()

    resp = await client.post(f"/api/v1/personas/{original['id']}/fork", headers=auth_headers)
    assert resp.status_code == 201, resp.text
    forked = resp.json()
    assert forked["owner_id"] != original["owner_id"]
    assert forked["system_prompt"] == original["system_prompt"]
    assert forked["temperature"] == 0.7
    assert forked["forked_from"] == original["id"]
    assert forked["visibility"] == "private"
    assert forked["slug"] != original["slug"]


async def test_fork_private_persona_forbidden(client, auth_headers):
    other = await client.post(
        "/api/v1/auth/register", json={"email": "secret-author@test.dev", "password": "password123"}
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    created = await client.post(
        "/api/v1/personas",
        json={"name": "私密", "slug": "secret-f", "system_prompt": "x"},
        headers=other_headers,
    )
    resp = await client.post(f"/api/v1/personas/{created.json()['id']}/fork", headers=auth_headers)
    assert resp.status_code == 404


async def test_fork_own_persona_conflict(client, auth_headers, persona):
    resp = await client.post(f"/api/v1/personas/{persona['id']}/fork", headers=auth_headers)
    assert resp.status_code == 409
