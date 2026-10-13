"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AutoAdvanceRule,
  type AutoAdvanceState,
  loadRound,
  loadState,
  mergeDueMessage,
  persistState,
  pickDueRules,
  sanitizeRules,
  saveRound,
} from "@/lib/autoAdvance";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { createPortal } from "react-dom";
import Markdown from "@/components/Markdown";
import {
  ApiError,
  clearTokens,
  getAccessToken,
  PERSONA_SLUG_PATTERN,
  PERSONA_SLUG_RULE,
  platformApi,
  regenerateStream,
  sendMessageStream,
  type Conversation,
  type LlmModel,
  type Message,
  type Persona,
  type User,
} from "@/lib/api";
import { downloadText, fetchAllMessages, safeFilename, toJson, toMarkdown } from "@/lib/export";
import {
  PacedReveal,
  reconcileTail,
  prefersReducedMotion,
  THINKING_SLOW_AFTER_MS,
} from "@/lib/pacing";
import { applyTheme, getTheme, type Theme } from "@/lib/theme";
import MemoryModal from "@/components/MemoryModal";
import Select from "@/components/Select";
import Avatar from "@/components/Avatar";
import {
  IconBrain,
  IconCopy,
  IconDownload,
  IconGear,
  IconGauge,
  IconMenu,
  IconPencil,
  IconPin,
  IconRefresh,
  IconStop,
} from "@/components/Icons";

const PAGE_SIZE = 20;

/** 人设头像 emoji 预设（avatar_url 里存非 URL 字符串时按 emoji 渲染） */
const AVATAR_EMOJIS = [
  "🧙", "🧚", "🥷", "👸", "🤴", "🧛", "🧜", "🧑‍🎓",
  "👩‍🏫", "🧑‍💻", "👨‍🍳", "🧑‍🚀", "🤖", "🐉", "🦊", "🐱",
  "🐼", "🦉", "🐺", "🦄", "🎭", "🎨", "📚", "🎵",
  "🍵", "🌸", "🌊", "🌙", "⭐", "🔥", "⚽", "🀄",
];

export default function ChatPage() {
  const router = useRouter();
  const [me, setMe] = useState<User | null>(null);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [models, setModels] = useState<LlmModel[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [convSearch, setConvSearch] = useState("");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  /** 本次流是否已经收到过内容/终态：用来决定气泡里是否还显示「思考中」。 */
  const [replyStarted, setReplyStarted] = useState(false);
  /** 「思考中」持续过久 ⇒ 换文案（不报错，长推理是合法状态）。 */
  const [slowThinking, setSlowThinking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [personaModal, setPersonaModal] = useState<
    { mode: "create" } | { mode: "edit"; persona: Persona } | null
  >(null);
  const [showSettings, setShowSettings] = useState(false);
  const [memoryModalFor, setMemoryModalFor] = useState<Persona | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportPos, setExportPos] = useState<{ left: number; top: number } | null>(null);
  const exportBtnRef = useRef<HTMLButtonElement>(null);
  const exportRootRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  /** 当前这条回复的节奏缓冲（null = 未在流式，或 reduced-motion 下不需要缓冲）。 */
  const pacingRef = useRef<PacedReveal | null>(null);
  /** 显示层已放出的正文。refetch 与「停止」都以它为准，故必须与 setMessages 同步维护。 */
  const pacedTextRef = useRef("");
  /** 当前这条临时气泡的 id：收尾对齐时要确认它还在末尾（切会话后不能乱接）。 */
  const streamBubbleIdRef = useRef<string | null>(null);

  // ---- 自动推进（auto-advance）----
  // 规则存 localStorage（按会话隔离），轮次计数也存 localStorage。
  // autoStateRef 是唯一真源：闭包里读它，避免 setState 时序问题。
  const autoStateRef = useRef<AutoAdvanceState>({ rules: [], enabled: false });
  const [autoRules, setAutoRules] = useState<AutoAdvanceRule[]>([]);
  const [autoEnabled, setAutoEnabled] = useState(false);
  const [autoRound, setAutoRound] = useState(0);
  const [autoPanelOpen, setAutoPanelOpen] = useState(false);
  /** 正在等待自动推进触发的会话；null = 没有挂起的自动发送。 */
  const autoTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 会话切换/挂载时，把该会话的推进状态从 localStorage 恢复到内存。 */
  const syncAutoState = useCallback((conversationId: string) => {
    const s = loadState(conversationId);
    autoStateRef.current = s;
    setAutoRules(s.rules);
    setAutoEnabled(s.enabled);
    setAutoRound(loadRound(conversationId));
  }, []);

  /** 把内存里的规则写回 localStorage（持久化入口）。 */
  const commitAutoState = useCallback(
    (conversationId: string, next: AutoAdvanceState) => {
      autoStateRef.current = next;
      persistState(conversationId, next);
      setAutoRules(next.rules);
      setAutoEnabled(next.enabled);
    },
    []
  );

  const toggleAuto = useCallback(
    (conversationId: string, on: boolean) => {
      if (on) saveRound(conversationId, 0), setAutoRound(0); // 开启 ⇒ 从 0 重新计数
      const next = { ...autoStateRef.current, enabled: on };
      commitAutoState(conversationId, next);
    },
    [commitAutoState]
  );

  /**
   * 回复正常收尾后的推进入口。只在 send() 的成功路径调用。
   * - 计数 +1；
   * - 取到期的规则合并成一条消息，挂一个 setTimeout 触发发送；
   * - 任何情况下不 throw（推进失败不能影响正常收尾）。
   */
  const maybeAutoAdvance = useCallback(
    (conversationId: string) => {
      try {
        const s = autoStateRef.current;
        if (!s.enabled) return;
        const round = loadRound(conversationId) + 1;
        saveRound(conversationId, round);
        setAutoRound(round);
        const due = mergeDueMessage(pickDueRules(s.rules, round));
        if (!due) return;
        // 延迟一拍：让收尾的 setStreaming(false) 先提交，避免 send() 里
        // `if (streaming) return` 的守卫读到的还是旧值。
        autoTimerRef.current = setTimeout(() => {
          autoTimerRef.current = null;
          void autoSendRef.current?.(due);
        }, 50);
      } catch {
        // 推进出错静默：功能坏了不能把聊天也弄坏。
      }
    },
    []
  );

  /** send() 的稳定引用（见下）：自动推进不经过用户输入框，直接以此内容发送。 */
  const autoSendRef = useRef<((content: string) => Promise<void>) | null>(null);
  useEffect(
    () => () => {
      // 卸载时清掉挂起的自动发送，避免打在已卸载组件上。
      if (autoTimerRef.current) clearTimeout(autoTimerRef.current);
    },
    []
  );

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));
  }, []);

  /** 结束节奏缓冲：停循环、丢弃剩余、此后不再回调（卸载/切会话/开新流都用它）。 */
  const disposePacing = useCallback(() => {
    pacingRef.current?.dispose();
    pacingRef.current = null;
  }, []);

  /**
   * 把显示层已放出的正文写回末尾气泡。
   *
   * **不能用「把新 delta 追加到气泡当前内容」那种写法**：那样放出的量取决于
   * setMessages 的调用次数，而缓冲是逐帧推进的 —— 重渲染的批次数一变，
   * 实际显示速度就跟着变（React 会把同一 tick 内的两次更新合并）。
   * 这里一律写成 `已放出的完整前缀`，显示内容就与渲染时机完全解耦。
   */
  const revealPaced = useCallback((chunk: string) => {
    if (!chunk) return;
    pacedTextRef.current += chunk;
    const full = pacedTextRef.current;
    const id = streamBubbleIdRef.current;
    setMessages((prev) => {
      if (!id) return prev;
      const idx = prev.findIndex((m) => m.id === id);
      if (idx === -1) return prev; // 气泡已被替换/移除（切会话等），不再幽灵更新
      if (prev[idx].content === full) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], content: full };
      return next;
    });
    scrollToBottom(); // 气泡在长高，跟住底部
  }, [scrollToBottom]);

  /**
   * 收尾：等缓冲排空 → 再与服务端列表对齐（判据 R3）。
   *
   * ⚠️ 顺序不能颠倒（本次实测过跳变）：refetch 若在缓冲放完之前发生，
   * 服务端那份**完整**文本会把气泡瞬间补全，动画凭空跳一大段。
   * 所以**先放完再 refetch**，本地文本那一版永远不早于服务端出现。
   *
   * ⚠️ `flush` 参数是判据 S1 的成败关键（浏览器实弹抓到过一次）：
   *   - 正常收尾（收到 `[DONE]`）必须走 `end()` —— 上游结束了，但**显示还要按
   *     自适应速率把积压放完**。这里若也走 flush，300 字会在 0.2s 内一次性铺满，
   *     压速等于没做（实测 maxJump=306@244ms）。
   *   - 只有「人为停止 / 出错」才 flush：那两种情况下用户要的是**立刻**看到终态，
   *     不能对着慢慢冒字的气泡干等。
   */
  const finishStream = useCallback(async (conversationId: string, flush: boolean) => {
    const p = pacingRef.current;
    pacingRef.current = null;
    if (p) {
      if (flush) p.flush();
      else p.end(); // 上游结束：队列继续按速率放完，不跳变
      await p.whenDrained();
    }
    // 对齐时给「已放出的正文」建一条等价消息：不能拿 setMessages 里那份读，
    // 因为这里在 await 之后、闭包拿到的还是旧 messages。
    const id = streamBubbleIdRef.current;
    const local = pacedTextRef.current
      ? ({ id: id ?? `tmp-a-${Date.now()}`, role: "assistant", content: pacedTextRef.current, model: null, created_at: "" } as Message)
      : null;
    try {
      const server = await platformApi.messages(conversationId, { limit: PAGE_SIZE });
      setMessages((prev) => (prev.some((m) => m.id === id) ? reconcileTail(server, local) : prev));
    } catch {
      // 拉不到服务端列表就保留本地这份（用户看得见的内容绝不因为刷新失败而消失）
    }
  }, []);

  // 卸载时清理缓冲循环：否则 rAF 还挂着，回调会打在已卸载的组件上（幽灵更新）。
  // ⚠️ 只在卸载时跑：React 严格模式下开发环境会「挂载→卸载→再挂载」，
  // 这里清掉的只是当前这个（尚未开始流式的）实例，不会误伤正在跑的流。
  useEffect(() => disposePacing, [disposePacing]);

  // 「思考中」超过 THINKING_SLOW_AFTER_MS 仍无任何块 ⇒ 改口成「仍在等待模型…」。
  // 只换文案、不报错：本单实测真实首块 +11.97s，长推理是合法状态。
  useEffect(() => {
    if (!streaming || replyStarted) {
      setSlowThinking(false);
      return;
    }
    const timer = setTimeout(() => setSlowThinking(true), THINKING_SLOW_AFTER_MS);
    return () => clearTimeout(timer);
  }, [streaming, replyStarted]);

  const loadData = useCallback(async () => {
    try {
      const [user, ps, ms, convs] = await Promise.all([
        platformApi.me(),
        platformApi.personas(),
        platformApi.models(),
        platformApi.conversations(),
      ]);
      setMe(user);
      setPersonas(ps);
      setModels(ms);
      setConversations(convs);
    } catch (err) {
      if (err instanceof Error && err.message.includes("登录已过期")) return;
      setError(err instanceof Error ? err.message : "加载失败");
    }
  }, []);

  useEffect(() => {
    if (!getAccessToken()) {
      router.replace("/login");
      return;
    }
    loadData();
  }, [loadData, router]);

  // 导出菜单打开期间：点击外部 / 滚动 / 缩放 / Esc 关闭（面板 portal 到 body，与 Select 同策略）
  useEffect(() => {
    if (!exportOpen) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target;
      if (t instanceof Element && t.closest(".export-pop")) return;
      if (!exportRootRef.current?.contains(t as Node)) setExportOpen(false);
    };
    const close = () => setExportOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setExportOpen(false);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [exportOpen]);

  const openConversation = useCallback(
    async (id: string) => {
      // 切会话：把在跑的缓冲循环停掉，且此后不再回调（否则会往新会话的消息里写旧内容）
      disposePacing();
      pacedTextRef.current = "";
      setStreaming(false);
      setReplyStarted(true);
      setSlowThinking(false);
      abortRef.current?.abort();
      abortRef.current = null;
      setActiveId(id);
      setMessages([]);
      setHasMore(false);
      setAutoPanelOpen(false); // 面板若开着，别让它编辑到新会话的规则
      syncAutoState(id); // 恢复该会话的自动推进规则与开关
      try {
        const msgs = await platformApi.messages(id, { limit: PAGE_SIZE });
        setMessages(msgs);
        setHasMore(msgs.length === PAGE_SIZE);
        scrollToBottom();
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载会话失败");
      }
    },
    [disposePacing, scrollToBottom, syncAutoState]
  );

  /** 点人设：优先接续该人设最近的会话，没有才新建。 */
  async function chatWithPersona(persona: Persona, forceNew = false) {
    setError(null);
    setSidebarOpen(false);
    try {
      if (!forceNew) {
        const existing = conversations
          .filter((c) => c.persona_id === persona.id)
          .sort(
            (a, b) =>
              (b.last_message_at ?? b.created_at).localeCompare(a.last_message_at ?? a.created_at)
          )[0];
        if (existing) {
          openConversation(existing.id);
          return;
        }
      }
      const conv = await platformApi.createConversation(persona.id);
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
      setMessages(await platformApi.messages(conv.id));
      setHasMore(false);
      syncAutoState(conv.id); // 新会话：恢复（必为空态）
      scrollToBottom();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建会话失败");
    }
  }

  /** 向上滚动加载更早的消息，并保持视口位置。 */
  async function loadOlder() {
    if (!activeId || !hasMore || messages.length === 0) return;
    const container = scrollRef.current;
    const prevHeight = container?.scrollHeight ?? 0;
    try {
      const older = await platformApi.messages(activeId, {
        limit: PAGE_SIZE,
        before_id: messages[0].id,
      });
      setMessages((prev) => [...older, ...prev]);
      setHasMore(older.length === PAGE_SIZE);
      requestAnimationFrame(() => {
        if (container) container.scrollTop = container.scrollHeight - prevHeight;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载历史失败");
    }
  }

  function onScroll() {
    const el = scrollRef.current;
    if (el && el.scrollTop === 0 && hasMore && !streaming) loadOlder();
  }

  function stopStreaming() {
    // 先排空缓冲再 abort：已生成的内容一次性显示齐全（判据 R1），
    // 不能让用户点了「停止」还对着慢慢冒字的气泡。abort 之后的收尾在 send() 的
    // catch → finishStream 里统一做（flush 幂等，重复调用无副作用）。
    pacingRef.current?.flush();
    abortRef.current?.abort();
  }

  /** 导出当前会话全量记录：分页拉完 → 组装 → 浏览器下载。 */
  async function exportConversation(format: "md" | "json") {
    if (!activeConv || exportBusy) return;
    setExportOpen(false);
    setExportBusy(true);
    setError(null);
    try {
      const all = await fetchAllMessages(activeConv.id);
      const persona = personaOf(activeConv.persona_id);
      const title = activeConv.title ?? persona?.name ?? "会话";
      const stamp = new Date().toISOString().slice(0, 10);
      const base = `${safeFilename(title)}_${stamp}`;
      if (format === "md") {
        downloadText(`${base}.md`, toMarkdown(activeConv, persona, all), "text/markdown");
      } else {
        downloadText(`${base}.json`, toJson(activeConv, persona, all), "application/json");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败");
    } finally {
      setExportBusy(false);
    }
  }

  async function send(autoContent?: string) {
    if (!activeId || streaming) return;
    // 自动推进走的发送路径：内容来自规则，不读也不清输入框。
    const isAuto = typeof autoContent === "string";
    const content = isAuto ? autoContent : input.trim();
    if (!content) return;
    const conversationId = activeId;
    if (!isAuto) setInput("");
    setStreaming(true);
    setReplyStarted(false);
    setError(null);
    disposePacing();
    pacedTextRef.current = "";
    const controller = new AbortController();
    abortRef.current = controller;
    const assistantId = `tmp-a-${Date.now()}`;
    streamBubbleIdRef.current = assistantId;
    setMessages((prev) => [
      ...prev,
      { id: `tmp-u-${Date.now()}`, role: "user", content, model: null, created_at: "" },
      { id: assistantId, role: "assistant", content: "", model: null, created_at: "" },
    ]);
    scrollToBottom();
    // 节奏缓冲：上游来多少进度、显示层按积压量连续算出的速率放多少。
    // reduced-motion 为真时 immediate 模式（来多少显示多少，不做任何延迟）。
    const reveal = new PacedReveal({
      onReveal: (chunk) => {
        setReplyStarted(true);
        revealPaced(chunk);
      },
      immediate: prefersReducedMotion(),
    });
    pacingRef.current = reveal;
    try {
      await sendMessageStream(
        conversationId,
        content,
        (delta) => reveal.push(delta),
        controller.signal,
        (msg) => {
          // 退避等待的进度提示：这不是"内容"，但确实是"已经不在思考了"，
          // 所以一并撤掉「思考中」，免得用户对着转圈以为还在等模型。
          setReplyStarted(true);
          setError(msg);
        }
      );
      setError(null); // 清掉退避期间的「N 秒后自动重试」提示
      await finishStream(conversationId, false); // 正常收尾：按速率放完，不跳变
      setConversations(await platformApi.conversations());
      // 自动推进：只在正常收尾后触发（错误/停止路径不进来，天然满足）。
      maybeAutoAdvance(conversationId);
    } catch (err) {
      // 收尾前先把缓冲排空：错误必须**立刻**看得见，不能让积压继续慢慢放。
      // 判据 C2（流内错误帧）/C3（开流前 502）要的就是这一下 —— 指示撤掉、错误显示出来。
      const aborted = (err as Error).name === "AbortError";
      if (!aborted) setError(err instanceof Error ? err.message : "发送失败");
      await finishStream(conversationId, true);
    } finally {
      abortRef.current = null;
      streamBubbleIdRef.current = null;
      setStreaming(false);
      setReplyStarted(true); // 指示必须先消失，再等 React 提交这次渲染
      setSlowThinking(false);
      scrollToBottom();
    }
  }

  // 自动推进的发送入口：send 会随每次渲染重建，这里用 ref 持有最新实例，
  // setTimeout 的回调经它调用（避免闭包捕获过期状态）。
  useEffect(() => {
    autoSendRef.current = (content: string) => send(content);
  });

  async function regenerate() {
    if (!activeId || streaming) return;
    const conversationId = activeId;
    setStreaming(true);
    setReplyStarted(false);
    setError(null);
    disposePacing();
    pacedTextRef.current = "";
    const controller = new AbortController();
    abortRef.current = controller;
    // 服务端会替换最后一条助手回复；前端先把末尾助手消息清空接流
    let assistantId: string;
    setMessages((prev) => {
      const next = [...prev];
      if (next[next.length - 1]?.role === "assistant") {
        assistantId = next[next.length - 1].id;
        next[next.length - 1] = { ...next[next.length - 1], content: "" };
      } else {
        assistantId = `tmp-a-${Date.now()}`;
        next.push({ id: assistantId, role: "assistant", content: "", model: null, created_at: "" });
      }
      return next;
    });
    streamBubbleIdRef.current = assistantId!;
    scrollToBottom();
    const reveal = new PacedReveal({
      onReveal: (chunk) => {
        setReplyStarted(true);
        revealPaced(chunk);
      },
      immediate: prefersReducedMotion(),
    });
    pacingRef.current = reveal;
    try {
      await regenerateStream(
        conversationId,
        (delta) => reveal.push(delta),
        controller.signal,
        (msg) => {
          setReplyStarted(true);
          setError(msg);
        }
      );
      setError(null); // 清掉退避期间的「N 秒后自动重试」提示
      await finishStream(conversationId, false); // 正常收尾：按速率放完，不跳变
    } catch (err) {
      const aborted = (err as Error).name === "AbortError";
      if (!aborted) setError(err instanceof Error ? err.message : "重新生成失败");
      await finishStream(conversationId, true);
    } finally {
      abortRef.current = null;
      streamBubbleIdRef.current = null;
      setStreaming(false);
      setReplyStarted(true);
      setSlowThinking(false);
      scrollToBottom();
    }
  }

  async function switchModel(conv: Conversation, model: string) {
    try {
      const updated = await platformApi.updateConversation(conv.id, { model: model || "" });
      setConversations((prev) => prev.map((c) => (c.id === conv.id ? updated : c)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "切换模型失败");
    }
  }

  async function removeConversation(id: string) {
    if (!confirm("删除这个会话及其全部消息？")) return;
    try {
      await platformApi.deleteConversation(id);
      setConversations((prev) => prev.filter((c) => c.id !== id));
      if (activeId === id) {
        setActiveId(null);
        setMessages([]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  }

  async function togglePin(conv: Conversation) {
    try {
      const updated = await platformApi.updateConversation(conv.id, { pinned: !conv.pinned });
      setConversations((prev) =>
        [updated, ...prev.filter((c) => c.id !== conv.id)].sort(
          (a, b) =>
            Number(b.pinned) - Number(a.pinned) ||
            (b.last_message_at ?? b.created_at).localeCompare(a.last_message_at ?? a.created_at)
        )
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  async function renameConversation(conv: Conversation) {
    const title = prompt("修改会话标题", conv.title ?? "");
    if (title === null || !title.trim()) return;
    try {
      const updated = await platformApi.updateConversation(conv.id, { title: title.trim() });
      setConversations((prev) => prev.map((c) => (c.id === conv.id ? updated : c)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "改名失败");
    }
  }

  async function copyMessage(m: Message) {
    try {
      await navigator.clipboard.writeText(m.content);
      setError(null);
    } catch {
      setError("复制失败");
    }
  }

  function logout() {
    clearTokens();
    router.replace("/login");
  }

  const activeConv = conversations.find((c) => c.id === activeId);
  const personaOf = (id: string) => personas.find((p) => p.id === id);
  const lastAssistantId =
    !streaming && messages.length > 0 && messages[messages.length - 1].role === "assistant"
      ? messages[messages.length - 1].id
      : null;
  const filteredConvs = convSearch.trim()
    ? conversations.filter((c) =>
        (c.title ?? personaOf(c.persona_id)?.name ?? "").toLowerCase().includes(convSearch.trim().toLowerCase())
      )
    : conversations;

  if (!me) {
    return <main className="chat-loading">{error ?? "加载中…"}</main>;
  }

  return (
    <div className="chat-layout">
      {sidebarOpen && <div className="sidebar-mask" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-user">
          <Avatar name={me.display_name ?? me.email} size={36} />
          <div className="sidebar-user-info">
            <strong>{me.display_name ?? me.email}</strong>
            <small>{me.email}</small>
          </div>
          <button className="icon-btn" title="账号设置" onClick={() => setShowSettings(true)}>
            <IconGear />
          </button>
        </div>

        <div className="sidebar-section">
          <div className="section-head">
            <span>人设</span>
            <button className="icon-btn" title="新建人设" onClick={() => setPersonaModal({ mode: "create" })}>
              ＋
            </button>
          </div>
          <div className="persona-list">
            {personas.length === 0 && <p className="empty">还没有人设，点 ＋ 创建</p>}
            {personas.map((p) => (
              <div key={p.id} className="persona-item">
                <button className="persona-main" onClick={() => chatWithPersona(p)} disabled={streaming}>
                  <Avatar name={p.name} url={p.avatar_url} size={28} />
                  <span className="persona-name">{p.name}</span>
                  <small>{p.visibility === "public" ? (p.owner_id === me.id ? "已发布" : "公共") : "私有"}</small>
                </button>
                {p.owner_id === me.id && (
                  <span className="persona-actions">
                    <button className="icon-btn" title="新对话" onClick={() => chatWithPersona(p, true)}>
                      ＋
                    </button>
                    <button className="icon-btn" title="编辑" onClick={() => setPersonaModal({ mode: "edit", persona: p })}>
                      <IconPencil />
                    </button>
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="sidebar-section grow">
          <div className="section-head">
            <span>会话</span>
            <input
              className="conv-search"
              placeholder="搜索…"
              value={convSearch}
              onChange={(e) => setConvSearch(e.target.value)}
            />
          </div>
          <div className="conv-list">
            {filteredConvs.length === 0 && <p className="empty">{convSearch ? "无匹配会话" : "点上方人设开始对话"}</p>}
            {filteredConvs.map((c) => {
              const p = personaOf(c.persona_id);
              return (
                <div
                  key={c.id}
                  className={`conv-item ${c.id === activeId ? "active" : ""}`}
                  onClick={() => {
                    openConversation(c.id);
                    setSidebarOpen(false);
                  }}
                >
                  <span className="conv-pin">{c.pinned && <IconPin filled size={13} className="pinned" />}</span>
                  <span className="conv-title">{c.title ?? p?.name ?? "会话"}</span>
                  <span className="conv-actions">
                    <button className="icon-btn" title={c.pinned ? "取消置顶" : "置顶"} onClick={(e) => { e.stopPropagation(); togglePin(c); }}>
                      <IconPin size={14} />
                    </button>
                    <button className="icon-btn" title="重命名" onClick={(e) => { e.stopPropagation(); renameConversation(c); }}>
                      <IconPencil size={14} />
                    </button>
                    <button className="icon-btn danger" title="删除会话" onClick={(e) => { e.stopPropagation(); removeConversation(c.id); }}>
                      ×
                    </button>
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        <footer className="sidebar-footer">
          <Link href="/market" className="sidebar-link">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z" />
              <path d="M3 6h18" />
              <path d="M16 10a4 4 0 0 1-8 0" />
            </svg>
            <span>人设市场</span>
          </Link>
          <Link href="/keys" className="sidebar-link">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z" />
              <circle cx="16.5" cy="7.5" r=".5" fill="currentColor" />
            </svg>
            <span>密钥与用量</span>
          </Link>
          {me.role === "admin" && (
            <Link href="/admin" className="sidebar-link">
              <IconGauge size={15} />
              <span>管理后台</span>
            </Link>
          )}
          {me.role === "admin" && (
            <Link href="/models" className="sidebar-link">
              <IconGear size={15} />
              <span>模型管理</span>
            </Link>
          )}
        </footer>
      </aside>

      <main className="chat-main">
        {activeConv ? (
          <>
            <header className="chat-header">
              <button className="icon-btn menu-btn" title="菜单" onClick={() => setSidebarOpen(true)}>
                <IconMenu />
              </button>
              <div className="chat-header-info">
                <Avatar
                  name={personaOf(activeConv.persona_id)?.name ?? activeConv.title ?? "会话"}
                  url={personaOf(activeConv.persona_id)?.avatar_url}
                  size={34}
                />
                <div className="chat-header-text">
                  <strong>{activeConv.title ?? personaOf(activeConv.persona_id)?.name ?? "会话"}</strong>
                  <small>{personaOf(activeConv.persona_id)?.name}</small>
                </div>
              </div>
              {personaOf(activeConv.persona_id)?.owner_id === me.id && (
                <button
                  className="icon-btn"
                  title="长期记忆"
                  onClick={() => {
                    const p = personaOf(activeConv.persona_id);
                    if (p) setMemoryModalFor(p);
                  }}
                >
                  <IconBrain />
                </button>
              )}
              <div ref={exportRootRef} className="export-wrap">
                <button
                  ref={exportBtnRef}
                  className="icon-btn"
                  title="导出聊天记录"
                  aria-haspopup="menu"
                  aria-expanded={exportOpen}
                  disabled={exportBusy}
                  onClick={() => {
                    if (exportOpen) {
                      setExportOpen(false);
                      return;
                    }
                    const r = exportBtnRef.current!.getBoundingClientRect();
                    setExportPos({ left: Math.max(8, r.right - 180), top: r.bottom + 6 });
                    setExportOpen(true);
                  }}
                >
                  <IconDownload />
                </button>
                {exportOpen &&
                  exportPos &&
                  createPortal(
                    <div className="export-pop" role="menu" style={{ left: exportPos.left, top: exportPos.top }}>
                      <button role="menuitem" onClick={() => exportConversation("md")}>
                        导出为 Markdown
                      </button>
                      <button role="menuitem" onClick={() => exportConversation("json")}>
                        导出为 JSON
                      </button>
                    </div>,
                    document.body
                  )}
              </div>
              <Select
                className="model-select"
                ariaLabel="切换本会话使用的模型"
                title="切换本会话使用的模型"
                value={activeConv.model ?? ""}
                onChange={(v) => switchModel(activeConv, v)}
                options={[
                  { value: "", label: "默认模型" },
                  ...models.map((m) => ({ value: m.model, label: m.name })),
                ]}
              />
            </header>
            <div className="messages" ref={scrollRef} onScroll={onScroll}>
              {hasMore && (
                <button className="load-older" onClick={loadOlder} disabled={streaming}>
                  ↑ 加载更早的消息
                </button>
              )}
              {messages.map((m, i) => {
                const prev = messages[i - 1];
                const showAvatar = !prev || prev.role !== m.role;
                const time = new Date(m.created_at).toLocaleTimeString("zh-CN", {
                  hour: "2-digit",
                  minute: "2-digit",
                });
                const activePersona = personaOf(activeConv.persona_id);
                // 「思考中」只在「正在流式 + 还没有任何块可显示」时为真。
                // 首块一到（replyStarted）立刻为假 ⇒ 指示与内容不会并存（C1）；
                // 错误帧/开流前失败同样把 replyStarted 置真并结束流式 ⇒ 指示立刻消失（C2/C3）。
                const thinking = streaming && !replyStarted && i === messages.length - 1;
                return (
                  <div key={m.id} className={`bubble-row ${m.role}`}>
                    {m.role === "assistant" ? (
                      showAvatar ? (
                        <Avatar name={activePersona?.name ?? "AI"} url={activePersona?.avatar_url} size={34} />
                      ) : (
                        <span className="avatar-spacer" />
                      )
                    ) : showAvatar ? (
                      <Avatar name={me.display_name ?? me.email} size={34} />
                    ) : (
                      <span className="avatar-spacer" />
                    )}
                    <div className="msg-col">
                      <div className={`bubble ${m.role}`}>
                        {m.role === "assistant" ? (
                          thinking ? (
                            <span className="thinking" role="status" aria-live="polite">
                              <span className="thinking-dots" aria-hidden>
                                <i />
                                <i />
                                <i />
                              </span>
                              <span className="thinking-text">
                                {slowThinking ? "仍在等待模型…" : "思考中…"}
                              </span>
                            </span>
                          ) : (
                            <Markdown content={m.content} />
                          )
                        ) : (
                          m.content
                        )}
                        {m.role === "assistant" && (
                          <span className="bubble-meta">
                            {m.model && <small>{m.model}</small>}
                        <button className="icon-btn copy-btn" title="复制" onClick={() => copyMessage(m)}>
                          <IconCopy />
                        </button>
                        {m.id === lastAssistantId && (
                          <button className="icon-btn copy-btn" title="重新生成" onClick={regenerate}>
                            <IconRefresh />
                          </button>
                        )}
                          </span>
                        )}
                      </div>
                      <small className="msg-time">{time}</small>
                    </div>
                  </div>
                );
              })}
              <div ref={bottomRef} />
            </div>
            <footer className="composer">
              <div className="composer-left">
                <button
                  type="button"
                  className={`auto-toggle ${autoEnabled ? "on" : ""}`}
                  title={autoEnabled ? "自动推进已开启（点击关闭）" : "自动推进已关闭（点击开启）"}
                  aria-pressed={autoEnabled}
                  onClick={() => {
                    if (!activeId) return;
                    setAutoPanelOpen(true);
                    if (!autoEnabled) toggleAuto(activeId, true);
                  }}
                >
                  <span className="auto-knob" aria-hidden />
                  自动推进
                </button>
                {autoEnabled && (
                  <button
                    type="button"
                    className="auto-round-chip"
                    title="点击编辑自动推进规则"
                    onClick={() => setAutoPanelOpen(true)}
                  >
                    第 {autoRound} 轮 · {autoRules.filter((r) => r.enabled).length} 条规则
                  </button>
                )}
              </div>
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                placeholder="输入消息，Enter 发送，Shift+Enter 换行"
                rows={2}
                disabled={streaming}
              />
              {streaming ? (
                <button className="primary stop" onClick={stopStreaming}>
                  <IconStop /> 停止
                </button>
              ) : (
                <button className="primary" onClick={() => send()} disabled={!input.trim()}>
                  发送
                </button>
              )}
            </footer>
          </>
        ) : (
          <div className="chat-empty">
            <button className="icon-btn menu-btn" title="菜单" onClick={() => setSidebarOpen(true)}>
              <IconMenu />
            </button>
            <h2>选择或创建一个会话</h2>
            <p>左侧点一个人设开始对话；也可以去人设市场逛逛</p>
          </div>
        )}
        {error && <div className="toast error">{error}</div>}
      </main>

      {personaModal && (
        <PersonaForm
          mode={personaModal.mode}
          persona={personaModal.mode === "edit" ? personaModal.persona : undefined}
          onClose={() => setPersonaModal(null)}
          onSaved={(saved, isNew) => {
            setPersonas((prev) => (isNew ? [saved, ...prev] : prev.map((p) => (p.id === saved.id ? saved : p))));
            setPersonaModal(null);
            if (isNew) chatWithPersona(saved, true);
          }}
          onDeleted={(id) => {
            setPersonas((prev) => prev.filter((p) => p.id !== id));
            setPersonaModal(null);
          }}
        />
      )}

      {memoryModalFor && (
        <MemoryModal
          persona={memoryModalFor}
          onClose={() => setMemoryModalFor(null)}
          onPersonaUpdated={(p) => setPersonas((prev) => prev.map((x) => (x.id === p.id ? p : x)))}
        />
      )}

      {autoPanelOpen && activeId && (
        <AutoAdvancePanel
          conversationId={activeId}
          state={{ rules: autoRules, enabled: autoEnabled }}
          round={autoRound}
          onClose={() => setAutoPanelOpen(false)}
          onChange={(next) => commitAutoState(activeId, next)}
          onToggle={(on) => toggleAuto(activeId, on)}
        />
      )}

      {showSettings && (
        <SettingsModal
          me={me}
          onClose={() => setShowSettings(false)}
          onSaved={(user) => {
            setMe(user);
            setShowSettings(false);
          }}
        />
      )}
    </div>
  );
}

function AutoAdvancePanel({
  conversationId,
  state,
  round,
  onClose,
  onChange,
  onToggle,
}: {
  conversationId: string;
  state: AutoAdvanceState;
  round: number;
  onClose: () => void;
  onChange: (s: AutoAdvanceState) => void;
  onToggle: (on: boolean) => void;
}) {
  const [drafts, setDrafts] = useState<Array<{ content: string; interval: string }>>(() =>
    state.rules.map((r) => ({ content: r.content, interval: String(r.interval) }))
  );
  const [error, setError] = useState<string | null>(null);

  function addRule() {
    setDrafts((prev) => [...prev, { content: "继续", interval: "1" }]);
  }

  function removeRule(i: number) {
    setDrafts((prev) => prev.filter((_, idx) => idx !== i));
  }

  function save() {
    const cleaned = drafts.map((d) => ({
      content: d.content,
      interval: d.interval,
      enabled: true,
    }));
    for (const d of cleaned) {
      const n = Number(d.interval);
      if (!Number.isInteger(n) || n < 1) {
        setError(`间隔必须是 ≥1 的整数（收到「${d.interval}」）`);
        return;
      }
      if (!d.content.trim()) {
        setError("规则内容不能为空");
        return;
      }
    }
    setError(null);
    // 规则变更 ⇒ 计数清零重新起算，避免旧计数对新规则语义错位。
    saveRound(conversationId, 0);
    onChange({
      rules: sanitizeRules(cleaned),
      enabled: state.enabled,
    });
    onClose();
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal auto-advance-panel"
        role="dialog"
        aria-label="自动推进规则"
        onClick={(e) => e.stopPropagation()}
      >
        <h3>自动推进</h3>
        <p className="auto-advance-hint">
          开启后，每轮模型回复正常结束，都会按下面的规则自动发送一条消息让剧情继续。
          多条规则同一轮到期时，会合并成一条消息发送。
        </p>
        <label className="auto-advance-switch">
          <input
            type="checkbox"
            checked={state.enabled}
            onChange={(e) => onToggle(e.target.checked)}
          />
          开启自动推进（当前第 {round} 轮）
        </label>
        <div className="auto-advance-rules">
          {drafts.length === 0 && <p className="auto-advance-empty">还没有规则，先加一条。</p>}
          {drafts.map((d, i) => (
            <div className="auto-advance-rule" key={i}>
              <textarea
                value={d.content}
                rows={2}
                placeholder="自动发送的内容，例如：继续"
                onChange={(e) =>
                  setDrafts((prev) => prev.map((x, idx) => (idx === i ? { ...x, content: e.target.value } : x)))
                }
              />
              <label className="auto-advance-interval">
                每
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={d.interval}
                  onChange={(e) =>
                    setDrafts((prev) => prev.map((x, idx) => (idx === i ? { ...x, interval: e.target.value } : x)))
                  }
                />
                轮回复后发送
              </label>
              <button type="button" className="auto-advance-remove" onClick={() => removeRule(i)}>
                删除
              </button>
            </div>
          ))}
          <button type="button" className="auto-advance-add" onClick={addRule}>
            + 添加规则
          </button>
        </div>
        {error && <p className="auto-advance-error">{error}</p>}
        <div className="auto-advance-actions">
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary" onClick={save}>
            保存
          </button>
        </div>
      </div>
    </div>
  );
}

function PersonaForm({
  mode,
  persona,
  onClose,
  onSaved,
  onDeleted,
}: {
  mode: "create" | "edit";
  persona?: Persona;
  onClose: () => void;
  onSaved: (p: Persona, isNew: boolean) => void;
  onDeleted: (id: string) => void;
}) {
  const isEdit = mode === "edit";
  const [name, setName] = useState(persona?.name ?? "");
  const [slug, setSlug] = useState(persona?.slug ?? "");
  const [systemPrompt, setSystemPrompt] = useState(persona?.system_prompt ?? "");
  const [opening, setOpening] = useState(persona?.opening_message ?? "");
  const [model, setModel] = useState(persona?.model ?? "");
  const [temperature, setTemperature] = useState(persona?.temperature?.toString() ?? "");
  const [tags, setTags] = useState(persona?.tags?.join(", ") ?? "");
  const [visibility, setVisibility] = useState(persona?.visibility ?? "private");
  const [avatar, setAvatar] = useState(persona?.avatar_url ?? "");
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  // 与后端同一条 pattern（api.ts 里那份），别等提交吃了 422 才知道规则
  const slugIssue = slug.trim() && !PERSONA_SLUG_PATTERN.test(slug.trim()) ? `slug ${PERSONA_SLUG_RULE}` : null;
  const missing = [!name.trim() && "名称", !systemPrompt.trim() && "System Prompt"].filter(Boolean) as string[];
  const guard = missing.length ? `还需填写：${missing.join("、")}` : slugIssue;
  // 表单里没有对应输入框的字段（如 avatar_url / tags）不能悄悄丢掉，仍要在整表级显示
  const inlineFields = new Set(["name", "slug", "system_prompt"]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      const tagList = tags
        .split(/[,，\s]+/)
        .map((t) => t.trim())
        .filter(Boolean);
      const body: Record<string, unknown> = {
        name,
        system_prompt: systemPrompt,
        opening_message: opening || undefined,
        model: model || undefined,
        temperature: temperature ? Number(temperature) : undefined,
        visibility,
        tags: tagList.length ? tagList : undefined,
        // 编辑时始终带上：清空头像也能生效；新建时空值交由后端默认
        avatar_url: avatar ? avatar : isEdit ? null : undefined,
      };
      const saved = isEdit
        ? await platformApi.updatePersona(persona!.id, body)
        : await platformApi.createPersona({ ...body, slug: slug || undefined });
      onSaved(saved, !isEdit);
    } catch (err) {
      // 422 的字段级原因落到各自输入框；其余（余额/限流/服务忙）仍走整表单级提示
      const fields = err instanceof ApiError ? err.fieldErrors : undefined;
      if (fields && Object.keys(fields).length) {
        setFieldErrors(fields);
        const orphan = Object.entries(fields)
          .filter(([f]) => !inlineFields.has(f))
          .map(([, text]) => text);
        setError(orphan.length ? orphan.join("；") : null);
      } else {
        setError(err instanceof Error ? err.message : "保存失败");
      }
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm(`删除人设「${persona!.name}」？（有会话引用时无法删除）`)) return;
    try {
      await platformApi.deletePersona(persona!.id);
      onDeleted(persona!.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h3>{isEdit ? "编辑人设" : "新建人设"}</h3>
        <div className="avatar-picker">
          <Avatar name={name || "人设"} url={avatar || null} size={56} />
          <div className="avatar-picker-body">
            <div className="emoji-grid">
              {AVATAR_EMOJIS.map((e) => (
                <button
                  key={e}
                  type="button"
                  className={`emoji-opt${avatar === e ? " picked" : ""}`}
                  onClick={() => setAvatar(avatar === e ? "" : e)}
                >
                  {e}
                </button>
              ))}
            </div>
            <input
              value={avatar}
              onChange={(e) => setAvatar(e.target.value)}
              placeholder="或粘贴头像图片链接（https://…）"
            />
          </div>
        </div>
        <label>
          名称 *
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：李白"
            aria-invalid={Boolean(fieldErrors.name)}
            required
          />
          {fieldErrors.name && <p className="field-error">{fieldErrors.name}</p>}
        </label>
        {!isEdit && (
          <label>
            slug（可选，用于 model=&quot;persona:slug&quot;，留空自动生成）
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="libai"
              aria-invalid={Boolean(slugIssue || fieldErrors.slug)}
            />
            <p className="hint">{`留空自动生成。规则：${PERSONA_SLUG_RULE}`}</p>
            {(slugIssue || fieldErrors.slug) && (
              <p className="field-error">{fieldErrors.slug ?? slugIssue}</p>
            )}
          </label>
        )}
        <label>
          人设 System Prompt *
          <textarea
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="你是诗仙李白，说话豪放洒脱…"
            rows={4}
            aria-invalid={Boolean(fieldErrors.system_prompt)}
            required
          />
          {fieldErrors.system_prompt && <p className="field-error">{fieldErrors.system_prompt}</p>}
        </label>
        <label>
          开场白（可选）
          <input
            value={opening}
            onChange={(e) => setOpening(e.target.value)}
            placeholder="君不见黄河之水天上来！"
          />
        </label>
        <div className="field-row">
          <label>
            底层模型（可选）
            <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="gpt-4o-mini / deepseek-chat" />
          </label>
          <label>
            温度 0~2（可选）
            <input
              type="number"
              step="0.1"
              min="0"
              max="2"
              value={temperature}
              onChange={(e) => setTemperature(e.target.value)}
            />
          </label>
        </div>
        <div className="field-row">
          <label>
            标签（逗号分隔，市场搜索用）
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="诗词, 历史角色" />
          </label>
          <label>
            可见性
            <Select
              ariaLabel="可见性"
              value={visibility}
              onChange={setVisibility}
              options={[
                { value: "private", label: "私有" },
                { value: "public", label: "公开（发布到人设市场）" },
              ]}
            />
          </label>
        </div>
        {error && <p className="auth-error">{error}</p>}
        {guard && <p className="hint">{guard}</p>}
        <div className="modal-actions">
          {isEdit && (
            <button type="button" className="danger" onClick={remove}>
              删除
            </button>
          )}
          <span className="spacer" />
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="primary" disabled={busy || Boolean(guard)}>
            {busy ? "保存中…" : isEdit ? "保存" : "创建并开始对话"}
          </button>
        </div>
      </form>
    </div>
  );
}

function SettingsModal({
  me,
  onClose,
  onSaved,
}: {
  me: User;
  onClose: () => void;
  onSaved: (u: User) => void;
}) {
  const [displayName, setDisplayName] = useState(me.display_name ?? "");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [theme, setTheme] = useState<Theme>("dark");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTheme(getTheme());
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const user = await platformApi.updateMe({
        display_name: displayName.trim() || undefined,
        ...(newPassword ? { new_password: newPassword, current_password: currentPassword } : {}),
      });
      onSaved(user);
      setSaved(true);
      setCurrentPassword("");
      setNewPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h3>账号设置</h3>
        <p className="hint">{me.email}</p>
        <label>
          昵称
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="显示名称" />
        </label>
        <label>
          外观
          <div className="theme-switch" role="group" aria-label="主题外观">
            <button
              type="button"
              className={theme === "dark" ? "on" : ""}
              aria-pressed={theme === "dark"}
              onClick={() => {
                setTheme("dark");
                applyTheme("dark");
              }}
            >
              深色
            </button>
            <button
              type="button"
              className={theme === "light" ? "on" : ""}
              aria-pressed={theme === "light"}
              onClick={() => {
                setTheme("light");
                applyTheme("light");
              }}
            >
              浅色
            </button>
          </div>
        </label>
        <div className="field-row">
          <label>
            当前密码（改密码时填）
            <input
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              placeholder="••••••••"
            />
          </label>
          <label>
            新密码（至少 8 位）
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="留空则不改密码"
            />
          </label>
        </div>
        {error && <p className="auth-error">{error}</p>}
        {saved && <p className="saved-hint">已保存 ✓</p>}
        <div className="modal-actions">
          <span className="spacer" />
          <button type="button" onClick={onClose}>
            关闭
          </button>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </form>
    </div>
  );
}
