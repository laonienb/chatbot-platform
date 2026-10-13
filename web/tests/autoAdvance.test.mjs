import { test } from "node:test";
import assert from "node:assert/strict";

// Node 24 直接擦除 TS 类型导入 .ts 源（与 api.test.mjs / pacing.test.mjs 同法）。
const {
  AUTO_ADVANCE_KEY_PREFIX,
  clearState,
  counterKey,
  loadRound,
  loadState,
  mergeDueMessage,
  persistState,
  pickDueRules,
  reviveState,
  sanitizeRules,
  saveRound,
  storageKey,
} = await import("../lib/autoAdvance.ts");

// ---------- pickDueRules：触发判据（纯函数） ----------

const R1 = { id: "a", content: "继续", interval: 1, enabled: true };
const R5 = { id: "b", content: "继续推进剧情", interval: 5, enabled: true };

test("每轮回复：interval=1 每轮都到期", () => {
  for (const round of [1, 2, 3, 4, 5, 6, 7, 10]) {
    assert.deepEqual(pickDueRules([R1], round), ["继续"]);
  }
});

test("每5轮：interval=5 只在 5,10 到期", () => {
  assert.deepEqual(pickDueRules([R5], 4), []);
  assert.deepEqual(pickDueRules([R5], 5), ["继续推进剧情"]);
  assert.deepEqual(pickDueRules([R5], 9), []);
  assert.deepEqual(pickDueRules([R5], 10), ["继续推进剧情"]);
});

test("用户示例：①每轮 + ②每5轮 ⇒ 第5轮两条合并", () => {
  assert.deepEqual(pickDueRules([R1, R5], 1), ["继续"]);
  assert.deepEqual(pickDueRules([R1, R5], 4), ["继续"]);
  assert.deepEqual(pickDueRules([R1, R5], 5), ["继续", "继续推进剧情"]);
  assert.deepEqual(pickDueRules([R1, R5], 10), ["继续", "继续推进剧情"]);
});

test("round<1 永不到期；disabled 规则不参与；非法 interval 跳过", () => {
  assert.deepEqual(pickDueRules([R1], 0), []);
  assert.deepEqual(pickDueRules([R1], -3), []);
  assert.deepEqual(pickDueRules([{ ...R1, enabled: false }], 3), []);
  assert.deepEqual(pickDueRules([{ ...R1, interval: 0 }], 3), []);
  assert.deepEqual(pickDueRules([{ ...R1, interval: -2 }], 3), []);
});

test("到期内容保持数组顺序", () => {
  const a = { id: "x", content: "甲", interval: 2, enabled: true };
  const b = { id: "y", content: "乙", interval: 3, enabled: true };
  // 第 6 轮：a(2,4,6) 与 b(3,6) 同时到期
  assert.deepEqual(pickDueRules([a, b], 6), ["甲", "乙"]);
  assert.deepEqual(pickDueRules([b, a], 6), ["乙", "甲"]);
});

// ---------- mergeDueMessage ----------

test("合并：去空白、同内容去重、按序换行拼接", () => {
  assert.equal(mergeDueMessage(["继续", "继续推进剧情"]), "继续\n继续推进剧情");
  assert.equal(mergeDueMessage([" 继续 ", "继续"]), "继续");
  assert.equal(mergeDueMessage(["  ", ""]), "");
  assert.equal(mergeDueMessage([]), "");
});

// ---------- reviveState / sanitizeRules：坏数据降级 ----------

test("reviveState：null/坏 JSON/非对象 ⇒ 空态", () => {
  for (const raw of [null, "", "not json", "123", '"str"']) {
    const s = reviveState(raw);
    assert.deepEqual(s, { rules: [], enabled: false });
  }
});

test("reviveState：坏规则丢弃、好规则保留、enabled 严格 true", () => {
  const s = reviveState(
    JSON.stringify({
      enabled: true,
      rules: [
        { id: "ok", content: "继续", interval: 2, enabled: true },
        { content: "", interval: 2 },
        { content: "x", interval: 0 },
        { content: "x", interval: "abc" },
        null,
        "junk",
      ],
    })
  );
  assert.equal(s.enabled, true);
  assert.equal(s.rules.length, 1);
  assert.equal(s.rules[0].content, "继续");
  assert.equal(s.rules[0].interval, 2);
});

test("reviveState：interval 字符串数字可恢复；缺 id 自动补", () => {
  const s = reviveState(JSON.stringify({ enabled: false, rules: [{ content: "推进", interval: "5" }] }));
  assert.equal(s.rules.length, 1);
  assert.equal(s.rules[0].interval, 5);
  assert.ok(s.rules[0].id);
});

test("sanitizeRules：interval 非法降级为丢、空内容丢弃、enabled 默认真", () => {
  const rules = sanitizeRules([
    { content: "继续", interval: 1 },
    { content: "推进", interval: "5" },
    { content: "", interval: 3 },
    { content: "坏", interval: 0 },
  ]);
  assert.equal(rules.length, 2);
  assert.equal(rules[0].interval, 1);
  assert.equal(rules[1].interval, 5);
  assert.equal(rules.every((r) => r.enabled), true);
});

// ---------- localStorage 往返（node --test 无 localStorage，注入 globalThis 桩） ----------

function withFakeStorage(fn) {
  const store = new Map();
  const fake = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  const saved = globalThis.localStorage;
  globalThis.localStorage = fake;
  try {
    fn(store);
  } finally {
    if (saved === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = saved;
  }
}

test("persist → load 往返一致；clear 后为空态", () => {
  withFakeStorage((store) => {
    const id = "conv-1";
    persistState(id, { enabled: true, rules: [{ id: "a", content: "继续", interval: 1, enabled: true }] });
    assert.equal(store.has(storageKey(id)), true);
    const s = loadState(id);
    assert.equal(s.enabled, true);
    assert.equal(s.rules.length, 1);
    clearState(id);
    assert.deepEqual(loadState(id), { rules: [], enabled: false });
  });
});

test("轮次计数：0 起步、写读一致、负数/坏值归 0", () => {
  withFakeStorage(() => {
    const id = "conv-2";
    assert.equal(loadRound(id), 0);
    for (const r of [1, 2, 5, 10]) {
      saveRound(id, r);
      assert.equal(loadRound(id), r);
    }
    saveRound(id, -1);
    assert.equal(loadRound(id), 0);
  });
});

test("localStorage 抛异常（隐私模式）不 crash", () => {
  const saved = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => {
      throw new Error("denied");
    },
  };
  try {
    persistState("c", { enabled: true, rules: [] });
    assert.deepEqual(loadState("c"), { rules: [], enabled: false });
    assert.equal(loadRound("c"), 0);
    saveRound("c", 3);
    clearState("c");
  } finally {
    if (saved === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = saved;
  }
});

test("key 前缀：按会话隔离", () => {
  assert.equal(storageKey("a"), `${AUTO_ADVANCE_KEY_PREFIX}a`);
  assert.equal(counterKey("a"), `${AUTO_ADVANCE_KEY_PREFIX}a:round`);
  withFakeStorage((store) => {
    persistState("a", { enabled: true, rules: [] });
    assert.equal(loadState("b").enabled, false, "会话 b 读不到会话 a 的开关");
  });
});
