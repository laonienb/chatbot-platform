"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { platformApi, type Persona } from "@/lib/api";

export default function MarketPage() {
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [meId, setMeId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [forking, setForking] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [me, ps] = await Promise.all([platformApi.me(), platformApi.personas()]);
      setMeId(me.id);
      setPersonas(ps.filter((p) => p.visibility === "public" && p.owner_id !== me.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return personas;
    return personas.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        (p.tags ?? []).some((t) => t.toLowerCase().includes(q))
    );
  }, [personas, search]);

  async function fork(p: Persona) {
    setForking(p.id);
    setError(null);
    try {
      await platformApi.forkPersona(p.id);
      setToast(`「${p.name}」已复制到你的人设，去聊天页开始对话`);
      setTimeout(() => setToast(null), 3000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "复制失败");
    } finally {
      setForking(null);
    }
  }

  return (
    <main className="market-page">
      <header className="keys-header">
        <Link href="/chat" className="back-link">
          ← 返回聊天
        </Link>
        <h1>人设市场</h1>
      </header>

      <input
        className="market-search"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="搜索人设名称或标签…"
      />

      {toast && <div className="toast success">{toast}</div>}
      {error && <div className="toast error">{error}</div>}

      {filtered.length === 0 ? (
        <p className="empty market-empty">
          {search ? "没有匹配的人设" : "市场还是空的 —— 在聊天页编辑人设，把可见性设为「公开」即可发布"}
        </p>
      ) : (
        <div className="market-grid">
          {filtered.map((p) => (
            <div key={p.id} className="market-card">
              <div className="market-card-head">
                <span className="avatar sm">{p.name[0]}</span>
                <strong>{p.name}</strong>
              </div>
              <p className="market-prompt">{p.system_prompt}</p>
              {p.tags && p.tags.length > 0 && (
                <div className="market-tags">
                  {p.tags.map((t) => (
                    <span key={t} className="tag">
                      {t}
                    </span>
                  ))}
                </div>
              )}
              <button className="primary" onClick={() => fork(p)} disabled={forking === p.id}>
                {forking === p.id ? "复制中…" : "复制到我的"}
              </button>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
