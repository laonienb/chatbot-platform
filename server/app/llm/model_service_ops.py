"""模型服务运维面（S4）：启动自检（协议 §8.3）与目录同步（§9）。

仅 `LLM_BACKEND=remote` 时启用；mock/litellm 模式下所有入口早退，零副作用。

- startup_self_check：native 模式**必须**校验 /v1/capabilities 协议版本，不符即抛
  （红线7 拒绝启动）；proxy 过渡模式跳过 capabilities，改探活 + 打「无成本/能力契约」警告。
- sync_catalog：拉 /v1/models，为目录中**缺失**的模型补 llm_models 行（enabled=False、
  无凭证）。既有行的展示名/排序/启用/白名单是平台业务字段，同步**不覆盖**（Q6-A）。
"""

import asyncio
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import LlmModel

logger = logging.getLogger("app.llm.model_service_ops")

_OPS_TIMEOUT = 10.0


class ProtocolVersionError(RuntimeError):
    """native 模式协议版本不兼容（红线7）：平台拒绝启动。"""


def _enabled() -> bool:
    return get_settings().llm_backend.lower() == "remote"


def _base_url() -> str:
    base = get_settings().model_service_base_url.rstrip("/")
    if not base:
        raise ProtocolVersionError("MODEL_SERVICE_BASE_URL 未配置，无法自检/同步")
    return base


def _headers() -> dict[str, str]:
    s = get_settings()
    return {
        "Authorization": f"Bearer {s.model_service_token}",
        "X-Model-Service-Version": str(s.model_service_version),
    }


async def fetch_capabilities() -> dict:
    try:
        async with httpx.AsyncClient(timeout=_OPS_TIMEOUT) as client:
            resp = await client.get(f"{_base_url()}/v1/capabilities", headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise _unreachable("/v1/capabilities", exc) from exc


async def fetch_models() -> list[str]:
    """GET /v1/models → 上游模型 id 列表（OpenAI 目录形状 {"data":[{"id":...}]}）。"""
    try:
        async with httpx.AsyncClient(timeout=_OPS_TIMEOUT) as client:
            resp = await client.get(f"{_base_url()}/v1/models", headers=_headers())
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise _unreachable("/v1/models", exc) from exc
    items = data.get("data") if isinstance(data, dict) else data
    return [m["id"] for m in (items or []) if isinstance(m, dict) and m.get("id")]


def _unreachable(path: str, exc: Exception) -> ProtocolVersionError:
    """P1-5：把裸 httpx 异常换成部署期可操作的指引（否则运维只看到 traceback）。"""
    return ProtocolVersionError(
        f"模型服务不可达：GET {path} 失败（{type(exc).__name__}）。"
        f"请确认 ① 模型服务已启动；② MODEL_SERVICE_BASE_URL 配置正确"
        f"（当前 = {get_settings().model_service_base_url or '<空>'}）；"
        f"③ 平台所在网络可访问该地址（模型服务仅集群内网可达）。原始错误：{exc!r}"
    )


async def startup_self_check() -> None:
    """§8.3 过渡模式三支判定。native 版本不符 → 抛 ProtocolVersionError（拒启动）。"""
    if not _enabled():
        return
    s = get_settings()
    mode = s.model_service_mode.lower()
    if mode == "proxy":
        logger.warning(
            "模型服务过渡模式（proxy）：无成本/能力契约，成本口径回落平台价格表（协议 §8.3）"
        )
        # 探活 + 目录可达即可，不校验 capabilities（LiteLLM Proxy 无此端点）
        try:
            async with httpx.AsyncClient(timeout=_OPS_TIMEOUT) as client:
                live = await client.get(f"{_base_url()}/health/liveness", headers=_headers())
                live.raise_for_status()
        except httpx.HTTPError as exc:
            raise _unreachable("/health/liveness", exc) from exc
        await fetch_models()
        return

    # native：必须调 capabilities，版本不符拒绝启动（红线7 全额生效）
    caps = await fetch_capabilities()
    pv = caps.get("protocol_version")
    if pv != s.model_service_version:
        raise ProtocolVersionError(
            f"模型服务协议版本不兼容：服务={pv!r} 平台={s.model_service_version}（红线7，拒绝启动）"
        )

    # P2-2 / §8.1：模型服务必须自持上游超时并在 capabilities 暴露，
    # 平台据此**断言内层 < 外层**（自动化红线 8，替代人工读配置）。
    timeouts = caps.get("timeouts") or {}
    inner_total = timeouts.get("upstream_total_s")
    outer_idle = s.model_service_stream_idle_timeout
    if inner_total is not None:
        if float(inner_total) >= float(outer_idle):
            raise ProtocolVersionError(
                f"超时分层违反红线 8：模型服务上游总超时 {inner_total}s ≥ 平台流式空闲上限 "
                f"{outer_idle}s。内层必须严格小于外层，否则平台会先断开（钱花了、回包丢了）。"
                f"请调小模型服务超时或调大 MODEL_SERVICE_STREAM_IDLE_TIMEOUT。"
            )
        inner_first = timeouts.get("upstream_first_token_s")
        if inner_first is not None and float(inner_first) >= float(outer_idle):
            raise ProtocolVersionError(
                f"超时分层违反红线 8：模型服务首 token 超时 {inner_first}s ≥ 平台流式空闲上限 "
                f"{outer_idle}s。"
            )
    logger.info("模型服务自检通过：native 模式，协议版本 %s", pv)


async def sync_catalog(db: AsyncSession) -> dict:
    """§9 目录同步：补缺失 + 处理**下线**；不覆盖业务字段，不携带任何凭证。

    下线语义（协议 §9.1，P2-1）：上游 `/v1/models` 中**消失**的 id → 对应行标
    `enabled=False`；**永不删除行** —— 行承载展示名/排序/白名单等运营配置，
    "临时下架又恢复"不应该变成人工重建。若停用的行原是默认模型，清除其默认标记
    并回退到其他启用行（沿用 `default_model_string` 的选择口径）。
    """
    upstream_ids = await fetch_models()
    upstream = set(upstream_ids)
    rows = (await db.execute(select(LlmModel))).scalars().all()
    existing = {r.model for r in rows}

    added = 0
    for mid in upstream_ids:
        if mid in existing:
            continue
        db.add(
            LlmModel(
                name=mid,
                model=mid,
                api_base=None,  # 凭证已下沉模型服务（§9），平台注册表不再持有
                api_key=None,
                enabled=False,  # 新模型默认不启用，由管理员显式开
                is_default=False,
                sort=0,
            )
        )
        added += 1

    # 下线方向：上游没有的**已启用**行 → 停用（不删除）
    disabled = 0
    had_default_disabled = False
    for row in rows:
        if row.model not in upstream and row.enabled:
            row.enabled = False
            disabled += 1
            if row.is_default:
                row.is_default = False
                had_default_disabled = True

    new_default: str | None = None
    if had_default_disabled:
        # 默认回退：其他启用行里挑一个（先看原 is_default，再取任一启用行）
        candidates = [
            r for r in rows if r.enabled and r.model in upstream
        ]
        if candidates:
            chosen = next((r for r in candidates if r.is_default), candidates[0])
            chosen.is_default = True
            new_default = chosen.model

    if added or disabled or had_default_disabled:
        await db.commit()
    return {
        "added": added,
        "disabled": disabled,
        "total_upstream": len(upstream_ids),
        "new_default": new_default,
    }


async def _catalog_sync_loop(interval_seconds: int) -> None:
    from app.database import SessionLocal

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with SessionLocal() as session:
                result = await sync_catalog(session)
                if result["added"]:
                    logger.info("目录同步：新增 %d 个上游模型", result["added"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("目录同步本轮失败（下轮重试）")


async def resync_catalog_background() -> None:
    """404 model_not_found 触发的即时重同步（§9）。后台执行，吞异常不扰主流程。"""
    from app.database import SessionLocal

    try:
        async with SessionLocal() as session:
            await sync_catalog(session)
    except Exception:
        logger.exception("即时目录重同步失败")
