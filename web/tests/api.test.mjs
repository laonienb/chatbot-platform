/**
 * web/lib/api.ts 的回归测试：零依赖（node:test + Node 24 的 TS 类型擦除，不需要 npm install）。
 *
 * 跑法：npm test（或 node --test web/tests/），需要 web 目录为当前工作目录的上级。
 *
 * 只测 lib/api.ts 里"判读分支"这类纯逻辑 —— 不发真请求、不起浏览器：fetch/localStorage/window
 * 都用桩替换，响应体形状取自后端实现（原生面 {"detail":…} 与兼容面 {"error":{…}} 两种信封）。
 * 每条带 ★ 的用例锁死一个曾经真实存在过的缺陷：它们在没有回归网时会静默倒退。
 */
import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
globalThis.window = { location: { href: "" } };

const { api, streamSSE, sendMessageStream, ApiError } = await import("../lib/api.ts");

/** 记录请求次数的 fetch 桩：按队列依次返回，队列空则复用最后一项。 */
let calls = [];
function stubFetch(...responses) {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return responses.shift() ?? responses[responses.length - 1] ?? new Response("{}", { status: 200 });
  };
}

const json = (status, body, headers = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

const sse = (...frames) =>
  new Response(
    new ReadableStream({
      start(c) {
        const enc = new TextEncoder();
        for (const f of frames) c.enqueue(enc.encode(f));
        c.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } }
  );

const chunk = (content) => `data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\n`;

beforeEach(() => {
  store.clear();
  store.set("cp_access_token", "t");
  store.set("cp_refresh_token", "r");
  globalThis.window.location.href = "";
});

async function captureError(...args) {
  try {
    await api(...args);
  } catch (e) {
    assert.ok(e instanceof ApiError, `抛出的不是 ApiError：${e}`);
    return e;
  }
  throw new Error("预期抛错，实际正常返回");
}

describe("错误判读：code 优先，缺失才回退状态码 + Retry-After", () => {
  test("原生面 402 → balance，并追加充值引导", async () => {
    stubFetch(json(402, { detail: "积分余额不足（余额 3，本次预估需 12）" }));
    const e = await captureError("/api/v1/conversations/1/messages");
    assert.equal(e.kind, "balance");
    assert.match(e.message, /积分余额不足.*请充值或联系管理员/);
  });

  test("★ 兼容面 402 的真实文案不丢（旧实现只读 detail，会退化成「请求失败 (402)」）", async () => {
    stubFetch(json(402, { error: { message: "insufficient balance: need 12", type: "insufficient_balance" } }));
    const e = await captureError("/v1/chat/completions");
    assert.equal(e.kind, "balance");
    assert.match(e.message, /insufficient balance: need 12/);
    assert.doesNotMatch(e.message, /请求失败/);
  });

  test("★ 上游 429 自带 Retry-After 时，budget_exhausted 仍判 quota（旧启发式会误判成限流并自动退避重试）", async () => {
    stubFetch(json(429, { error: { message: "quota exceeded", code: "budget_exhausted" } }, { "Retry-After": "30" }));
    const e = await captureError("/v1/chat/completions");
    assert.equal(e.kind, "quota");
    assert.equal(e.retryAfter, undefined);
    assert.match(e.message, /重试无效/);
  });

  test("code=rate_limited + Retry-After → rate_limit 并带上退避秒数", async () => {
    stubFetch(json(429, { detail: "慢一点", code: "rate_limited" }, { "Retry-After": "47" }));
    const e = await captureError("/api/v1/conversations/1/messages");
    assert.equal(e.kind, "rate_limit");
    assert.equal(e.retryAfter, 47);
    assert.equal(e.code, "rate_limited");
  });

  test("无 code 的 429：带 Retry-After 判限流、不带的判额度用尽（旧行为不倒退）", async () => {
    stubFetch(json(429, { detail: "rate limit exceeded" }, { "Retry-After": "12" }));
    assert.equal((await captureError("/api/v1/x")).kind, "rate_limit");

    stubFetch(json(429, { detail: "quota exceeded" }));
    assert.equal((await captureError("/api/v1/x")).kind, "quota");
  });

  test("Retry-After 非法值（非数字 / 0 / 负数）不当作退避依据", async () => {
    for (const raw of ["abc", "0", "-5", ""]) {
      stubFetch(json(429, { detail: "限流" }, { "Retry-After": raw }));
      const e = await captureError("/api/v1/x");
      assert.equal(e.retryAfter, undefined, `Retry-After: "${raw}" 不应产出秒数`);
      assert.equal(e.kind, "quota");
    }
  });

  test("503 → service；413 → context；403 合规 → forbidden；404 模型 → model_missing", async () => {
    stubFetch(new Response("Upstream Not Ready", { status: 503 }));
    const svc = await captureError("/api/v1/x");
    assert.equal(svc.kind, "service");
    assert.equal(svc.message, "服务繁忙，请稍后重试");

    stubFetch(json(413, { error: { message: "context too long", code: "context_length_exceeded" } }));
    assert.equal((await captureError("/v1/chat/completions")).kind, "context");

    stubFetch(json(403, { detail: "内容不合规", code: "compliance_denied" }));
    assert.equal((await captureError("/api/v1/x")).kind, "forbidden");

    stubFetch(json(404, { error: { message: "no such model", code: "model_not_found" } }));
    assert.equal((await captureError("/v1/chat/completions")).kind, "model_missing");
  });

  test("★ 畸形 code（对象/数字）不炸，回落到状态码启发式", async () => {
    stubFetch(json(429, { detail: "限流", code: { oops: 1 } }, { "Retry-After": "8" }));
    const e = await captureError("/api/v1/x");
    assert.equal(e.kind, "rate_limit");
    assert.equal(e.retryAfter, 8);

    // code 是数字（不是字符串）时被丢弃，但同信封里的 message 必须留下 —— 兜底文案
    // 只该在"根本没有可用文案"时出现，否则真实原因又被吞掉了。
    stubFetch(json(500, { error: { message: "boom", code: 42 } }));
    const e2 = await captureError("/v1/chat/completions");
    assert.equal(e2.code, undefined);
    assert.equal(e2.kind, undefined);
    assert.equal(e2.message, "boom");
  });

  test("错误体不是 JSON 时用兜底文案，不因解析失败而丢状态码", async () => {
    stubFetch(new Response("<html>502</html>", { status: 502 }));
    const e = await captureError("/api/v1/x");
    assert.equal(e.status, 502);
    assert.equal(e.message, "请求失败 (502)");
    assert.equal(e.kind, undefined);
  });

  test("401 先用 refresh token 换新 token 重试一次", async () => {
    stubFetch(
      json(401, { detail: "expired" }),
      json(200, { access_token: "t2", refresh_token: "r2" }), // /auth/refresh
      json(200, { ok: true })
    );
    const out = await api("/api/v1/me/usage");
    assert.deepEqual(out, { ok: true });
    assert.equal(store.get("cp_access_token"), "t2");
    assert.equal(calls.length, 3);
    assert.match(calls[1].url, /\/api\/v1\/auth\/refresh$/);
  });

  test("401 且刷新失败 → 清 token 跳登录，不做第三次尝试", async () => {
    stubFetch(json(401, { detail: "expired" }), json(401, { detail: "refresh 也坏了" }));
    const e = await captureError("/api/v1/me/usage").catch((x) => x);
    assert.equal(e.status, 401);
    assert.equal(store.has("cp_access_token"), false);
    assert.equal(globalThis.window.location.href, "/login");
    assert.equal(calls.length, 2);
  });
});

describe("SSE 解析：内容收集、宽松忽略、流内错误不静默", () => {
  const collect = async (resp) => {
    let text = "";
    await streamSSE(resp, { onDelta: (t) => (text += t) });
    return text;
  };

  test("多 chunk 聚合，[DONE] 结束", async () => {
    assert.equal(await collect(sse(chunk("你好"), chunk("，世界"), "data: [DONE]\n\n", "data: 不该被读\n\n")), "你好，世界");
  });

  test("★ 流内错误帧要抛出（此前被丢弃 → 用户看到空回复且无任何提示）", async () => {
    let text = "";
    await assert.rejects(
      streamSSE(sse(chunk("前半句"), `data: ${JSON.stringify({ error: { message: "上游超时", code: "upstream_error" } })}\n\n`), {
        onDelta: (t) => (text += t),
      }),
      (e) => {
        assert.ok(e instanceof ApiError);
        assert.equal(e.kind, "service");
        assert.equal(e.code, "upstream_error");
        return true;
      }
    );
    assert.equal(text, "前半句", "已产出的内容不能被丢掉");
  });

  test("心跳注释与 event: model_service 终止事件按协议 §6 宽松忽略", async () => {
    const frames = [": keepalive\n\n", chunk("A"), "event: model_service\ndata: {\"finish_reason\":\"stop\"}\n\n", chunk("B"), "data: [DONE]\n\n"];
    assert.equal(await collect(sse(...frames)), "AB");
  });

  test("跨网络块劈开的半帧要缓冲后拼回", async () => {
    const resp = new Response(
      new ReadableStream({
        start(c) {
          const enc = new TextEncoder();
          const whole = chunk("拼接") + "data: [DONE]\n\n";
          for (const ch of whole) c.enqueue(enc.encode(ch)); // 一次一个字符
          c.close();
        },
      }),
      { status: 200 }
    );
    assert.equal(await collect(resp), "拼接");
  });

  test("无 content 的收尾 chunk（usage / finish_reason）不产生增量也不抛", async () => {
    const tail = `data: ${JSON.stringify({ choices: [], usage: { total_tokens: 7 } })}\n\n`;
    assert.equal(await collect(sse(chunk("只有我"), tail)), "只有我");
  });
});

describe("发送流式的 429 自动退避：只重发平台自身 RPM 限流", () => {
  const send = async (onNotice) => {
    const deltas = [];
    await sendMessageStream("c1", "内容", (t) => deltas.push(t), undefined, onNotice);
    return deltas;
  };
  const sendAborted = (signal, onNotice) => sendMessageStream("c1", "内容", () => {}, signal, onNotice);

  test("平台自有 RPM 429（无 code + Retry-After）→ 回显等待并自动重发一次", async () => {
    stubFetch(
      json(429, { detail: "rate limit exceeded" }, { "Retry-After": "1" }),
      sse(chunk("重发后的回复"), "data: [DONE]\n\n")
    );
    const notices = [];
    assert.deepEqual(await send((m) => notices.push(m)), ["重发后的回复"]);
    assert.equal(calls.length, 2);
    assert.match(notices.join(), /1 秒后自动重试/);
  });

  test("★ 带 code=rate_limited 的 429 不重发 —— 它发生在用户消息落库之后，重发会重复落库", async () => {
    stubFetch(json(429, { detail: "上游限流", code: "rate_limited" }, { "Retry-After": "1" }));
    const e = await send().catch((x) => x);
    assert.equal(e.kind, "rate_limit");
    assert.equal(calls.length, 1, "只该发一次");
  });

  test("Retry-After 超过 60s 不自动等（让用户自己决定），503 同样不重发", async () => {
    stubFetch(json(429, { detail: "rate limit exceeded" }, { "Retry-After": "61" }));
    await assert.rejects(send());
    assert.equal(calls.length, 1);

    stubFetch(json(503, { detail: "模型服务暂不可用，请稍后再试" }));
    const e = await send().catch((x) => x);
    assert.equal(e.kind, "service");
    assert.equal(calls.length, 1);
  });

  test("★ 退避等待开始前就已 abort → 立即中断（不重发）", async () => {
    stubFetch(
      json(429, { detail: "rate limit exceeded" }, { "Retry-After": "30" }),
      sse(chunk("永远不该到达"), "data: [DONE]\n\n")
    );
    const ac = new AbortController();
    const p = sendMessageStream("c1", "内容", () => {}, ac.signal, () => {});
    ac.abort();
    const t0 = Date.now();
    await assert.rejects(p, (e) => e.name === "AbortError");
    assert.ok(Date.now() - t0 < 500, "不能白等满 30 秒再重发");
    assert.equal(calls.length, 1);
  });

  test("退避等待进行中才 abort → 同样立即中断", async () => {
    stubFetch(
      json(429, { detail: "rate limit exceeded" }, { "Retry-After": "30" }),
      sse(chunk("永远不该到达"), "data: [DONE]\n\n")
    );
    const ac = new AbortController();
    setTimeout(() => ac.abort(), 40);
    const t0 = Date.now();
    await assert.rejects(sendAborted(ac.signal, () => {}), (e) => e.name === "AbortError");
    assert.ok(Date.now() - t0 < 2000);
    assert.equal(calls.length, 1);
  });
});
