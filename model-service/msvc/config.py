"""模型服务配置（环境变量注入，§3/§8.1/§9）。无凭证入 git；生产由部署侧提供。"""

import logging
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("msvc.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MS_", env_file=".env", extra="ignore")

    # 服务间鉴权（§3）：接受多个 token（新旧并行轮转窗口），逗号分隔。至少配一个。
    service_tokens: str = "dev-insecure-model-service-token"
    # 上游：fake（离线确定性，测试/本地）| litellm（真实 provider）
    upstream: str = "fake"
    litellm_api_base: str = ""
    litellm_api_key: str = ""
    litellm_default_model: str = "deepseek/deepseek-chat"

    # ── A-1（协议 §26.6-丙）：**协议名与供应商名分开建模** ─────────────────────────
    # 「OpenAI 兼容端点」是**协议**，「小蜜/xiaomimimo」是**供应商**；此前两者被混成
    # model 字符串的前缀，导致要么必须发 `openai/...`（撞红线5 的 deny 前缀判定），
    # 要么让 litellm 猜 provider。拆成两个显式字段后：协议名进 `custom_llm_provider`，
    # 供应商名进 `x_model_service.provider`（归因/用量统计），请求里的 model 保持无前缀。
    # (2) 协议名：交给 litellm 的 `custom_llm_provider`，默认 openai 兼容。
    litellm_provider: str = "openai"
    # (4) 供应商名：**部署侧声明**，进 `provider` 归因与红线5 的启动期对账。字段名决定
    # env 名（`MS_` + 大写 ⇒ `MS_UPSTREAM_VENDOR`）；改成别的名字会被 `extra="ignore"`
    # 静默吞掉，配置看着生效、其实没绑上。
    # 默认空 = 未声明 ⇒ 启动 WARNING（不 fail-fast：旧部署没有这个 env，升级期不能起不来）。
    upstream_vendor: str = ""

    # 合规硬策略（§4.3）：命中 deny 列表的 provider 一律 403，调用方不可放宽（红线5）。
    compliance_deny_providers: str = "openai,anthropic"

    # 目录（§9）：本服务对外声明的模型集合，逗号分隔（真实由上游/provider 注册表决定）。
    # ⚠️ **无前缀** model 会走 `main.py` 的目录校验（有 "/" 才免检）⇒ 接真实上游时
    # 目标 model id 必须列进这里，否则请求在到达上游之前就 404 `model_not_found`（§26.6-丙 第3项）。
    catalog_models: str = "deepseek-chat,qwen-plus"

    # 超时（§8.1，模型服务侧自持，红线8：内层 < 平台流式空闲上限 90s）。
    upstream_first_token_timeout: float = 15.0
    # RB-1（协议 §28.1／§8.1 表新增行）：**首个正文 token** 预算——从请求开始到首个非空
    # `delta.content` 的预算，**含全部推理时长**。首 token 行修正后（reasoning chunk 会复位
    # 它，§28.1）不再覆盖"只推理不出正文"这一场景，外层由本项承担；超 ⇒ 504
    # `upstream_reasoning_timeout`（§7 表，与 `upstream_timeout` **区分归因**）。
    # ⚠️ 60s 是**默认值、不是契约值**（部署侧可按模型族放大，须守红线8）。
    upstream_first_content_timeout: float = 60.0
    # 首字节**之后**的 chunk 间隔预算（O1，v1.7）：中途停顿超此值 → 流内错误帧
    # `upstream_chunk_timeout`（与首 token 超时的 `upstream_timeout` 区分归因）。
    # 旧实现硬编码 1.0s，会把真实上游（LiteLLMProvider）的正常停顿误杀。
    upstream_chunk_idle_timeout: float = 30.0
    upstream_total_timeout: float = 60.0

    # 幂等窗口（§8.2.1）：≥10 分钟。窗口对**流式/非流式同等适用**（§8.2.2）。
    idempotency_window_seconds: int = 600
    # 窗口缓存的条数/体积上限（§8.2.2-3：缓存必须有界）。超限按插入序淘汰最旧条目，
    # 被淘汰的 key 后续重发按未命中处理（会真实调用上游）——语义已声明于 README。
    idempotency_cache_max_entries: int = 256
    idempotency_cache_max_chars: int = 1_000_000
    # SW-5：在飞位 TTL 兜底。默认对齐上游总超时（60s）——超过它还在"飞"，说明那次调用
    # 已经丢了（异常漏放 finally），此时必须放行，否则同 key 永久 409。
    idempotency_inflight_ttl_seconds: float = 60.0
    # RB-5（协议 §8.2.1 TTL 表／§28.3）：**入窗分类不只分类"入不入"，还要分"入多久"**。
    # 成功回放的窗口需要长（默认 600s）；「结果不明」类（首 token 超时、首个正文 token 超时、
    # 总超时、已发起调用后的 5xx）入窗只为**防双烧**、不是回放答案 ⇒ 用独立短 TTL。
    # 客户端抖动重发集中在**秒级**，600s 会把一次慢推理变成同一条消息的窗口级锁死。
    # ⚠️ 60s 是默认值、非契约值；且必须 **< `MS_IDEMPOTENCY_WINDOW_SECONDS`**（否则"分类"无意义）。
    idempotency_error_ttl_seconds: float = 60.0

    protocol_version: int = 1
    implementation_name: str = "model-service"
    implementation_version: str = "0.1.0"

    @property
    def token_set(self) -> set[str]:
        return {t.strip() for t in self.service_tokens.split(",") if t.strip()}

    @property
    def deny_providers(self) -> set[str]:
        return {p.strip().lower() for p in self.compliance_deny_providers.split(",") if p.strip()}

    @property
    def models(self) -> list[str]:
        return [m.strip() for m in self.catalog_models.split(",") if m.strip()]

    @model_validator(mode="after")
    def _reconcile_upstream_vendor_with_compliance(self) -> "Settings":
        """A-1 第4项（协议 §26.6-丙 / 架构师 B 2.4）：红线5 对**真实出口**的启动期对账。

        为什么需要：A-1 之后请求里的 model **不带** provider 前缀 ⇒ `_check_compliance`
        的「model 前缀 ∈ deny」那条判定臂对真实流量恒不命中，而平台不发 `routing`
        ⇒ allow 臂也是死的；真实出口由 env（`MS_LITELLM_API_BASE`）决定，那里**从不**被
        合规检查读到。于是条文一字未放宽，红线5 却对真实出口失明。本断言把这条臂搬到
        启动期：**供应商名命中 deny 列表 ⇒ 拒绝启动**（不是请求期 403——请求期已经晚了，
        且会被误读成上游问题）。

        `vendor` 未声明只给 WARNING、不 fail-fast：升级窗口里旧部署没有这个 env。
        """
        if self.upstream != "litellm":
            return self
        vendor = self.upstream_vendor.strip().lower()
        if not vendor:
            logger.warning(
                "MS_UPSTREAM=litellm 但未声明 MS_UPSTREAM_VENDOR：provider 归因将回落到 model/"
                "MS_LITELLM_DEFAULT_MODEL 的前缀 ⇒ usage_logs.provider 记的可能不是真实出口，"
                "且红线5 的启动期供应商对账被跳过。生产部署必须显式声明供应商名。"
            )
            return self
        if vendor in self.deny_providers:
            raise ValueError(
                f"合规硬策略冲突：真实上游供应商 MS_UPSTREAM_VENDOR={vendor!r} 命中 "
                f"MS_COMPLIANCE_DENY_PROVIDERS={sorted(self.deny_providers)}（红线5）。"
                f"拒绝启动——请把供应商名改为真实值，或改 MS_LITELLM_API_BASE 换出口；"
                f"放宽 deny 列表是运营决策，不在本服务配置里静默做。"
            )
        return self


    @model_validator(mode="after")
    def _check_budget_layering(self) -> "Settings":
        """预算/窗口的**内部先后关系**必须在启动期就成立（协议 §28.1-2、§28.3-1）。

        - `upstream_first_content_timeout ≥ upstream_first_token_timeout`：首个正文 token 是
          **外层**（含全部推理时长），若它比首 token 还小，第二项预算永不可达 ⇒ 分类无意义，
          且"上游正在推理"会被误归因成"上游没连上"。
        - `idempotency_error_ttl_seconds < idempotency_window_seconds`：错误类入窗只为防双烧，
          窗口不比成功短就不是"分类"而是"同一个数写两遍"（§8.2.1 TTL 表）。
        """
        if self.upstream_first_content_timeout < self.upstream_first_token_timeout:
            raise ValueError(
                f"预算层级违反协议 §28.1：MS_UPSTREAM_FIRST_CONTENT_TIMEOUT="
                f"{self.upstream_first_content_timeout} < MS_UPSTREAM_FIRST_TOKEN_TIMEOUT="
                f"{self.upstream_first_token_timeout}。首个正文 token 是含推理时长的外层预算，"
                f"必须 ≥ 首 token 预算，否则它永不可达、错误归因（只推理不出正文会被报成上游没连上）。"
            )
        if self.idempotency_error_ttl_seconds >= self.idempotency_window_seconds:
            raise ValueError(
                f"入窗 TTL 分类违反协议 §8.2.1/§28.3：MS_IDEMPOTENCY_ERROR_TTL_SECONDS="
                f"{self.idempotency_error_ttl_seconds} ≥ MS_IDEMPOTENCY_WINDOW_SECONDS="
                f"{self.idempotency_window_seconds}。错误类窗口必须比成功窗口短，"
                f"否则一次首 token 超时会把同一条消息锁死整个幂等窗。"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
