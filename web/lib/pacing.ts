/**
 * 自适应节奏缓冲（W-4 §1-2）：把「上游到货」与「屏幕上放字」解耦。
 *
 * 问题（架构师 2026-10-13 实测）：上游首块 +11.97s、正文 4 块在 0.61s 内吐完
 * ⇒ 用户看到的是「干等 12 秒 → 0.6 秒内全部砸出来」。流式链路本身没问题，
 * 坏在**上游的节奏不是人看的节奏**。
 *
 * 两条用户要求，正好是同一枚硬币的两面：
 * 1. 上游**慢** ⇒ 按真实速度显示，不额外加延迟（队列几乎总是空的）；
 * 2. 上游**快** ⇒ 压速，像真人打字（有积压才放慢，积压消化完立刻回到真实速度）。
 *
 * ❗ 用户的硬约束：**速率必须由积压字符数连续决定，不许设阈值/分档**。
 * 所以本文件的速率函数是**一个 Math.exp，没有 if**（见 releaseRate）。任何
 * `if (q > N) speed = …` 形式的写法都是把这个需求做错。
 *
 * ── 参数怎么来的（不是拍的）──
 *
 *    速率 r(q) = BASE · e^(q/K)        q = 积压字符数
 *
 * 反解积压 q 从 q₀ 排空所需时间（d q/dt = −r(q)，u = e^(−q/K) ⇒ du/dt = BASE/K）：
 *
 *    T(q₀) = (K/BASE) · (1 − e^(−q₀/K))  <  K/BASE
 *
 * **T 有上界 K/BASE，与积压多少无关** —— 这是选参数最要紧的一条：只慢不快会让
 * 「压速」变成新问题（用户等几分钟），而这条性质保证**缓冲最多只加 K/BASE 秒**。
 *
 * 取 BASE = 30 字/秒、K = 360 字 ⇒ 上界 12 秒：
 *   - 积压 0        → 30 字/秒 ≈ 快速但不失真人的打字/阅读节奏；
 *   - 积压 360(=K)  → 30e ≈ 82 字/秒（×e）；
 *   - 积压 1000     → 30e^2.78 ≈ 483 字/秒；
 *   - 积压 2000     → 30e^5.56 ≈ 7800 字/秒（秒级追平，不会越积越多）；
 *   - 300 字一次性到达 → T = 12(1−e^−0.833) ≈ 6.8s（「明显慢于真实到达」）；
 *   - 1 字/100ms 的慢上游 → 到货 10 字/秒 ≪ 30 字/秒，队列恒空 ⇒ 显示即真实速度。
 *
 * 也就是说：**上界由 K/BASE 决定、低速端由 BASE 决定**，两个参数各有明确职责，
 * 改一个不会把另一个性质弄坏。mutation 检验见 web/tests/pacing.test.mjs。
 */

/** 积压趋近 0 时的释放速率（字/秒）——即「上游慢」时用户看到的真实速度上限。 */
export const PACING_BASE_RATE = 30;

/** 积压字符数的特征尺度：积压每 +K，速率 ×e（连续，无档位）。 */
export const PACING_BACKLOG_K = 360;

/** 缓冲最多给单条回复额外增加的显示时长（秒）：T(q) 的渐近上界 K/BASE。 */
export const PACING_MAX_EXTRA_SECONDS = PACING_BACKLOG_K / PACING_BASE_RATE;

/**
 * 「思考中」多久之后改口成「仍在等待模型…」（毫秒）。
 * 依据：本单实测的真实首块 +11.97s、首个内容块 +12.39s，取 ≈3.7× 余量。
 * 只是**换文案**，不是报错 —— 长推理是合法状态，超时不该被当成失败。
 */
export const THINKING_SLOW_AFTER_MS = 45_000;

/**
 * 释放速率（字/秒）：**积压字符数的连续函数**，唯一自变量就是 q。
 *
 * r(q) = BASE · e^(q/K)。刻意不写任何 `if`/分档/夹取：
 *   - q = 0 ⇒ 恰为 BASE（慢上游时显示速度 = 真实到货速度）；
 *   - 单调递增、处处连续（单测锁死）；
 *   - 无上界 ⇒ 积压极大时秒级追平，不会让用户干等。
 */
export function releaseRate(backlog: number): number {
  return PACING_BASE_RATE * Math.exp(backlog / PACING_BACKLOG_K);
}

/**
 * 把 q₀ 个字符按 r(q) 排空所需秒数（闭式解，用于推算与文档，不参与运行期循环）。
 * T(q₀) = (K/BASE)·(1 − e^(−q₀/K))，恒 < K/BASE。
 */
export function drainSeconds(backlog: number): number {
  return PACING_MAX_EXTRA_SECONDS * (1 - Math.exp(-backlog / PACING_BACKLOG_K));
}

/**
 * 取不劈开代理对的截断长度：n 落在代理对中间时退回 1 个 UTF-16 单元。
 * 少了这一步，emoji 会被切成两半渲染成乱码方块。
 */
export function safeCut(text: string, maxUnits: number): number {
  let n = maxUnits >= text.length ? text.length : Math.max(0, Math.floor(maxUnits));
  if (n > 0 && n < text.length) {
    const unit = text.charCodeAt(n - 1);
    if (unit >= 0xd800 && unit <= 0xdbff) n -= 1; // 落点前一个是高代理 ⇒ 别从对中间切
  }
  return n;
}

/**
 * `prefers-reduced-motion: reduce` 是否为真 ⇒ 关闭缓冲、实时显示。
 * 这是无障碍要求（WCAG 2.3.3 / 用户明确不想要动画），不是可选项。
 * 用 globalThis 取 matchMedia：浏览器里它就是 window.matchMedia，Node 里缺失即 false。
 */
export function prefersReducedMotion(): boolean {
  const mm = (globalThis as { matchMedia?: (q: string) => { matches: boolean } }).matchMedia;
  if (typeof mm !== "function") return false;
  try {
    return mm("(prefers-reduced-motion: reduce)").matches === true;
  } catch {
    return false;
  }
}

/**
 * 流结束/中断后与服务端消息列表对齐（判据 R1/R3 的收尾那一半）。
 *
 * 为什么不能直接 `setMessages(await api.messages(...))`：服务端**只在正常走到
 * StreamDone 时**才落助手消息（`server/app/services/chat.py` 的 `_settle` 只结算计费、
 * 不落消息），所以「客户端中断/上游报错」这条路上，服务端列表里根本没有这条回复
 * —— 直接覆盖会把用户刚看到的内容整条抹掉。故取两侧的**较长者**：
 *   - 服务端末尾已是助手消息且不比本地短 ⇒ 用服务端（权威版本，含完整落库文本）；
 *   - 否则把本地这条（已 flush 的）接回去。
 * 本地 `content` 恒为上游文本的前缀，所以「更长即更完整」这个比较是成立的。
 */
export function reconcileTail<T extends { role: string; content: string }>(
  server: T[],
  local: T | null
): T[] {
  if (!local || !local.content) return server;
  const last = server[server.length - 1];
  if (last && last.role === "assistant" && last.content.length >= local.content.length) return server;
  return [...server, local];
}


type Handle = number;

export interface PacedRevealOptions {
  /** 显示层增长回调（参数是本次新增文本）。**dispose 之后不会再被调用**。 */
  onReveal: (chunk: string) => void;
  /** true = 不做缓冲、来多少显示多少（prefers-reduced-motion 为真时用）。 */
  immediate?: boolean;
  /** 调度器可注入，默认 requestAnimationFrame（无 rAF 时退化为 setTimeout）。 */
  schedule?: (cb: () => void) => Handle;
  cancel?: (handle: Handle) => void;
  now?: () => number;
}

const hasRaf = typeof requestAnimationFrame === "function";
const defaultSchedule = (cb: () => void): Handle =>
  hasRaf ? requestAnimationFrame(cb) : (setTimeout(cb, 16) as unknown as Handle);
const defaultCancel = (h: Handle): void => {
  if (hasRaf) cancelAnimationFrame(h);
  else clearTimeout(h as unknown as ReturnType<typeof setTimeout>);
};
/** performance.now 优先（单调时钟，不受系统时间调整影响），无则退回首屏时间戳。 */
const defaultNow = (): number => {
  const perf = (globalThis as { performance?: { now?: () => number } }).performance;
  return typeof perf?.now === "function" ? perf.now() : Date.now();
};

/** 单帧最多按多少毫秒结算：切回前台时别把「睡着的 60 秒」当成一帧全砸出去。 */
const MAX_STEP_SECONDS = 0.25;

/**
 * 节奏释放器：上游 `push()` 文本入队，自己按 r(积压) 逐帧往外放。
 *
 * 生命周期契约（每条都有对应单测）：
 * - 单个循环驱动（自己重排下一帧），**不是每个 delta 起一个定时器**；
 * - `end()` 上游结束 ⇒ 队列排空后 `whenDrained()` 兑现（refetch 必须等它，见 R3）；
 * - `flush()` 停止缓冲并把剩余**一次性**显示（点「停止」走这条）；
 * - `dispose()` 卸载/切会话：停循环、丢弃剩余、**不再回调 onReveal**（防幽灵更新）。
 *   `dispose()` 也会兑现 `whenDrained()`，避免 await 它的调用方永久挂住。
 */
export class PacedReveal {
  #onReveal: (chunk: string) => void;
  #schedule: (cb: () => void) => Handle;
  #cancel: (handle: Handle) => void;
  #now: () => number;
  #immediate: boolean;

  #pending = "";
  #carry = 0; // 不足一字的零头：r=30 字/秒、16ms 一帧时每帧只有 0.48 字，不留零头会一个字都放不出来
  #handle: Handle | null = null;
  #last = 0;
  #ended = false;
  #disposed = false;
  #revealed = "";
  #drained = false;
  #promise: Promise<void> | null = null;
  #resolveDrained: (() => void) | null = null;

  constructor(opts: PacedRevealOptions) {
    this.#onReveal = opts.onReveal;
    this.#immediate = opts.immediate === true;
    this.#schedule = opts.schedule ?? defaultSchedule;
    this.#cancel = opts.cancel ?? defaultCancel;
    this.#now = opts.now ?? defaultNow;
  }

  /** 已到货但尚未显示的字符数（= 速率函数的唯一自变量）。 */
  get backlog(): number {
    return this.#pending.length;
  }

  /** 已显示出来的文本（测试与自证用）。 */
  get revealed(): string {
    return this.#revealed;
  }

  /** 上游是否已结束（不代表已放完）。 */
  get ended(): boolean {
    return this.#ended;
  }

  /** 上游到货。 */
  push(text: string): void {
    if (this.#disposed || !text) return;
    if (this.#immediate) {
      // 无障碍：用户明确不想要动画 ⇒ 直接实时显示
      this.#revealed += text;
      this.#onReveal(text);
      if (this.#ended) this.#settle();
      return;
    }
    this.#pending += text;
    this.#start();
  }

  /** 上游结束（收尾的 `[DONE]` 之后）。还要把队列放完才算 drained。 */
  end(): void {
    if (this.#disposed) return;
    this.#ended = true;
    if (this.#pending.length === 0) this.#settle();
    else this.#start();
  }

  /** 立刻停止缓冲，把剩余内容**一次性**显示齐全（点「停止」/ 异常收尾）。 */
  flush(): void {
    if (this.#disposed) return;
    this.#stop();
    const rest = this.#pending;
    this.#pending = "";
    this.#carry = 0;
    this.#immediate = true; // 之后即使还有迟到到货，也不许再慢慢放
    if (rest) {
      this.#revealed += rest;
      this.#onReveal(rest);
    }
    this.#settle();
  }

  /** 卸载/切会话：停循环 + 丢弃剩余 + 之后不再回调。 */
  dispose(): void {
    if (this.#disposed) return;
    this.#disposed = true;
    this.#stop();
    this.#pending = "";
    this.#carry = 0;
    this.#settle();
  }

  /** 队列排空（且上游已结束）或 flush/dispose 之后兑现。 */
  whenDrained(): Promise<void> {
    // 已经排空/冲刷/卸载过 ⇒ 立刻兑现。
    // ⚠️ 少了这个判断就是死等：`flush()` 早于 `whenDrained()` 时，兑现点已经过去，
    // 再新建的 Promise 永远没人 resolve —— 实测表现是**整个发送流程卡在流式态**
    // （「停止」按钮再也不变回来，refetch 永不发生）。发送路径里 flush 先于
    // whenDrained 是常态（错误分支就是这么写的），所以这条不是理论边界。
    if (this.#drained) return Promise.resolve();
    if (!this.#promise) {
      this.#promise = new Promise<void>((resolve) => {
        this.#resolveDrained = resolve;
      });
    }
    return this.#promise;
  }

  #settle(): void {
    if (this.#drained) return;
    this.#drained = true;
    const done = this.#resolveDrained;
    this.#resolveDrained = null;
    done?.();
  }

  #start(): void {
    if (this.#disposed || this.#handle !== null) return;
    this.#last = this.#now();
    this.#handle = this.#schedule(this.#tick);
  }

  #stop(): void {
    if (this.#handle === null) return;
    this.#cancel(this.#handle);
    this.#handle = null;
  }

  #tick = (): void => {
    this.#handle = null;
    if (this.#disposed) return;
    const now = this.#now();
    const dt = Math.min(Math.max(now - this.#last, 0), MAX_STEP_SECONDS * 1000) / 1000;
    this.#last = now;

    const q = this.#pending.length;
    const budget = releaseRate(q) * dt + this.#carry;
    const n = Math.max(0, Math.min(q, Math.floor(budget)));
    // 零头按**实际放出的单元数**扣减，不是按"本帧想放多少"扣：
    // 截断点落在代理对中间时这一帧会少放（cut < n），差额必须留在零头里，
    // 否则等于把这部分预算丢掉，队列永远追不上（实测：200 个 emoji 放 20 秒只出 68 个）。
    // 也正是"零头只增不减"才让"攒够 2 个单元再整对放"成立。
    let cut = 0;
    if (n > 0) cut = safeCut(this.#pending, n);
    this.#carry = Math.max(0, budget - cut);
    if (cut > 0) {
      const chunk = this.#pending.slice(0, cut);
      this.#pending = this.#pending.slice(cut);
      this.#revealed += chunk;
      this.#onReveal(chunk);
    }
    if (this.#pending.length === 0) {
      this.#carry = 0;
      if (this.#ended) this.#settle();
      return;
    }
    this.#start(); // 单循环自我重排
  };
}
