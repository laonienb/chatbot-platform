"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { platformApi, ApiError, type LlmModel } from "@/lib/api";
import { IconPencil, IconStar } from "@/components/Icons";

/** 模型管理（管理员）：注册表 CRUD。api_key 不回显（只有 has_key），空串表示不修改。 */
export default function ModelsPage() {
  const [models, setModels] = useState<LlmModel[]>([]);
  const [notAdmin, setNotAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // 新建表单
  const [name, setName] = useState("");
  const [model, setModel] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [isDefault, setIsDefault] = useState(false);

  // 编辑弹窗（api_base / api_key / 排序；名称与模型串创建后不可改）
  const [editing, setEditing] = useState<LlmModel | null>(null);
  const [editBase, setEditBase] = useState("");
  const [editKey, setEditKey] = useState("");
  const [editSort, setEditSort] = useState("0");

  const flash = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2500);
  };

  const load = useCallback(async () => {
    try {
      setModels(await platformApi.adminModels());
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setNotAdmin(true);
      } else {
        setError(err instanceof Error ? err.message : "加载失败");
      }
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !model.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await platformApi.createModel({
        name: name.trim(),
        model: model.trim(),
        api_base: apiBase.trim() || undefined,
        api_key: apiKey.trim() || undefined,
        is_default: isDefault || undefined,
        sort: models.length,
      });
      setName("");
      setModel("");
      setApiBase("");
      setApiKey("");
      setIsDefault(false);
      flash("模型已注册");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function patch(id: string, body: Record<string, unknown>, okMsg: string) {
    setError(null);
    try {
      await platformApi.updateModel(id, body);
      await load();
      flash(okMsg);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  async function remove(m: LlmModel) {
    if (!confirm(`删除模型「${m.name}」？已选它的会话将回退到默认模型。`)) return;
    setError(null);
    try {
      await platformApi.deleteModel(m.id);
      await load();
      flash("已删除");
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  }

  function openEdit(m: LlmModel) {
    setEditing(m);
    setEditBase(m.api_base ?? "");
    setEditKey("");
    setEditSort(String(m.sort));
  }

  async function saveEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editing) return;
    setBusy(true);
    setError(null);
    try {
      await platformApi.updateModel(editing.id, {
        api_base: editBase.trim() || null,
        // 留空 = 不修改密钥（后端契约）
        ...(editKey.trim() ? { api_key: editKey.trim() } : {}),
        sort: Number(editSort) || 0,
      });
      setEditing(null);
      flash("已保存");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="keys-page">
      <header className="keys-header">
        <Link href="/chat" className="back-link">
          ← 返回聊天
        </Link>
        <h1>模型管理</h1>
      </header>

      {toast && <div className="toast success">{toast}</div>}
      {error && <div className="toast error">{error}</div>}

      {notAdmin ? (
        <section className="keys-card">
          <p className="empty">需要管理员权限 —— 让管理员执行 python -m scripts.make_admin 你的邮箱</p>
        </section>
      ) : (
        <>
          <section className="keys-card">
            <h2>注册新模型</h2>
            <form onSubmit={create} className="model-create">
              <div className="field-row">
                <label>
                  名称 *
                  <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：自部署 Qwen" required />
                </label>
                <label>
                  模型串（LiteLLM 格式）*
                  <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="openai/qwen-72b 或 deepseek-chat" required />
                </label>
              </div>
              <div className="field-row">
                <label>
                  API 地址（可选，自部署/中转端点）
                  <input value={apiBase} onChange={(e) => setApiBase(e.target.value)} placeholder="http://localhost:8000/v1" />
                </label>
                <label>
                  API Key（可选）
                  <input value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…" />
                </label>
              </div>
              <label className="check-row">
                <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
                设为默认模型（新会话的默认选择）
              </label>
              <div className="modal-actions">
                <button type="submit" className="primary" disabled={busy || !name.trim() || !model.trim()}>
                  注册
                </button>
              </div>
            </form>
          </section>

          <section className="keys-card">
            <h2>已注册模型（{models.length}）</h2>
            {models.length === 0 ? (
              <p className="empty">还没有注册任何模型</p>
            ) : (
              <div className="model-list">
                {models.map((m) => (
                  <div key={m.id} className={`model-row ${m.enabled ? "" : "disabled"}`}>
                    <div className="model-row-main">
                      <strong>
                        {m.name}
                        {m.is_default && <span className="tag default">默认</span>}
                        {!m.enabled && <span className="tag off">已停用</span>}
                      </strong>
                      <code>{m.model}</code>
                      <small>
                        {m.api_base ? m.api_base : "平台默认端点"} · {m.has_key ? "已配密钥" : "无独立密钥"}
                      </small>
                    </div>
                    <div className="model-row-actions">
                      <button
                        className={`switch ${m.enabled ? "on" : ""}`}
                        title={m.enabled ? "停用" : "启用"}
                        onClick={() => patch(m.id, { enabled: !m.enabled }, m.enabled ? "已停用" : "已启用")}
                      >
                        <span className="knob" />
                      </button>
                      <button
                        className="icon-btn"
                        title={m.is_default ? "已是默认" : "设为默认"}
                        onClick={() => patch(m.id, { is_default: true }, "已设为默认")}
                        disabled={m.is_default}
                      >
                        <IconStar size={15} filled={m.is_default} />
                      </button>
                      <button className="icon-btn" title="编辑端点/密钥/排序" onClick={() => openEdit(m)}>
                        <IconPencil size={14} />
                      </button>
                      <button className="icon-btn danger" title="删除" onClick={() => remove(m)}>
                        ×
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <p className="hint">
              模型串是 LiteLLM 格式：官方模型直接写名字（如 deepseek-chat），自部署/中转写
              provider/模型名（如 openai/qwen-72b）并配 API 地址。停用的模型不出现在聊天下拉里。
            </p>
          </section>
        </>
      )}

      {editing && (
        <div className="modal-mask" onClick={() => setEditing(null)}>
          <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={saveEdit}>
            <h3>编辑「{editing.name}」</h3>
            <p className="hint">名称与模型串创建后不可修改；密钥留空表示不修改。</p>
            <label>
              API 地址（留空则用平台默认端点）
              <input value={editBase} onChange={(e) => setEditBase(e.target.value)} placeholder="http://localhost:8000/v1" />
            </label>
            <label>
              API Key（{editing.has_key ? "已配置" : "未配置"}；留空不修改）
              <input value={editKey} onChange={(e) => setEditKey(e.target.value)} placeholder="sk-…" />
            </label>
            <label>
              排序（越小越靠前）
              <input value={editSort} onChange={(e) => setEditSort(e.target.value)} type="number" />
            </label>
            <div className="modal-actions">
              <button type="button" onClick={() => setEditing(null)}>
                取消
              </button>
              <button type="submit" className="primary" disabled={busy}>
                保存
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}
