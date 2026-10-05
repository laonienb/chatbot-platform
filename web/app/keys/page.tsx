"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { platformApi, type ApiKey, type ApiKeyCreated, type Usage } from "@/lib/api";

export default function KeysPage() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [name, setName] = useState("");
  const [whitelist, setWhitelist] = useState("");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [ks, us] = await Promise.all([platformApi.keys(), platformApi.usage(30)]);
      setKeys(ks);
      setUsage(us);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function createKey(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const models = whitelist
        .split(/[,，\s]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const key = await platformApi.createKey({
        name: name.trim(),
        model_whitelist: models.length ? models : undefined,
      });
      setCreated(key);
      setName("");
      setWhitelist("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    if (!confirm("吊销后使用该 Key 的应用（如机器人）将立即失效，确定？")) return;
    try {
      await platformApi.revokeKey(id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "吊销失败");
    }
  }

  async function copyKey() {
    if (!created) return;
    await navigator.clipboard.writeText(created.key);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <main className="keys-page">
      <header className="keys-header">
        <Link href="/chat" className="back-link">
          ← 返回聊天
        </Link>
        <h1>API 密钥与用量</h1>
      </header>

      {error && <div className="toast error">{error}</div>}

      <section className="keys-card">
        <h2>用量（近 30 天）</h2>
        {usage ? (
          usage.total_requests === 0 ? (
            <p className="empty">还没有任何调用记录</p>
          ) : (
            <>
              <div className="usage-summary">
                <div>
                  <small>调用次数</small>
                  <strong>{usage.total_requests}</strong>
                </div>
                <div>
                  <small>Prompt tokens</small>
                  <strong>{usage.total_prompt_tokens.toLocaleString()}</strong>
                </div>
                <div>
                  <small>Completion tokens</small>
                  <strong>{usage.total_completion_tokens.toLocaleString()}</strong>
                </div>
                <div>
                  <small>合计 tokens</small>
                  <strong>
                    {(usage.total_prompt_tokens + usage.total_completion_tokens).toLocaleString()}
                  </strong>
                </div>
              </div>
              <table className="keys-table">
                <thead>
                  <tr>
                    <th>模型</th>
                    <th>次数</th>
                    <th>Prompt</th>
                    <th>Completion</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.by_model.map((m) => (
                    <tr key={m.model}>
                      <td>{m.model}</td>
                      <td>{m.requests}</td>
                      <td>{m.prompt_tokens.toLocaleString()}</td>
                      <td>{m.completion_tokens.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )
        ) : (
          <p className="empty">加载中…</p>
        )}
      </section>

      <section className="keys-card">
        <h2>创建密钥</h2>
        <p className="hint">
          给外部应用（如 AstrBot 机器人框架）使用的凭证，格式 sk-…，创建后仅显示一次。
        </p>
        <form onSubmit={createKey} className="key-form">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="名称，如：我的QQ机器人" required />
          <input
            value={whitelist}
            onChange={(e) => setWhitelist(e.target.value)}
            placeholder="模型白名单（可选，逗号分隔，如 persona:libai, gpt-4o-mini）"
          />
          <button type="submit" className="primary" disabled={busy || !name.trim()}>
            {busy ? "创建中…" : "生成密钥"}
          </button>
        </form>

        {created && (
          <div className="key-reveal">
            <p>
              <strong>{created.name}</strong> 的新密钥（<em>仅此一次显示，请立即保存</em>）：
            </p>
            <div className="key-row">
              <code>{created.key}</code>
              <button onClick={copyKey}>{copied ? "已复制 ✓" : "复制"}</button>
            </div>
          </div>
        )}
      </section>

      <section className="keys-card">
        <h2>我的密钥</h2>
        {keys.length === 0 ? (
          <p className="empty">还没有密钥，用上面的表单生成</p>
        ) : (
          <table className="keys-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>密钥</th>
                <th>白名单</th>
                <th>最近使用</th>
                <th>创建时间</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td>{k.name}</td>
                  <td>
                    <code>{k.key_prefix}…</code>
                  </td>
                  <td>{k.model_whitelist ? k.model_whitelist.join(", ") : "不限"}</td>
                  <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "从未"}</td>
                  <td>{new Date(k.created_at).toLocaleDateString()}</td>
                  <td>
                    <button className="danger" onClick={() => revoke(k.id)}>
                      吊销
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
