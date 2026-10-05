/** 平台 API 客户端：token 管理、401 自动刷新重试、SSE 解析。 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

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

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
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
    let detail = `请求失败 (${resp.status})`;
    try {
      const body = await resp.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* 保留默认消息 */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

/** 解析 SSE 流（OpenAI chunk 格式），逐个产出 content 增量；[DONE] 结束。 */
export async function streamSSE(resp: Response, onDelta: (text: string) => void): Promise<void> {
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
      try {
        const obj = JSON.parse(payload);
        const content = obj?.choices?.[0]?.delta?.content;
        if (typeof content === "string" && content) onDelta(content);
      } catch {
        /* 跳过无法解析的帧 */
      }
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

export interface AdminModelCreate {
  name: string;
  model: string;
  api_base?: string;
  api_key?: string;
  is_default?: boolean;
  sort?: number;
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
  forkPersona: (id: string) => api<Persona>(`/api/v1/personas/${id}/fork`, { method: "POST" }),
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
    let detail = `请求失败 (${resp.status})`;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* keep */
    }
    throw new ApiError(resp.status, detail);
  }
  await streamSSE(resp, onDelta);
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
