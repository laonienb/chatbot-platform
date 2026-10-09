"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { platformApi, ApiError, type MarginSummary } from "@/lib/api";
import { IconRefresh } from "@/components/Icons";

/** 负毛利明细的取数量档位（后端上限 200）。 */
const LIMITS = [20, 50, 200];

const int = (n: number) => n.toLocaleString("zh-CN");
const credit = (n: number) => n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
/** 成本可以是极小的真实值（mock 后端约 5e-5），固定两位小数会把它渲染成 0.00。 */
const usd = (n: number) => {
  const a = Math.abs(n);
  if (a !== 0 && a < 0.01) {
    return n.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 8 });
  }
  return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
/** 上游成本趋近 0 时比率会放大到失真（实测达 4192 倍），超过 10000% 只标注量级。 */
const pct = (r: number | null) => {
  if (r === null) return "—";
  const v = r * 100;
  if (Math.abs(v) >= 10000) return "> 10000%";
  return `${v.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}%`;
};
const time = (iso: string | null) => (iso ? new Date(iso).toLocaleString("zh-CN") : "—");

/** 管理后台（管理员）：毛利监控 + 手动对账。数据口径见页脚说明。 */
export default function AdminPage() {
  const [data, setData] = useState<MarginSummary | null>(null);
  const [limit, setLimit] = useState(LIMITS[0]);
  const [notAdmin, setNotAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const flash = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 2500);
  };

  const load = useCallback(async () => {
    try {
      setData(await platformApi.adminMargin(limit));
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setNotAdmin(true);
      } else {
        setError(err instanceof Error ? err.message : "加载失败");
      }
    }
  }, [limit]);

  useEffect(() => {
    load();
  }, [load]);

  async function reconcile() {
    setBusy(true);
    setError(null);
    try {
      const { abandoned } = await platformApi.adminReconcile();
      flash(abandoned > 0 ? `已收编 ${abandoned} 条残留账行` : "没有需要收编的残留账行");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "对账失败");
    } finally {
      setBusy(false);
    }
  }

  if (notAdmin) {
    return (
      <main className="admin-page">
        <Link href="/chat" className="back-link">
          ← 返回聊天
        </Link>
        <section className="keys-card">
          <h2>管理后台</h2>
          <p className="empty">需要管理员权限。当前账号无法查看全站毛利与对账数据。</p>
        </section>
      </main>
    );
  }

  const negative = data !== null && data.margin < 0;

  return (
    <main className="admin-page">
      <header className="keys-header">
        <div>
          <h1>管理后台</h1>
          <p className="hint">全站毛利监控与账本对账</p>
        </div>
        <Link href="/chat" className="back-link">
          返回聊天
        </Link>
      </header>

      {error && <p className="auth-error">{error}</p>}
      {toast && <div className="toast">{toast}</div>}

      <section className="keys-card">
        <div className="admin-card-head">
          <h2>毛利概览</h2>
          <button className="copy-btn" onClick={load} disabled={!data}>
            刷新
          </button>
        </div>

        {!data ? (
          <p className="empty">加载中…</p>
        ) : data.requests === 0 ? (
          <p className="empty">还没有已结算的计费账行</p>
        ) : (
          <>
            <div className="usage-summary">
              <div>
                <small>已结算账行</small>
                <strong>{int(data.requests)}</strong>
              </div>
              <div>
                <small>账面收入（USD）</small>
                <strong>{usd(data.revenue_usd)}</strong>
              </div>
              <div>
                <small>上游成本（USD）</small>
                <strong>{usd(data.upstream_total)}</strong>
              </div>
              <div>
                <small>毛利（USD）</small>
                <strong className={negative ? "stat-neg" : "stat-pos"}>{usd(data.margin)}</strong>
              </div>
              <div>
                <small>毛利率</small>
                <strong className={negative ? "stat-neg" : ""}>{pct(data.margin_ratio)}</strong>
              </div>
            </div>
            <p className="hint">
              原始账面收入 {credit(data.billed_total)} 积分；积分按后端配置的兑换率折算为上方 USD
              收入，前端不做换算。
            </p>
          </>
        )}
      </section>

      {data && data.violations > 0 && (
        <div className="admin-alert">
          <strong>{int(data.violations)} 条账行卖价低于上游成本</strong>
          <span>这通常是费率规则或积分兑换率配错——钱在无声地亏，需要人工核对费率。</span>
        </div>
      )}

      <section className="keys-card">
        <div className="admin-card-head">
          <h2>负毛利明细</h2>
          {data && data.requests > 0 && (
            <div className="admin-limits">
              {LIMITS.map((n) => (
                <button
                  key={n}
                  className={`chip${limit === n ? " active" : ""}`}
                  onClick={() => setLimit(n)}
                >
                  {n}
                </button>
              ))}
            </div>
          )}
        </div>

        {!data ? (
          <p className="empty">加载中…</p>
        ) : data.violations_detail.length === 0 ? (
          <p className="empty">
            {data.violations === 0 ? "没有卖价低于成本的账行" : "本页无明细，可尝试更大的取数量"}
          </p>
        ) : (
          <table className="keys-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>用户</th>
                <th>模型</th>
                <th>卖价（积分）</th>
                <th>成本（USD）</th>
              </tr>
            </thead>
            <tbody>
              {data.violations_detail.map((r) => (
                <tr key={r.id}>
                  <td>{time(r.created_at)}</td>
                  <td title={r.user_id}>{r.user_id.slice(0, 8)}</td>
                  <td>{r.model || "—"}</td>
                  <td>{credit(r.cost_billed)}</td>
                  <td>{usd(r.cost_upstream)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data && data.violations > data.violations_detail.length && (
          <p className="hint">
            共 {int(data.violations)} 条，当前仅列出 {int(data.violations_detail.length)} 条。
          </p>
        )}
      </section>

      <section className="keys-card">
        <h2>账本对账</h2>
        <p className="hint">
          把进程被中断后残留的 pending 账行收编为 abandoned（按预扣估算结清）。正常由后台循环自动执行，
          这里用于手动立即触发一次。
        </p>
        <div className="admin-actions">
          <button className="primary" onClick={reconcile} disabled={busy}>
            <IconRefresh size={15} />
            {busy ? "对账中…" : "立即对账"}
          </button>
        </div>
      </section>

      <section className="keys-card">
        <h2>配置入口</h2>
        <div className="admin-actions">
          <Link href="/models" className="copy-btn">
            模型管理
          </Link>
        </div>
      </section>

      <p className="admin-note">
        统计口径：仅计入 status=settled 且 currency=credit 的账行，为<strong>全站累计、无时间窗</strong>；
        以 USD 兜底记账的历史行不参与毛利汇总，因此本表不等于全站用量。毛利率在上游成本为 0
        时无意义，显示为 —；成本趋近 0（如 mock 后端）时比率会放大失真，超过 10000% 仅标注量级。
      </p>
    </main>
  );
}
