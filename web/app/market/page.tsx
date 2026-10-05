"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { platformApi, type MarketPersona } from "@/lib/api";

export default function MarketPage() {
  const [all, setAll] = useState<MarketPersona[]>([]);
  const [meId, setMeId] = useState<string | null>(null);
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState(""); // 已提交的搜索词
  const [forking, setForking] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  /** 首次：不带过滤拉全量（用于分类标签导航 + 全量展示）。 */
  const loadBase = useCallback(async () => {
    setLoading(true);
    try {
      const [me, ps] = await Promise.all([platformApi.me(), platformApi.market()]);
      setMeId(me.id);
      setAll(ps);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadBase();
  }, [loadBase]);

  /** 提交搜索或切换标签时走服务端过滤；两者都为空则回到全量。 */
  const applyFilters = useCallback(async (q: string, tag: string | null) => {
    if (!q.trim() && !tag) {
      setAll((prev) => prev); // 由 loadBase 恢复，避免闪烁
      await loadBase();
      return;
    }
    setLoading(true);
    try {
      setAll(await platformApi.market({ q: q.trim() || undefined, tag: tag ?? undefined }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "搜索失败");
    } finally {
      setLoading(false);
    }
  }, [loadBase]);

  const tags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const p of all) for (const t of p.tags ?? []) counts.set(t, (counts.get(t) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([t]) => t);
  }, [all]);

  async function submitSearch(e: React.FormEvent) {
    e.preventDefault();
    setQuery(search);
    await applyFilters(search, activeTag);
  }

  async function pickTag(tag: string | null) {
    setActiveTag(tag);
    await applyFilters(query, tag);
  }

  async function fork(p: MarketPersona) {
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

      <form onSubmit={submitSearch} className="market-search-row">
        <input
          className="market-search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索人设名称或人设内容…"
        />
        <button type="submit" className="primary">搜索</button>
      </form>

      <div className="market-chips">
        <button className={`chip ${activeTag === null ? "active" : ""}`} onClick={() => pickTag(null)}>
          全部
        </button>
        {tags.map((t) => (
          <button key={t} className={`chip ${activeTag === t ? "active" : ""}`} onClick={() => pickTag(t)}>
            {t}
          </button>
        ))}
      </div>

      {toast && <div className="toast success">{toast}</div>}
      {error && <div className="toast error">{error}</div>}

      {loading ? (
        <p className="empty market-empty">加载中…</p>
      ) : all.length === 0 ? (
        <p className="empty market-empty">
          {query || activeTag ? "没有匹配的人设" : "市场还是空的 —— 在聊天页编辑人设，把可见性设为「公开」即可发布"}
        </p>
      ) : (
        <div className="market-grid">
          {all.map((p) => {
            const mine = p.owner_id === meId;
            return (
              <div key={p.id} className="market-card">
                <div className="market-card-head">
                  <span className="avatar sm">{p.name[0]}</span>
                  <strong>{p.name}</strong>
                  {mine && <span className="tag mine">我发布的</span>}
                </div>
                <p className="market-prompt">{p.system_prompt}</p>
                {p.tags && p.tags.length > 0 && (
                  <div className="market-tags">
                    {p.tags.map((t) => (
                      <button key={t} className="tag link" onClick={() => pickTag(t)}>
                        {t}
                      </button>
                    ))}
                  </div>
                )}
                <div className="market-card-foot">
                  <small className="owner">{p.owner_name ? `by ${p.owner_name}` : "匿名"}</small>
                  {mine ? (
                    <small className="owner">去聊天页「✎ 编辑」管理</small>
                  ) : (
                    <button className="primary" onClick={() => fork(p)} disabled={forking === p.id}>
                      {forking === p.id ? "复制中…" : "复制到我的"}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </main>
  );
}
