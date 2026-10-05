"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
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
  const [personaModal, setPersonaModal] = useState<
    { mode: "create" } | { mode: "edit"; persona: Persona } | null
  >(null);
  const [showSettings, setShowSettings] = useState(false);
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

  /** 点人设：优先接续该人设最近的会话，没有才新建。 */
  async function chatWithPersona(persona: Persona, forceNew = false) {
    setError(null);
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

  if (!me) {
    return <main className="chat-loading">{error ?? "加载中…"}</main>;
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
          <button className="icon-btn" title="账号设置" onClick={() => setShowSettings(true)}>
            ⚙
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
                  <span className="avatar sm">{p.name[0]}</span>
                  <span className="persona-name">{p.name}</span>
                  <small>{p.visibility === "public" && p.owner_id !== me.id ? "公共" : "私有"}</small>
                </button>
                {p.owner_id === me.id && (
                  <span className="persona-actions">
                    <button className="icon-btn" title="新对话" onClick={() => chatWithPersona(p, true)}>
                      ＋
                    </button>
                    <button className="icon-btn" title="编辑" onClick={() => setPersonaModal({ mode: "edit", persona: p })}>
                      ✎
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
          </div>
          <div className="conv-list">
            {conversations.length === 0 && <p className="empty">点上方人设开始对话</p>}
            {conversations.map((c) => {
              const p = personaOf(c.persona_id);
              return (
                <div
                  key={c.id}
                  className={`conv-item ${c.id === activeId ? "active" : ""}`}
                  onClick={() => openConversation(c.id)}
                >
                  <span className="conv-pin">{c.pinned ? "📌" : ""}</span>
                  <span className="conv-title">{c.title ?? p?.name ?? "会话"}</span>
                  <span className="conv-actions">
                    <button className="icon-btn" title="置顶/取消置顶" onClick={(e) => { e.stopPropagation(); togglePin(c); }}>
                      📌
                    </button>
                    <button className="icon-btn" title="重命名" onClick={(e) => { e.stopPropagation(); renameConversation(c); }}>
                      ✎
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
          <Link href="/keys" className="sidebar-link">
            🔑 密钥与用量
          </Link>
        </footer>
      </aside>

      <main className="chat-main">
        {activeConv ? (
          <>
            <header className="chat-header">
              <strong>{activeConv.title ?? personaOf(activeConv.persona_id)?.name ?? "会话"}</strong>
              <small>{personaOf(activeConv.persona_id)?.name}</small>
            </header>
            <div className="messages">
              {messages.map((m) => (
                <div key={m.id} className={`bubble-row ${m.role}`}>
                  <div className={`bubble ${m.role}`}>
                    {m.content}
                    {m.role === "assistant" && (
                      <span className="bubble-meta">
                        {m.model && <small>{m.model}</small>}
                        <button className="icon-btn copy-btn" title="复制" onClick={() => copyMessage(m)}>
                          ⧉
                        </button>
                      </span>
                    )}
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
            <p>左侧点一个人设开始对话；点人设旁的 ＋ 开新对话</p>
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
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        name,
        system_prompt: systemPrompt,
        opening_message: opening || undefined,
        model: model || undefined,
        temperature: temperature ? Number(temperature) : undefined,
      };
      const saved = isEdit
        ? await platformApi.updatePersona(persona!.id, body)
        : await platformApi.createPersona({ ...body, slug: slug || undefined });
      onSaved(saved, !isEdit);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
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
        <label>
          名称 *
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：李白" required />
        </label>
        {!isEdit && (
          <label>
            slug（可选，用于 model=&quot;persona:slug&quot;，留空自动生成）
            <input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="libai" />
          </label>
        )}
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
        {error && <p className="auth-error">{error}</p>}
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
          <button type="submit" className="primary" disabled={busy || !name || !systemPrompt}>
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
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

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
