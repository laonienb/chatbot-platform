/** 平台 API 客户端：token 管理、401 自动刷新重试、SSE 解析。 */

// 默认同源（经 Next.js 反向代理转发到后端）；前后端分离部署时用 NEXT_PUBLIC_API_BASE 指向后端
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export type Tokens = { access_token: string; refresh_token: string };

const ACCESS_KEY = "cp_access_token";
const REFRESH_KEY = "cp_refresh_token";

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(ACCESS_KEY);
}

export function saveTokens(t: Tokens) {
  localStorage.setItem(ACCESS_KEY, t.access_token);
  localStorage.setItem(REFRESH_KEY, t.refresh_token);
}

export function clearTokens() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

/**
 * 错误语义分类（供 UI 差异化处理）。
 * - balance        余额不足（402）：引导充值，**禁止自动重试**
 * - quota          额度/预算用尽（429 budget_exhausted / 无 Retry-After）：重试无用
 * - rate_limit     限流（429 rate_limited / 带 Retry-After）：可按 retryAfter 退避
 * - service        服务繁忙（503 service_unavailable / upstream_error）：可稍后手动重试
 * - context        上下文超长（413）：需用户裁剪输入，重试无用
 * - forbidden      合规否决（403 compliance_denied）：该模型不可用，重试无用
 * - model_missing  模型不存在（404 model_not_found）：重试无用
 * - truncated      流未收到 [DONE] 就结束：连接被提前掐断，可重试
 *                  （注意与「绝对上限中断」区分 —— 后者按协议仍发 [DONE]，不属此类）
 */
export type ApiErrorKind =
  | "balance"
  | "quota"
  | "rate_limit"
  | "service"
  | "context"
  | "forbidden"
  | "model_missing"
  | "truncated";

export class ApiError extends Error {
  status: number;
  kind?: ApiErrorKind;
  /** 仅 rate_limit：限流退避秒数（来自 Retry-After） */
  retryAfter?: number;
  /** 平台 / 模型服务的机器可判语义码（协议 §7），无则为 undefined */
  code?: string;
  /**
   * 仅 422：后端字段级校验原因，键是后端字段名（`loc` 末段）、值是人话。
   * 表单据此把错误落到具体输入框，而不是只甩一句「请求失败 (422)」。
   */
  fieldErrors?: Record<string, string>;
  constructor(
    status: number,
    message: string,
    opts?: {
      kind?: ApiErrorKind;
      retryAfter?: number;
      code?: string;
      fieldErrors?: Record<string, string>;
    }
  ) {
    super(message);
    this.status = status;
    this.kind = opts?.kind;
    this.retryAfter = opts?.retryAfter;
    this.code = opts?.code;
    this.fieldErrors = opts?.fieldErrors;
  }
}

/** 后端 slug 校验用的就是这条 pattern：3–64 位，首尾须是字母或数字，中间可带连字符。 */
export const PERSONA_SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/;
export const PERSONA_SLUG_RULE =
  "只能用小写字母、数字和连字符，长度 3–64，且不能以连字符开头或结尾";

/** 后端字段名 → 界面用语；认不出来的直接回退字段名本身，不编标签。 */
const FIELD_LABELS: Record<string, string> = {
  name: "名称",
  slug: "slug",
  system_prompt: "System Prompt",
  opening_message: "开场白",
  model: "底层模型",
  temperature: "温度",
  tags: "标签",
  visibility: "可见性",
  avatar_url: "头像",
  content: "消息内容",
  persona_id: "人设",
  conversation_id: "会话",
};

const labelOf = (field: string) => FIELD_LABELS[field] ?? field;

/**
 * FastAPI 校验错误的单项 → 人话。
 * `loc` 形如 `["body","name"]`，**最后一段才是字段名**（前面是位置，不是字段）。
 * 只认 pydantic 那几种稳定的 `type`，认不出的退回后端原话并保留字段名——
 * 宁可给一句带字段名的英文，也不能把字段丢了。
 */
function describeValidation(item: Record<string, unknown>): { field: string; text: string } {
  const loc = Array.isArray(item.loc) ? item.loc : [];
  const field = typeof loc[loc.length - 1] === "string" ? (loc[loc.length - 1] as string) : "";
  const label = field ? labelOf(field) : "输入";
  const ctx = (item.ctx && typeof item.ctx === "object" ? item.ctx : {}) as Record<string, unknown>;
  const bound = (v: unknown) =>
    typeof v === "number" && Number.isFinite(v) ? String(Math.round(v * 1000) / 1000) : undefined;
  const raw = typeof item.msg === "string" ? item.msg : "格式不正确";

  switch (item.type) {
    case "missing":
      return { field, text: `${label}不能为空` };
    case "string_too_short": {
      const min = Number(ctx.min_length);
      return {
        field,
        text: min > 1 ? `${label}至少 ${min} 个字符` : `${label}不能为空`,
      };
    }
    case "string_too_long": {
      const max = Number(ctx.max_length);
      return {
        field,
        text: Number.isFinite(max) ? `${label}最多 ${max} 个字符` : `${label}内容过长`,
      };
    }
    case "string_pattern_mismatch":
      return {
        field,
        text: field === "slug" ? `slug ${PERSONA_SLUG_RULE}` : `${label}格式不正确：${raw}`,
      };
    case "greater_than":
    case "greater_than_equal":
    case "less_than":
    case "less_than_equal": {
      const type = item.type as string;
      const key = { greater_than: "gt", greater_than_equal: "ge", less_than: "lt", less_than_equal: "le" }[
        type
      ] as string | undefined;
      const v = bound(ctx[key ?? ""]);
      if (v === undefined) return { field, text: `${label}超出允许范围` };
      const word = (type.startsWith("greater") ? "大于" : "小于") + (type.endsWith("_equal") ? "或等于" : "");
      return { field, text: `${label}需${word} ${v}` };
    }
    default:
      return { field, text: `${label}：${raw}` };
  }
}

/** 422 的 detail 数组 → 人话文案 + 按字段归并的错误表。 */
function readValidationDetail(items: unknown[]): { message: string; fieldErrors: Record<string, string> } {
  const described = items
    .filter((i): i is Record<string, unknown> => Boolean(i) && typeof i === "object")
    .map(describeValidation);
  const fieldErrors: Record<string, string> = {};
  for (const d of described) if (d.field && !(d.field in fieldErrors)) fieldErrors[d.field] = d.text;
  const MAX_SHOWN = 3;
  const head = described.slice(0, MAX_SHOWN).map((d) => d.text);
  const rest = described.length - head.length;
  const message = described.length
    ? head.join("；") + (rest > 0 ? `；另有 ${rest} 处不符合要求` : "")
    : "提交的内容不符合要求";
  return { message, fieldErrors };
}

/**
 * 从响应体取「可展示文案 + 机器可判 code」。
 *
 * 三种面形状都要认（已核实后端实现，非猜测）：
 * - 原生面：`{"detail": "...", "code": "rate_limited"}`
 * - 兼容面：`{"error": {"message": "...", "code": "rate_limited"}}`
 * - 校验面（422）：`{"detail": [{"loc": ["body", "name"], "msg": ..., "type": ...}]}`
 *   ⇒ 额外产出 `fieldErrors`，供表单落到具体输入框。
 * 错误体本身可能就是流式里的帧，这里只负责已解析的 JSON 对象。
 */
function readErrorBody(
  body: unknown,
  fallback: string
): { message: string; code?: string; retryAfterS?: number; fieldErrors?: Record<string, string> } {
  if (!body || typeof body !== "object") return { message: fallback };
  const b = body as Record<string, unknown>;
  // FastAPI 的 422 把字段级原因放在 **数组** 里（`{"detail":[{"loc":[…,"name"],…}]}`）；
  // 旧实现只认字符串 detail，于是整条链落到兜底文案，用户只看到「请求失败 (422)」四个字。
  if (Array.isArray(b.detail)) {
    const { message, fieldErrors } = readValidationDetail(b.detail);
    return { message, fieldErrors };
  }
  const err = b.error as Record<string, unknown> | undefined;
  // 兼容面把文案放在 error.message；原生面在顶层 detail
  const message =
    (typeof err?.message === "string" && err.message) ||
    (typeof b.detail === "string" && b.detail) ||
    (typeof err?.detail === "string" && err.detail) ||
    (typeof b.message === "string" && b.message) ||
    fallback;
  // code 可能在顶层（原生面）或 error.code（兼容面）
  const code =
    (typeof err?.code === "string" && err.code) ||
    (typeof b.code === "string" && b.code) ||
    undefined;
  // 流式路径拿不到 Retry-After header（响应头在错误发生前就发完了），退避时长
  // 只能读错误体的 retry_after_s（协议 v1.7-澄清2 的加法字段，有才给）。
  const rawRetry = err?.retry_after_s ?? b.retry_after_s;
  const retryAfterS =
    typeof rawRetry === "number" && Number.isFinite(rawRetry) && rawRetry > 0
      ? Math.round(rawRetry)
      : undefined;
  return { message, code, retryAfterS };
}

/**
 * 服务端 code → 前端语义 + 展示文案（协议 §7 的 code 表）。
 * 优先按 code 判读（机器可判、无歧义）；code 缺失时回退到状态码 + Retry-After 启发式。
 *
 * 为什么要 code 优先：旧约定靠「429 有没有 Retry-After」区分限流与预算耗尽，
 * 而上游 429 常自带 Retry-After —— 一旦平台透传，`budget_exhausted`（重试无用）
 * 会被误判成限流并自动退避重试。协议 §7 明确「旧约定保留一个版本周期后废弃」，
 * 故此处主动切换到 code 优先，同时保留启发式以免后端未发 code 时行为倒退。
 */
function classify(
  status: number,
  code: string | undefined,
  retryAfter: number | undefined,
  message: string,
  fieldErrors?: Record<string, string>
): ApiError {
  const withKind = (kind: ApiErrorKind, text: string, extra?: { retryAfter?: number }) =>
    new ApiError(status, text, { kind, code, fieldErrors, ...extra });

  switch (code) {
    case "rate_limited":
      return withKind(
        "rate_limit",
        retryAfter ? `操作过于频繁，请 ${retryAfter} 秒后再试` : "操作过于频繁，请稍后再试",
        { retryAfter }
      );
    case "budget_exhausted":
      return withKind("quota", "本月额度已用尽，请充值或联系管理员（重试无效）");
    case "service_unavailable":
    case "upstream_error":
      return withKind("service", "服务繁忙，请稍后重试");
    case "context_length_exceeded":
      return withKind("context", "对话上下文过长，请精简后重试（或开新会话）");
    case "compliance_denied":
      return withKind("forbidden", "该模型当前不可用，请更换模型");
    case "model_not_found":
      return withKind("model_missing", "模型不存在或已下线，请刷新模型列表");
  }

  // ---- code 缺失或未知：回退到状态码启发式（保持既有行为）----
  if (status === 402) return withKind("balance", `${message}，请充值或联系管理员`);
  if (status === 429) {
    return retryAfter
      ? withKind("rate_limit", `操作过于频繁，请 ${retryAfter} 秒后再试`, { retryAfter })
      : withKind("quota", "本月额度已用尽，请充值或联系管理员（重试无效）");
  }
  if (status === 503) return withKind("service", "服务繁忙，请稍后重试");
  if (status === 413) return withKind("context", "对话上下文过长，请精简后重试（或开新会话）");
  if (status === 403) return withKind("forbidden", "该模型当前不可用，请更换模型");
  if (status === 404) return withKind("model_missing", "模型不存在或已下线，请刷新模型列表");
  return new ApiError(status, message, { code, fieldErrors });
}

/**
 * 把非 2xx 响应翻译成可展示的错误。
 * 判读顺序：`error.code`（协议 §7）→ 状态码 → `Retry-After` 启发式。
 * 注意：401 的刷新重试逻辑不在此处，且绝不扩展到其他 4xx。
 */
async function interpretError(resp: Response): Promise<ApiError> {
  let message = `请求失败 (${resp.status})`;
  let code: string | undefined;
  let fieldErrors: Record<string, string> | undefined;
  try {
    ({ message, code, fieldErrors } = readErrorBody(await resp.json(), message));
  } catch {
    /* 非 JSON 响应体：保留兜底文案 */
  }
  const ra = resp.headers.get("Retry-After");
  const secs = ra !== null ? Number(ra) : NaN;
  const retryAfter = Number.isFinite(secs) && secs > 0 ? Math.round(secs) : undefined;
  return classify(resp.status, code, retryAfter, message, fieldErrors);
}

async function refreshTokens(): Promise<boolean> {
  const refresh_token = localStorage.getItem(REFRESH_KEY);
  if (!refresh_token) return false;
  const resp = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token }),
  });
  if (!resp.ok) return false;
  saveTokens(await resp.json());
  return true;
}

export async function api<T = unknown>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const resp = await fetch(`${API_BASE}${path}`, { ...init, headers });

  if (resp.status === 401 && !retried && token) {
    if (await refreshTokens()) {
      return api<T>(path, init, true); // 换新 token 重试一次
    }
    clearTokens();
    window.location.href = "/login";
    throw new ApiError(401, "登录已过期");
  }
  if (!resp.ok) {
    throw await interpretError(resp);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

/** 解析 SSE 流（OpenAI chunk 格式），逐个产出 content 增量；`[DONE]` 是唯一正常收尾信号。
 *
 * 宽松解析（协议 §6）：`: keepalive` 注释行与未知命名事件一律忽略——
 * 本实现只认 `data: ` 行，天然满足（模型服务协议会发 `event: model_service`
 * 终止事件与心跳注释，都不应被当成内容或错误）。
 *
 * 流内错误（协议 §6.7，AGENTS「三」a639446）：两面在流中途都会发
 * `data: {"error": {...}}`（HTTP 已是 200，无法再改状态码），其后必随 `[DONE]`。
 * 此时必须抛出，否则错误被静默吞掉、用户只看到空回复；抛出时已产出的部分内容
 * 保留在调用方状态里（并由服务端结算落库）。退避时长只能从错误体的
 * `retry_after_s` 读 —— 响应头早在错误发生前就发完了。
 *
 * 截断判定以「有没有 `[DONE]`」为准，不以「最后一个 chunk 是不是 content」为准：
 * 绝对上限（§8.1.1 stream_max_duration）按协议**不发**错误帧，只给"中断内容 +
 * `[DONE]`"，那是合法收尾，不能当解析失败。
 */
export async function streamSSE(
  resp: Response,
  opts: { onDelta: (text: string) => void }
): Promise<void> {
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  /** 处理一个 SSE 帧；返回 true 表示收到 `[DONE]`（正常收尾）。 */
  const handleFrame = (frame: string): boolean => {
    const line = frame.trim();
    if (!line.startsWith("data: ")) return false;
    const payload = line.slice(6);
    if (payload === "[DONE]") return true;
    let obj: unknown;
    try {
      obj = JSON.parse(payload);
    } catch {
      return false; // 跳过无法解析的帧
    }
    const body = obj as Record<string, unknown> | null;
    if (body && typeof body === "object" && body.error) {
      const { message, code, retryAfterS } = readErrorBody(body, "生成失败");
      // 流已开始，HTTP 状态无从更改：以 200 传入，判读主要由 code 决定
      throw classify(resp.status, code, retryAfterS, message);
    }
    const content = (body?.choices as { delta?: { content?: unknown } }[] | undefined)?.[0]?.delta
      ?.content;
    if (typeof content === "string" && content) opts.onDelta(content);
    return false;
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      if (handleFrame(frame)) return;
    }
  }
  // 连接已关闭：末尾可能残留没有 `\n\n` 终止的帧（`[DONE]` 恰好落在其中时，
  // 不处理它会把正常收尾误判成截断），先冲掉解码器再收尾解析一次。
  buffer += decoder.decode();
  if (handleFrame(buffer)) return;
  throw new ApiError(resp.status, "回复被中断（连接提前结束），请重试", { kind: "truncated" });
}

// ---------- 类型 ----------

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
}

export interface Persona {
  id: string;
  slug: string;
  name: string;
  system_prompt: string;
  opening_message: string | null;
  model: string | null;
  temperature: number | null;
  visibility: string;
  owner_id: string;
  forked_from: string | null;
  tags: string[] | null;
  avatar_url: string | null;
  memory_enabled: boolean;
}

export interface Memory {
  id: string;
  persona_id: string;
  content: string;
  category: string;
  source: string; // chat=自动提取 / manual=手动
  created_at: string;
}

export interface Conversation {
  id: string;
  persona_id: string;
  title: string | null;
  pinned: boolean;
  model: string | null;
  last_message_at: string | null;
  created_at: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  model: string | null;
  created_at: string;
}

export interface UsageByModel {
  model: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface Usage {
  days: number;
  total_requests: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  by_model: UsageByModel[];
}

export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  model_whitelist: string[] | null;
  revoked: boolean;
  last_used_at: string | null;
  created_at: string;
}

export interface ApiKeyCreated extends ApiKey {
  key: string;
}

export interface LlmModel {
  id: string;
  name: string;
  model: string;
  api_base: string | null;
  has_key: boolean;
  enabled: boolean;
  is_default: boolean;
  sort: number;
}

export interface MarketPersona extends Persona {
  owner_name: string | null;
}

export interface AdminModelCreate {
  name: string;
  model: string;
  api_base?: string;
  api_key?: string;
  is_default?: boolean;
  sort?: number;
}

/** 负毛利明细行。cost_billed 是积分、cost_upstream 是 USD，两者不可直接相减。 */
export interface MarginViolationRow {
  id: string;
  user_id: string;
  model: string | null;
  cost_billed: number;
  cost_upstream: number;
  created_at: string | null;
}

export interface MarginSummary {
  requests: number;
  violations: number;
  billed_total: number;
  revenue_usd: number;
  upstream_total: number;
  margin: number;
  /** 上游成本为 0 时后端返回 null（无分母），不可当 0 渲染。 */
  margin_ratio: number | null;
  violations_detail: MarginViolationRow[];
}

// ---------- 业务封装 ----------

export const authApi = {
  async register(email: string, password: string, display_name?: string) {
    const t = await api<Tokens>("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, display_name: display_name || undefined }),
    });
    saveTokens(t);
    return t;
  },
  async login(email: string, password: string) {
    const t = await api<Tokens>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    saveTokens(t);
    return t;
  },
};

export const platformApi = {
  me: () => api<User>("/api/v1/auth/me"),
  updateMe: (body: { display_name?: string; new_password?: string; current_password?: string }) =>
    api<User>("/api/v1/auth/me", { method: "PATCH", body: JSON.stringify(body) }),
  usage: (days = 30) => api<Usage>(`/api/v1/me/usage?days=${days}`),
  wallet: () => api<{ balance: number; lifetime_topup: number }>("/api/v1/me/wallet"),
  personas: () => api<Persona[]>("/api/v1/personas"),
  createPersona: (body: Record<string, unknown>) =>
    api<Persona>("/api/v1/personas", { method: "POST", body: JSON.stringify(body) }),
  updatePersona: (id: string, body: Record<string, unknown>) =>
    api<Persona>(`/api/v1/personas/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deletePersona: (id: string) => api<void>(`/api/v1/personas/${id}`, { method: "DELETE" }),
  conversations: () => api<Conversation[]>("/api/v1/conversations"),
  createConversation: (persona_id: string) =>
    api<Conversation>("/api/v1/conversations", { method: "POST", body: JSON.stringify({ persona_id }) }),
  updateConversation: (id: string, body: { title?: string; pinned?: boolean; model?: string }) =>
    api<Conversation>(`/api/v1/conversations/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteConversation: (id: string) => api<void>(`/api/v1/conversations/${id}`, { method: "DELETE" }),
  messages: (conversationId: string, params?: { limit?: number; before_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.before_id) q.set("before_id", params.before_id);
    const qs = q.toString();
    return api<Message[]>(`/api/v1/conversations/${conversationId}/messages${qs ? `?${qs}` : ""}`);
  },
  keys: () => api<ApiKey[]>("/api/v1/me/keys"),
  createKey: (body: { name: string; model_whitelist?: string[]; expires_at?: string }) =>
    api<ApiKeyCreated>("/api/v1/me/keys", { method: "POST", body: JSON.stringify(body) }),
  revokeKey: (id: string) => api<void>(`/api/v1/me/keys/${id}`, { method: "DELETE" }),
  models: () => api<LlmModel[]>("/api/v1/models"),
  adminModels: () => api<LlmModel[]>("/api/v1/admin/models"),
  createModel: (body: AdminModelCreate) =>
    api<LlmModel>("/api/v1/admin/models", { method: "POST", body: JSON.stringify(body) }),
  updateModel: (id: string, body: Record<string, unknown>) =>
    api<LlmModel>(`/api/v1/admin/models/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteModel: (id: string) => api<void>(`/api/v1/admin/models/${id}`, { method: "DELETE" }),
  adminMargin: (limit = 20) =>
    api<MarginSummary>(`/api/v1/admin/billing/margin?limit=${encodeURIComponent(String(limit))}`),
  adminReconcile: () =>
    api<{ abandoned: number }>("/api/v1/admin/billing/reconcile", { method: "POST" }),
  forkPersona: (id: string) => api<Persona>(`/api/v1/personas/${id}/fork`, { method: "POST" }),
  memories: (personaId: string) => api<Memory[]>(`/api/v1/personas/${personaId}/memories`),
  addMemory: (personaId: string, content: string) =>
    api<Memory>(`/api/v1/personas/${personaId}/memories`, { method: "POST", body: JSON.stringify({ content }) }),
  updateMemory: (id: string, content: string) =>
    api<Memory>(`/api/v1/memories/${id}`, { method: "PATCH", body: JSON.stringify({ content }) }),
  deleteMemory: (id: string) => api<void>(`/api/v1/memories/${id}`, { method: "DELETE" }),
  market: (params?: { q?: string; tag?: string }) => {
    const sp = new URLSearchParams();
    if (params?.q) sp.set("q", params.q);
    if (params?.tag) sp.set("tag", params.tag);
    const qs = sp.toString();
    return api<MarketPersona[]>(`/api/v1/personas/market${qs ? `?${qs}` : ""}`);
  },
};

/** 发送 POST 并消费 SSE 流；401 时自动刷新 token 重试一次。 */
/** 限流退避重发的等待上限。后端滑窗 60s，真触发限流时 Retry-After 通常接近 60
 *  （`ratelimit.py` 取"最老请求过期还需几秒"），所以不能把上限压到几秒，否则形同禁用。
 *  等待期间通过 onNotice 反馈进度，不让用户对着 loading 干等。 */
const MAX_AUTO_BACKOFF_SECONDS = 60;

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    // 已经 abort 的 signal 不会再发第二次 "abort" 事件，所以必须先查当前状态：
    // 只挂监听会让"用户在退避等待期间点停止"变成"白等满 60 秒，然后照样重发一次"。
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function postSSE(
  path: string,
  body: unknown,
  onDelta: (t: string) => void,
  signal?: AbortSignal,
  retried = false,
  backoffRetried = false,
  onNotice?: (msg: string) => void
): Promise<void> {
  const doFetch = async (): Promise<Response> => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    return fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal,
    });
  };

  let resp = await doFetch();
  if (resp.status === 401 && !retried && (await refreshTokens())) {
    resp = await doFetch(); // 换新 token 重试一次
  }
  if (resp.status === 401) {
    clearTokens();
    window.location.href = "/login";
    throw new ApiError(401, "登录已过期");
  }
  if (!resp.ok) {
    const err = await interpretError(resp);
    // 只重发「平台自身 RPM 限流」这一种 429：它由 rate_limit_native 依赖抛出，
    // 早于 ensure_native_admitted 与 send_message（conversations.py:107→111→124），
    // 此时用户消息尚未落库，重发安全。其响应体为 {"detail": "rate limit exceeded"}，无 code。
    // 而带 code=rate_limited 的 429 是模型服务/上游限流经原生面透传
    // （main.py:180-182），发生在阶段A 之后——重发会把用户消息再落一遍。
    const isOwnRpmLimit = err.kind === "rate_limit" && err.code === undefined;
    const secs = err.retryAfter ?? 0;
    if (isOwnRpmLimit && !backoffRetried && secs > 0 && secs <= MAX_AUTO_BACKOFF_SECONDS) {
      onNotice?.(`请求过于频繁，${secs} 秒后自动重试…`);
      await sleep(secs * 1000, signal);
      return postSSE(path, body, onDelta, signal, retried, true, onNotice);
    }
    throw err;
  }
  await streamSSE(resp, { onDelta });
}

export async function sendMessageStream(
  conversationId: string,
  content: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
  onNotice?: (msg: string) => void
): Promise<void> {
  await postSSE(
    `/api/v1/conversations/${conversationId}/messages`,
    { content, stream: true },
    onDelta,
    signal,
    false,
    false,
    onNotice
  );
}

/** 重新生成最后一条助手回复（SSE）。 */
export async function regenerateStream(
  conversationId: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
  onNotice?: (msg: string) => void
): Promise<void> {
  await postSSE(
    `/api/v1/conversations/${conversationId}/regenerate`,
    {},
    onDelta,
    signal,
    false,
    false,
    onNotice
  );
}
