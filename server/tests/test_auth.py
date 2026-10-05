"""认证链路：注册 / 登录 / 刷新 / me。"""


async def test_register_returns_tokens(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "bob@test.dev", "password": "password123"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == "bob@test.dev"
    assert body["user"]["role"] == "user"


async def test_register_duplicate_email(client, user_tokens):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "alice@test.dev", "password": "password456"},
    )
    assert resp.status_code == 409


async def test_register_password_too_short(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "carol@test.dev", "password": "short"},
    )
    assert resp.status_code == 422


async def test_login_success(client, user_tokens):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "alice@test.dev", "password": "password123"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_login_wrong_password(client, user_tokens):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "alice@test.dev", "password": "wrong-password"},
    )
    assert resp.status_code == 401


async def test_me_requires_token(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_me(client, auth_headers):
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "alice@test.dev"


async def test_me_rejects_refresh_token(client, user_tokens):
    """refresh token 不能当 access token 用。"""
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {user_tokens['refresh_token']}"}
    )
    assert resp.status_code == 401


async def test_refresh_token_flow(client, user_tokens):
    resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": user_tokens["refresh_token"]},
    )
    assert resp.status_code == 200
    new_tokens = resp.json()
    # 新 access token 可用且指向同一用户
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == user_tokens["user"]["id"]


async def test_refresh_rejects_access_token(client, user_tokens):
    resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": user_tokens["access_token"]},
    )
    assert resp.status_code == 401


async def test_update_me_display_name(client, auth_headers):
    resp = await client.patch(
        "/api/v1/auth/me",
        json={"display_name": "新昵称"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "新昵称"


async def test_update_me_password_flow(client, user_tokens):
    headers = {"Authorization": f"Bearer {user_tokens['access_token']}"}
    # 当前密码错误 → 403
    resp = await client.patch(
        "/api/v1/auth/me",
        json={"new_password": "newpassword123", "current_password": "wrong"},
        headers=headers,
    )
    assert resp.status_code == 403
    # 正确改密
    resp = await client.patch(
        "/api/v1/auth/me",
        json={"new_password": "newpassword123", "current_password": "password123"},
        headers=headers,
    )
    assert resp.status_code == 200
    # 旧密码失效、新密码可登录
    assert (
        await client.post(
            "/api/v1/auth/login", json={"email": "alice@test.dev", "password": "password123"}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login", json={"email": "alice@test.dev", "password": "newpassword123"}
        )
    ).status_code == 200


async def test_update_me_requires_auth(client):
    assert (await client.patch("/api/v1/auth/me", json={"display_name": "x"})).status_code == 401
