"use client";

import { useCallback, useEffect, useState } from "react";
import { platformApi, type Memory, type Persona } from "@/lib/api";

/** 人设的长期记忆管理：查看/添加/编辑/删除 + 记忆开关。 */
export default function MemoryModal({
  persona,
  onClose,
  onPersonaUpdated,
}: {
  persona: Persona;
  onClose: () => void;
  onPersonaUpdated: (p: Persona) => void;
}) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [enabled, setEnabled] = useState(persona.memory_enabled);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setMemories(await platformApi.memories(persona.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    }
  }, [persona.id]);

  useEffect(() => {
    load();
  }, [load]);

  async function toggle() {
    const next = !enabled;
    setEnabled(next); // 乐观更新
    try {
      onPersonaUpdated(await platformApi.updatePersona(persona.id, { memory_enabled: next }));
    } catch (err) {
      setEnabled(!next);
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await platformApi.addMemory(persona.id, draft.trim());
      setDraft("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "添加失败");
    } finally {
      setBusy(false);
    }
  }

  async function edit(m: Memory) {
    const content = prompt("修改记忆", m.content);
    if (content === null || !content.trim()) return;
    try {
      await platformApi.updateMemory(m.id, content.trim());
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "修改失败");
    }
  }

  async function remove(m: Memory) {
    try {
      await platformApi.deleteMemory(m.id);
      setMemories((prev) => prev.filter((x) => x.id !== m.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal memory-modal" onClick={(e) => e.stopPropagation()}>
        <h3>长期记忆 · {persona.name}</h3>
        <p className="hint">
          人设会记住这些关于你的事实，并在每次对话时想起它们。聊天中提到的新信息也会被自动记住。
        </p>

        <label className="memory-toggle">
          <span>启用长期记忆</span>
          <button className={enabled ? "switch on" : "switch"} onClick={toggle} title="开关">
            <span className="knob" />
          </button>
        </label>

        <form onSubmit={add} className="memory-add">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="手动添加一条记忆，如：我是上海人"
          />
          <button type="submit" className="primary" disabled={busy || !draft.trim()}>
            添加
          </button>
        </form>

        {error && <p className="auth-error">{error}</p>}

        <div className="memory-list">
          {memories.length === 0 ? (
            <p className="empty">还没有记忆 —— 多聊聊，或手动添加</p>
          ) : (
            memories.map((m) => (
              <div key={m.id} className="memory-item">
                <span className="memory-content">{m.content}</span>
                <span className="memory-meta">
                  <small>{m.source === "chat" ? "自动" : "手动"}</small>
                  <button className="icon-btn" title="编辑" onClick={() => edit(m)}>
                    ✎
                  </button>
                  <button className="icon-btn danger" title="删除" onClick={() => remove(m)}>
                    ×
                  </button>
                </span>
              </div>
            ))
          )}
        </div>

        <div className="modal-actions">
          <span className="spacer" />
          <button type="button" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
