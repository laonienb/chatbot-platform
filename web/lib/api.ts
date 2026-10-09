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
 */
export type ApiErrorKind =
  | "balance"
  | "quota"
  | "rate_limit"
  | "service"
  | "context"
  | "forbidden"
  | "model_missing";

export class ApiError extends Error {
  status: number;
  kind?: ApiErrorKind;
  /** 仅 rate_limit：限流退避秒数（来自 Retry-After） */
  retryAfter?: number;
  /** 平台 / 模型服务的机器可判语义码（协议 §7），无则为 undefined */
  code?: string;
  constructor(
    status: number,
    message: string,
    opts?: { kind?: ApiErrorKind; retryAfter?: number; code?: string }
  ) {
    super(message);
    this.status = status;
    this.kind = opts?.kind;
    this.retryAfter = opts?.retryAfter;
    this.code = opts?.code;
  }
}

/**
 * 从响应体取「可展示文案 + 机器可判 code」。
 *
 * 两种面形状都要认（已核实后端实现，非猜测）：
 * - 原生面：`{"detail": "...", "code": "rate_limited"}`
 * - 兼容面：`{"error": {"message": "...", "code": "rate_limited"}}`
 * 错误体本身可能就是流式里的帧，这里只负责已解析的 JSON 对象。
 */
function readErrorBody(body: unknown, fallback: string): { message: string; code?: string } {
  if (!body || typeof body !== "object") return { message: fallback };
  const b = body as Record<string, unknown>;
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
  return { message, code };
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
  message: string
): ApiError {
  const withKind = (kind: ApiErrorKind, text: string, extra?: { retryAfter?: number }) =>
    new ApiError(status, text, { kind, code, ...extra });

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
  return new ApiError(status, message, { code });
}

/**
 * 把非 2xx 响应翻译成可展示的错误。
 * 判读顺序：`error.code`（协议 §7）→ 状态码 → `Retry-After` 启发式。
 * 注意：401 的刷新重试逻辑不在此处，且绝不扩展到其他 4xx。
 */
async function interpretError(resp: Response): Promise<ApiError> {
  let message = `请求失败 (${resp.status})`;
  let code: string | undefined;
  try {
    ({ message, code } = readErrorBody(await resp.json(), message));
  } catch {
    /* 非 JSON 响应体：保留兜底文案 */
  }
  const ra = resp.headers.get("Retry-After");
  const secs = ra !== null ? Number(ra) : NaN;
  const retryAfter = Number.isFinite(secs) && secs > 0 ? Math.round(secs) : undefined;
  return classify(resp.status, code, retryAfter, message);
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

/** 解析 SSE 流（OpenAI chunk 格式），逐个产出 content 增量；[DONE] 结束。
 *
 * 宽松解析（协议 §6）：`: keepalive` 注释行与未知命名事件一律忽略——
 * 本实现只认 `data: ` 行，天然满足（模型服务协议会发 `event: model_service`
 * 终止事件与心跳注释，都不应被当成内容或错误）。
 *
 * 流内错误：兼容面在流中途会发 `data: {"error": {...}}`（HTTP 已是 200，
 * 无法再改状态码）。此时必须抛出，否则错误被静默吞掉、用户只看到空回复。
 */
export async function streamSSE(
  resp: Response,
  opts: { onDelta: (text: string) => void }
): Promise<void> {
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6);
      if (payload === "[DONE]") return;
      let obj: unknown;
      try {
        obj = JSON.parse(payload);
      } catch {
        continue; // 跳过无法解析的帧
      }
      const body = obj as Record<string, unknown> | null;
      if (body && typeof body === "object" && body.error) {
        const { message, code } = readErrorBody(body, "生成失败");
        // 流已开始，HTTP 状态无从更改：以 200 传入，判读主要由 code 决定
        throw classify(resp.status, code, undefined, message);
      }
      const content = (body?.choices as { delta?: { content?: unknown } }[] | undefined)?.[0]?.delta
        ?.content;
      if (typeof content === "string" && content) opts.onDelta(content);
    }
  }
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
async function postSSE(
  path: string,
  body: unknown,
  onDelta: (t: string) => void,
  signal?: AbortSignal,
  retried = false
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
    throw await interpretError(resp);
  }
  await streamSSE(resp, { onDelta });
}

export async function sendMessageStream(
  conversationId: string,
  content: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal
): Promise<void> {
  await postSSE(
    `/api/v1/conversations/${conversationId}/messages`,
    { content, stream: true },
    onDelta,
    signal
  );
}

/** 重新生成最后一条助手回复（SSE）。 */
export async function regenerateStream(
  conversationId: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal
): Promise<void> {
  await postSSE(`/api/v1/conversations/${conversationId}/regenerate`, {}, onDelta, signal);
}
