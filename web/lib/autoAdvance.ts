// 自动推进（auto-advance）：模型回复结束后，前端替用户自动发送预设消息，让剧情继续。
//
// 设计（v1，纯前端零后端）：
// - 规则与轮次计数都存 localStorage，刷新不丢。
// - 计数语义：round = 「开启自动推进后成功完成的模型回复数」，从开启时从 1 起算。
//   每轮结束后对每条规则取模：round % interval === 0 ⇒ 该规则到期。
//   多条规则同时到期 ⇒ 合并成一条用户消息（按列表顺序拼接），只产生一个新回合。
//   例：①「继续」interval=1、②「继续推进剧情」interval=5 ⇒ 第 1,2,3,4 轮发「继续」，
//   第 5 轮发「继续\n继续推进剧情」，第 6 轮起又只有「继续」，第 10 轮再合并，如此循环。
// - 只在正常收尾后触发；出错/截断/用户点停止 ⇒ 停止推进（计数不清，重开后接着算）。

export interface AutoAdvanceRule {
  id: string;
  /** 触发后自动发送的内容。 */
  content: string;
  /** 每 N 轮触发一次；N 为 ≥1 的整数。 */
  interval: number;
  enabled: boolean;
}

export interface AutoAdvanceState {
  rules: AutoAdvanceRule[];
  /** 自动推进开关。 */
  enabled: boolean;
}

/** localStorage 读写都按「每会话」隔离：不同会话各有各的规则与开关。 */
export const AUTO_ADVANCE_KEY_PREFIX = "autoAdvance:v1:conv:";

export function storageKey(conversationId: string): string {
  return `${AUTO_ADVANCE_KEY_PREFIX}${conversationId}`;
}

/** 轮次计数用独立 key：关掉再开 ⇒ 计数清零重算（语义最直白）。 */
export function counterKey(conversationId: string): string {
  return `${AUTO_ADVANCE_KEY_PREFIX}${conversationId}:round`;
}

function safeInt(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v) && Number.isInteger(v) && v >= 1) return v;
  if (typeof v === "string" && /^-?\d+$/.test(v.trim())) {
    const n = Number(v);
    if (Number.isInteger(n) && n >= 1) return n;
  }
  return null;
}

/** 恢复单条规则；坏了就丢（不 throw，坏数据不能挡住整个恢复）。 */
function reviveRule(raw: unknown): AutoAdvanceRule | null {
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Record<string, unknown>;
  const content = typeof r.content === "string" ? r.content : "";
  const interval = safeInt(r.interval);
  if (!content.trim() || interval === null) return null;
  return {
    id: typeof r.id === "string" && r.id ? r.id : `r-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    content,
    interval,
    enabled: r.enabled !== false,
  };
}

/** 恢复整份状态；任何一层坏了都降级成「关闭 + 空规则」，不 throw。 */
export function reviveState(raw: string | null): AutoAdvanceState {
  const empty: AutoAdvanceState = { rules: [], enabled: false };
  if (!raw) return empty;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return empty;
  }
  if (typeof parsed !== "object" || parsed === null) return empty;
  const obj = parsed as Record<string, unknown>;
  const rules = Array.isArray(obj.rules)
    ? (obj.rules.map(reviveRule).filter((r): r is AutoAdvanceRule => r !== null))
    : [];
  return { rules, enabled: obj.enabled === true };
}

/** 落盘。 */
export function persistState(conversationId: string, state: AutoAdvanceState): void {
  try {
    localStorage.setItem(storageKey(conversationId), JSON.stringify(state));
  } catch {
    // 隐身模式/存储超额：设置丢了不致命，静默。
  }
}

/** 读盘。 */
export function loadState(conversationId: string): AutoAdvanceState {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(storageKey(conversationId));
  } catch {
    return { rules: [], enabled: false };
  }
  return reviveState(raw);
}

/** 读轮次计数；没有记录 ⇒ 0（表示还没推进过）。 */
export function loadRound(conversationId: string): number {
  try {
    const raw = localStorage.getItem(counterKey(conversationId));
    if (!raw) return 0;
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : 0;
  } catch {
    return 0;
  }
}

/** 写轮次计数。 */
export function saveRound(conversationId: string, round: number): void {
  try {
    localStorage.setItem(counterKey(conversationId), String(Math.max(0, Math.floor(round))));
  } catch {
    // 同上，静默。
  }
}

/** 清空某会话的推进状态。 */
export function clearState(conversationId: string): void {
  try {
    localStorage.removeItem(storageKey(conversationId));
  } catch {
    // 静默
  }
}

/**
 * 本轮哪些规则到期？纯函数，单测的锚点。
 *
 * - 只有 enabled 的规则参与；
 * - round < 1 ⇒ 永不到期（还没有「已完成回复」可言）；
 * - round % interval === 0 ⇒ 到期；
 * - 到期内容按数组顺序返回（合并交给 mergeDueMessage）。
 */
export function pickDueRules(rules: AutoAdvanceRule[], round: number): string[] {
  if (round < 1) return [];
  const due: string[] = [];
  for (const rule of rules) {
    if (!rule.enabled) continue;
    if (rule.interval < 1) continue;
    if (round % rule.interval === 0) due.push(rule.content);
  }
  return due;
}

/** 把到期内容合并成一条待发送消息（按序、去空白、同内容去重）。 */
export function mergeDueMessage(contents: string[]): string {
  const seen = new Set<string>();
  const lines: string[] = [];
  for (const c of contents) {
    const t = c.trim();
    if (!t || seen.has(t)) continue;
    seen.add(t);
    lines.push(t);
  }
  return lines.join("\n");
}

/** 表单输入 → 规则列表的清洗：interval 非法降级为 1，空内容丢弃。 */
export function sanitizeRules(
  inputs: Array<{ content: string; interval: number | string; enabled?: boolean }>,
): AutoAdvanceRule[] {
  const rules: AutoAdvanceRule[] = [];
  inputs.forEach((item, i) => {
    const interval = safeInt(item.interval);
    const content = typeof item.content === "string" ? item.content.trim() : "";
    if (!content || interval === null) return;
    rules.push({
      id: `r-${i}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      content,
      interval,
      enabled: item.enabled !== false,
    });
  });
  return rules;
}
