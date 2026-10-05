"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { authApi } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "login") {
        await authApi.login(email, password);
      } else {
        await authApi.register(email, password, displayName);
      }
      router.push("/chat");
    } catch (err) {
      setError(err instanceof Error ? err.message : "出错了，请重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <div className="auth-card">
        <h1>Chatbot Platform</h1>
        <div className="auth-tabs">
          <button className={mode === "login" ? "tab active" : "tab"} onClick={() => setMode("login")}>
            登录
          </button>
          <button className={mode === "register" ? "tab active" : "tab"} onClick={() => setMode("register")}>
            注册
          </button>
        </div>
        <form onSubmit={submit}>
          <label>
            邮箱
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              required
            />
          </label>
          {mode === "register" && (
            <label>
              昵称（可选）
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="怎么称呼你"
              />
            </label>
          )}
          <label>
            密码
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === "register" ? "至少 8 位" : "你的密码"}
              minLength={mode === "register" ? 8 : undefined}
              required
            />
          </label>
          {error && <p className="auth-error">{error}</p>}
          <button type="submit" className="primary" disabled={busy}>
            {busy ? "请稍候…" : mode === "login" ? "登录" : "注册并登录"}
          </button>
        </form>
      </div>
    </main>
  );
}
