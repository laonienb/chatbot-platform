"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  clearTokens,
  getAccessToken,
  platformApi,
  sendMessageStream,
  type Conversation,
  type Message,
  type Persona,
  type User,
} from "@/lib/api";

export default function ChatPage() {
  const router = useRouter();
  const [me, setMe] = useState<User | null>(null);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPersonaForm, setShowPersonaForm] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }));
  }, []);

  const loadData = useCallback(async () => {
    try {
      const [user, ps, convs] = await Promise.all([
        platformApi.me(),
        platformApi.personas(),
        platformApi.conversations(),
      ]);
      setMe(user);
      setPersonas(ps);
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

  const openConversation = useCallback(
    async (id: string) => {
      setActiveId(id);
      setMessages([]);
      try {
        setMessages(await platformApi.messages(id));
        scrollToBottom();
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载会话失败");
      }
    },
    [scrollToBottom]
  );

  async function startConversation(persona: Persona) {
    setError(null);
    try {
      const conv = await platformApi.createConversation(persona.id);
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
      const msgs = await platformApi.messages(conv.id);
      setMessages(msgs);
      scrollToBottom();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建会话失败");
    }
  }

  async function send() {
    if (!activeId || !input.trim() || streaming) return;
    const content = input.trim();
    setInput("");
    setStreaming(true);
    setError(null);
    // 乐观插入：用户消息 + 空的助手消息占位
    setMessages((prev) => [
      ...prev,
      { id: `tmp-u-${Date.now()}`, role: "user", content, model: null, created_at: "" },
      { id: `tmp-a-${Date.now()}`, role: "assistant", content: "", model: null, created_at: "" },
    ]);
    scrollToBottom();
    try {
      await sendMessageStream(activeId, content, (delta) => {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role === "assistant") next[next.length - 1] = { ...last, content: last.content + delta };
          return next;
        });
        scrollToBottom();
      });
      // 流结束：同步服务端落库的消息（拿到真实 id）并刷新会话排序
      setMessages(await platformApi.messages(activeId));
      setConversations(await platformApi.conversations());
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败");
      if (activeId) setMessages(await platformApi.messages(activeId).catch(() => []));
    } finally {
      setStreaming(false);
      scrollToBottom();
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

  function logout() {
    clearTokens();
    router.replace("/login");
  }

  const activeConv = conversations.find((c) => c.id === activeId);
  const personaOf = (id: string) => personas.find((p) => p.id === id);

  if (!me) {
    return (
      <main className="chat-loading">{error ?? "加载中…"}</main>
    );
  }

  return (
    <div className="chat-layout">
      <aside className="sidebar">
        <div className="sidebar-user">
          <span className="avatar">{(me.display_name ?? me.email)[0]?.toUpperCase()}</span>
          <div className="sidebar-user-info">
            <strong>{me.display_name ?? me.email}</strong>
            <small>{me.email}</small>
          </div>
          <button className="icon-btn" title="退出登录" onClick={logout}>
            ⎋
          </button>
        </div>

        <div className="sidebar-section">
          <div className="section-head">
            <span>人设</span>
            <button className="icon-btn" title="新建人设" onClick={() => setShowPersonaForm(true)}>
              ＋
            </button>
          </div>
          <div className="persona-list">
            {personas.length === 0 && <p className="empty">还没有人设，点 ＋ 创建</p>}
            {personas.map((p) => (
              <button key={p.id} className="persona-item" onClick={() => startConversation(p)} disabled={streaming}>
                <span className="avatar sm">{p.name[0]}</span>
                <span className="persona-name">{p.name}</span>
                <small>{p.visibility === "public" ? "公共" : "私有"}</small>
              </button>
            ))}
          </div>
        </div>

        <div className="sidebar-section grow">
          <div className="section-head">
            <span>会话</span>
          </div>
          <div className="conv-list">
            {conversations.length === 0 && <p className="empty">点上方人设开始新对话</p>}
            {conversations.map((c) => {
              const p = personaOf(c.persona_id);
              return (
                <div
                  key={c.id}
                  className={`conv-item ${c.id === activeId ? "active" : ""}`}
                  onClick={() => openConversation(c.id)}
                >
                  <span className="conv-title">{c.title ?? p?.name ?? "会话"}</span>
                  <button
                    className="icon-btn danger"
                    title="删除会话"
                    onClick={(e) => {
                      e.stopPropagation();
                      removeConversation(c.id);
                    }}
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </aside>

      <main className="chat-main">
        {activeConv ? (
          <>
            <header className="chat-header">
              <strong>{activeConv.title ?? personaOf(activeConv.persona_id)?.name ?? "会话"}</strong>
              <small>{personaOf(activeConv.persona_id)?.name}</small>
            </header>
            <div className="messages" onClick={scrollToBottom}>
              {messages.map((m) => (
                <div key={m.id} className={`bubble-row ${m.role}`}>
                  <div className={`bubble ${m.role}`}>
                    {m.content}
                    {m.role === "assistant" && m.model && <small className="bubble-meta">{m.model}</small>}
                  </div>
                </div>
              ))}
              <div ref={bottomRef} />
            </div>
            <footer className="composer">
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
              <button className="primary" onClick={send} disabled={streaming || !input.trim()}>
                {streaming ? "…" : "发送"}
              </button>
            </footer>
          </>
        ) : (
          <div className="chat-empty">
            <h2>选择或创建一个会话</h2>
            <p>左侧点一个人设即可开始对话</p>
          </div>
        )}
        {error && <div className="toast error">{error}</div>}
      </main>

      {showPersonaForm && (
        <PersonaForm
          onClose={() => setShowPersonaForm(false)}
          onCreated={(p) => {
            setPersonas((prev) => [p, ...prev]);
            setShowPersonaForm(false);
            startConversation(p);
          }}
        />
      )}
    </div>
  );
}

function PersonaForm({ onClose, onCreated }: { onClose: () => void; onCreated: (p: Persona) => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [opening, setOpening] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const p = await platformApi.createPersona({
        name,
        slug: slug || undefined,
        system_prompt: systemPrompt,
        opening_message: opening || undefined,
      });
      onCreated(p);
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h3>新建人设</h3>
        <label>
          名称 *
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：李白" required />
        </label>
        <label>
          slug（可选，用于 model=&quot;persona:slug&quot;，留空自动生成）
          <input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="libai" />
        </label>
        <label>
          人设 System Prompt *
          <textarea
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="你是诗仙李白，说话豪放洒脱…"
            rows={4}
            required
          />
        </label>
        <label>
          开场白（可选）
          <input
            value={opening}
            onChange={(e) => setOpening(e.target.value)}
            placeholder="君不见黄河之水天上来！"
          />
        </label>
        {error && <p className="auth-error">{error}</p>}
        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="primary" disabled={busy || !name || !systemPrompt}>
            {busy ? "创建中…" : "创建并开始对话"}
          </button>
        </div>
      </form>
    </div>
  );
}
