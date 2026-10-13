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
    #
    # ⚠️ **两处都读**（顶层 `timeouts` 与 `features.timeouts`）：§8.1 只写"在 capabilities
    # 暴露 `timeouts`"、§8.3 的形状示例里根本没有该字段，**条文没钉死嵌套层级**。真实服务
    # 放在 `features.timeouts`（`msvc/main.py` 的 capabilities），而这里此前只读顶层
    # ⇒ 对真实服务永远读不到，再叠上"字段缺失即跳过"，红线 8 断言在生产里**静默不执行**
    # （P2-2 名义闭合、实则从未触发，与 P1-8 是同一类"两端各跑自家桩"的盲区）。
    # 层级应由架构师在条文里钉死；实现先做兼容读，见工作单 P1-9 回执。
    features = caps.get("features") or {}
    timeouts = caps.get("timeouts") or features.get("timeouts") or {}

    # ── RB-3：**缺键即失败**（协议 §8.1 硬约束 2，v1.7-澄清5 增补）────────────────────
    # 此前这里写的是 `if inner is None: continue`，注释给的理由是"Proxy 过渡期无
    # capabilities 契约（§8.3）"—— **那个理由是假的**：proxy 模式在本函数开头
    # （`mode == "proxy"` 那一支）**已经 return**，根本走不到这个循环。它保护的不是 proxy，
    # 只是把 native 路径的缺键**静默吞掉**：整个 `timeouts` 缺失时每个键都是 `None`、
    # 全部 `continue`，于是断言一条都不执行、**零日志零警告**。这正是 P2-2「名义闭合、
    # 实则空转」的真相。role 卡 §3.4 明令禁止用注释缩小契约范围，故连带删掉假理由。
    required_keys = (
        "upstream_first_token_s",
        "upstream_first_content_s",
        "upstream_chunk_idle_s",
        "upstream_total_s",
    )
    missing = [k for k in required_keys if timeouts.get(k) is None]
    if missing:
        raise ProtocolVersionError(
            f"capabilities 缺少超时声明键 {missing}（§8.1 硬约束 2，v1.7-澄清5：缺键即失败）。"
            f"规范位置 = features.timeouts（当前读到的 timeouts = {timeouts!r}）。"
            f"模型服务实现应暴露 {list(required_keys)} 四项实际值；"
            f"平台**不会**「字段缺失即跳过」——那正是红线 8 断言在生产里从未执行的原因。"
        )

    # ── RB-4：内层预算的**对标对象**取决于服务是否真会发心跳（协议 §8.1 硬约束 2，
    # v1.7-澄清5 的 A 裁定）────────────────────────────────────────────────────────
    # 服务会发 `: keepalive` ⇒ 平台「流式空闲上限 90s」**永不触发**（心跳持续复位它），
    # 真正的外层边界是「绝对上限 180s」；不发 ⇒ 仍是空闲上限。对标错了就是两种误判：
    # ① 会发心跳的服务把内层设成 120s（合法，<180s）被拦下；② 断言放行但流被 180s 砍断、
    # 归因成"上游超时"。
    #
    # 声明位缺失**不**拒启动（协议 §8.1 硬约束 2 末尾，v1.7-澄清6）：
    # 它是**能力放宽位**，只在显式 true 时才把对标放宽到 180s ⇒ 缺失本身即安全的确定值
    # （不可能比 90s 宽），断言照常执行、无静默跳过 ⇒ 与显式 false 同待遇。
    # 这与**超时键**的"缺失即失败"不矛盾：超时键缺失时「内层 < 外层」**根本无从执行**
    # （P2-2 空转病根），而本键缺失不影响断言可执行性。且本键随本批新增，拒启动会让
    # 平台拒绝与任何未升级的旧服务启动（滚动升级先升平台 = 自造停服），换不到任何安全收益。
    keepalive = features.get("stream_keepalive") is True

    outer_idle = float(s.model_service_stream_idle_timeout)
    outer_abs = float(s.model_service_stream_max_duration)
    if keepalive:
        # 心跳会不断复位空闲计时 ⇒ 真外层是绝对上限。
        outer, outer_label, outer_cfg = (
            outer_abs,
            "平台流式绝对上限",
            "MODEL_SERVICE_STREAM_MAX_DURATION",
        )
    else:
        # 不发心跳（显式 false **或声明位缺失**，v1.7-澄清6 两者同待遇）⇒ **保守侧**：
        # 空闲上限是真外层。
        outer, outer_label, outer_cfg = (
            outer_idle,
            "平台流式空闲上限",
            "MODEL_SERVICE_STREAM_IDLE_TIMEOUT",
        )

    # 流式三项按上面选出的外层对标。文案**参数化**（工作单 B2 要求"按保守侧给出原因"）：
    # 硬写"90s"会让 keepalive=true 时的报错指向一个根本没参与判定的配置。
    for key, label in (
        ("upstream_first_token_s", "首 token 超时"),
        ("upstream_first_content_s", "首个正文 token 超时"),
        # H2 / O1：中途空闲预算同样必须 < 外层，否则平台先断开、
        # 服务侧的 `upstream_chunk_timeout` 归因永远看不到。
        ("upstream_chunk_idle_s", "中途空闲预算"),
    ):
        inner = float(timeouts[key])
        if inner >= outer:
            raise ProtocolVersionError(
                f"超时分层违反红线 8：模型服务{label} {inner}s ≥ {outer_label} "
                f"{outer}s（判定依据：capabilities features.stream_keepalive={keepalive}）。"
                f"内层必须严格小于外层，否则平台会先断开（钱花了、回包丢了）。"
                f"请调小模型服务该项超时或调大 {outer_cfg}。"
            )

    # `upstream_total_s` **仅约束非流式**（协议 §8.1 表），按平台读超时口径对标，**不**随
    # `stream_keepalive` 放宽：把它并进上面的循环会让 keepalive=true 时一条非流式预算
    # 被对标到 180s，即"放过了头"（工作单 W-1 架构师裁定 §五）。对标基准的修订另立单。
    total = float(timeouts["upstream_total_s"])
    if total >= outer_idle:
        raise ProtocolVersionError(
            f"超时分层违反红线 8：模型服务上游总超时 {total}s ≥ 平台流式空闲上限 "
            f"{outer_idle}s。（本项仅约束非流式调用，故对标基准固定为空闲上限、不随 "
            f"features.stream_keepalive 放宽。）请调小模型服务该项超时或调大 "
            f"MODEL_SERVICE_STREAM_IDLE_TIMEOUT。"
        )
    logger.info(
        "模型服务自检通过：native 模式，协议版本 %s，流式内层预算对标 %s %ss"
        "（stream_keepalive=%s）",
        pv,
        outer_label,
        outer,
        keepalive,
    )


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
