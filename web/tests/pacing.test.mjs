/**
 * W-4 §1-2 自适应节奏缓冲的回归测试。零依赖（node:test + Node 24 的 TS 类型擦除）。
 *
 * 分两层：
 * 1. **解析层**——`releaseRate(q)` 必须逐点等于 BASE·e^(q/K)，且处处连续、单调递增。
 *    这一层是 S3「不许阈值/分档」的**机器判据**：任何 `if (q > N) …` 的分段实现
 *    都会在某些 q 上偏离解析式，而"只看几个点单调递增"是拦不住的（分段也能单调）。
 * 2. **运行层**——`PacedReveal` 用注入的假时钟/假调度器逐帧推进，可复现地量出
 *    「300 字一次性到货要放多久」「1 字/100ms 的慢上游跟得多紧」，对应判据
 *    S1/S2/R1/R2/R3 与 prefers-reduced-motion。
 *
 * 每条带 ★ 的用例都是一个**已经想过会被怎样做错**的防线。
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";

const {
  PacedReveal,
  releaseRate,
  drainSeconds,
  safeCut,
  reconcileTail,
  PACING_BASE_RATE,
  PACING_BACKLOG_K,
  PACING_MAX_EXTRA_SECONDS,
} = await import("../lib/pacing.ts");

/** 假时钟 + 手动调度器：`advance(ms)` 按 16ms 一帧推进并跑掉当帧回调。 */
function makeHarness() {
  let now = 0;
  let seq = 0;
  const queue = new Map();
  const clock = {
    now: () => now,
    schedule: (cb) => {
      const h = ++seq;
      queue.set(h, cb);
      return h;
    },
    cancel: (h) => {
      queue.delete(h);
    },
    /** 推进 ms 毫秒，逐帧触发；返回实际跑的帧数。 */
    advance(ms, frameMs = 16) {
      let frames = 0;
      const until = now + ms;
      while (now < until) {
        now = Math.min(now + frameMs, until);
        const due = [...queue.entries()];
        queue.clear();
        for (const [, cb] of due) {
          frames++;
          cb();
        }
      }
      return frames;
    },
    get scheduled() {
      return queue.size;
    },
  };
  return clock;
}

/** 跑一个场景：返回显示文本随时间的记录。 */
function run({ pushes, totalMs, frameMs = 16 }) {
  const clock = makeHarness();
  const events = [];
  const seen = [];
  const p = new PacedReveal({
    onReveal: (chunk) => {
      events.push({ at: clock.now(), chunk });
      seen.push(chunk);
    },
    now: clock.now,
    schedule: clock.schedule,
    cancel: clock.cancel,
  });
  let elapsed = 0;
  for (const step of pushes) {
    if (step.at > elapsed) {
      clock.advance(step.at - elapsed, frameMs);
      elapsed = step.at;
    }
    p.push(step.text);
    if (step.end) p.end();
  }
  if (totalMs > elapsed) clock.advance(totalMs - elapsed, frameMs);
  return { p, events, seen, text: seen.join(""), clock };
}

describe("速率函数：连续、单调、无阈值（S3 的机器判据）", () => {
  test("★ q=0 恰为 BASE，且逐点等于解析式 BASE·e^(q/K)（分段实现会在这里露馅）", () => {
    assert.equal(releaseRate(0), PACING_BASE_RATE);
    for (let q = 0; q <= 3000; q += 1) {
      assert.equal(
        releaseRate(q),
        PACING_BASE_RATE * Math.exp(q / PACING_BACKLOG_K),
        `q=${q} 偏离解析式 ⇒ 速率不是"积压字符数的连续函数"`
      );
    }
  });

  test("★ 单调递增：q 每 +1，速率严格变大（不存在平台/回落档位）", () => {
    let prev = releaseRate(0);
    for (let q = 1; q <= 3000; q += 1) {
      const cur = releaseRate(q);
      assert.ok(cur > prev, `q=${q} 速率没有变大：${prev} → ${cur}`);
      prev = cur;
    }
  });

  test("★ 无跳变：相邻取值差 ≤ 千分之一相对量（阈值分档必然产生台阶）", () => {
    // 这条是"离散化后仍连续"的判据，也正是它能抓住分档的原因：
    //   - 连续函数的整数采样，相对差 ≈ 1/K = 0.00278 —— **与 q 无关**，因此必须按
    //     "差值与 q=0 处同阶"来判，写成固定 1e-3 会把正确的实现也判红；
    //   - 而任何 `if (q > N) speed = …` 的台阶都会在 N 处留下一个远大于 1/K 的跃变，
    //     下面的 `maxStep ≤ 1.001 × 基准步长` 正对着它。
    const base = (releaseRate(1) - releaseRate(0)) / releaseRate(0);
    const steps = [];
    for (let q = 0; q <= 3000; q += 1) {
      const a = releaseRate(q);
      const b = releaseRate(q + 1);
      steps.push((b - a) / a);
    }
    const maxStep = Math.max(...steps);
    assert.ok(
      maxStep <= base * 1.001,
      `最大相对跳变 ${maxStep} 超过 q=0 处基准 ${base} ⇒ 存在台阶（分档）`
    );
    // 台阶验证：把分档实现喂进同一个判据，必须被抓到（否则判据本身没有牙齿）
    const stepped = (q) => (q > 500 ? 3000 : releaseRate(q));
    const steppedMax = Math.max(
      ...Array.from({ length: 1000 }, (_, i) => {
        const a = stepped(i);
        return (stepped(i + 1) - a) / a;
      })
    );
    assert.ok(steppedMax > base * 1.001, `分档实现没被这条判据抓到（${steppedMax}）`);
  });

  test("★ 对 q 连续（非整数积压也平滑），且 u→0 时极限就是 BASE", () => {
    for (let q = 0; q <= 100; q += 0.25) {
      const a = releaseRate(q);
      const b = releaseRate(q + 0.01);
      assert.ok(b >= a, `q=${q} 处不单调`);
      assert.ok((b - a) / a < 1e-4, `q=${q} 处不连续`);
    }
    assert.ok(Math.abs(releaseRate(1e-9) - PACING_BASE_RATE) < 1e-6);
  });

  test("量级自证：积压 360 ⇒ ×e；1000 ⇒ ≈483 字/秒；2000 秒级追平", () => {
    assert.ok(Math.abs(releaseRate(360) / releaseRate(0) - Math.E) < 1e-9);
    assert.ok(Math.abs(releaseRate(1000) - 483) < 1, `实测 ${releaseRate(1000)}`);
    assert.ok(releaseRate(2000) > 7000, `实测 ${releaseRate(2000)}`);
  });
});

describe("排空时长：上界 K/BASE，与积压量无关", () => {
  test("★ T(q) 单调递增且恒小于 K/BASE —— 「只慢不快」会让压速变成新问题", () => {
    assert.ok(Math.abs(PACING_MAX_EXTRA_SECONDS - 12) < 1e-9, `上界实测 ${PACING_MAX_EXTRA_SECONDS}`);
    assert.equal(drainSeconds(0), 0, "积压为 0 时不该有任何额外时长");
    // 上界是"恒小于"，但浮点上 e^−q/K 会饱和到 0、T 恰好取到渐近值 12，
    // 所以判据用「绝不超过 12」而不是「严格小于」——后者会把正确的实现判红。
    let prev = drainSeconds(0);
    for (const q of [1, 10, 100, 300, 360, 1000, 5000, 100000]) {
      const t = drainSeconds(q);
      assert.ok(t > prev, `q=${q} 不单调：${prev} → ${t}`);
      assert.ok(t <= PACING_MAX_EXTRA_SECONDS, `q=${q} 越过上界：${t}`);
      prev = t;
    }
  });

  test("300 字一次到货 ⇒ 推算 ≈6.8s（判据 S1 要求 >2s，明显慢于真实到达）", () => {
    assert.ok(Math.abs(drainSeconds(300) - 6.8) < 0.15, `实测 ${drainSeconds(300)}`);
  });
});

describe("运行层：上游快则压速（S1）", () => {
  test("★ 一次到货 300 字：0.2s 内不会显示完，总时长 >2s，且没有任何一帧把整段砸出来", () => {
    const { events, text, p } = run({ pushes: [{ at: 0, text: "字".repeat(300), end: true }], totalMs: 20000 });
    assert.equal(text, "字".repeat(300), "内容必须一字不丢");
    assert.equal(p.backlog, 0, "最终积压必须归零");
    // 到货后 200ms 内显示了多少
    const shownAt200 = events.filter((e) => e.at <= 200).reduce((n, e) => n + e.chunk.length, 0);
    assert.ok(shownAt200 < 300, `200ms 内已显示 ${shownAt200}/300 ⇒ 没有压速`);
    // 总时长
    const lastAt = events.at(-1).at;
    assert.ok(lastAt > 2000, `总显示时长仅 ${lastAt}ms ⇒ 判据 S1 不成立`);
    assert.ok(lastAt < 12000, `总显示时长 ${lastAt}ms 超过上界（K/BASE=12s）`);
    // 无跳变：单帧新增不超过「速率上界 × 帧长」，绝不出现一帧 300 字
    for (let i = 1; i < events.length; i++) {
      const dt = (events[i].at - events[i - 1].at) / 1000;
      const cap = releaseRate(p.backlog) * dt + 1;
      assert.ok(events[i].chunk.length <= cap + 2, `第 ${i} 帧一次放了 ${events[i].chunk.length} 字`);
    }
    assert.ok(events.length > 40, `只有 ${events.length} 帧 ⇒ 不是逐帧平滑增长`);
  });

  test("★ 逐帧增长是平滑的：相邻帧显示量的比值不出现台阶（无阈值突变）", () => {
    const { events } = run({ pushes: [{ at: 0, text: "字".repeat(600), end: true }], totalMs: 20000 });
    const sizes = events.map((e) => e.chunk.length).filter((n) => n > 0);
    assert.ok(sizes.length > 30, `有效帧太少：${sizes.length}`);
    // 相邻帧增量比：单调抬升（积压越大越快），且没有一帧突然跳 10 倍
    for (let i = 1; i < sizes.length; i++) {
      const ratio = sizes[i] / sizes[i - 1];
      assert.ok(ratio < 3, `第 ${i} 帧增量跳了 ${ratio.toFixed(2)} 倍：${sizes[i - 1]} → ${sizes[i]}`);
    }
  });
});

describe("运行层：上游慢则基本实时（S2）", () => {
  test("★ 每 100ms 到 1 字、共 40 字：显示总时长与真实到货时长差 < 1s", () => {
    const pushes = [];
    for (let i = 0; i < 40; i++) pushes.push({ at: i * 100, text: "字", end: i === 39 });
    const { events, text, p } = run({ pushes, totalMs: 30000 });
    assert.equal(text, "字".repeat(40));
    assert.equal(p.backlog, 0);
    const realMs = 39 * 100; // 末字到货时刻
    const shownMs = events.at(-1).at;
    assert.ok(
      Math.abs(shownMs - realMs) < 1000,
      `显示总时长 ${shownMs}ms vs 真实 ${realMs}ms，差 ${shownMs - realMs}ms ⇒ 判据 S2 不成立`
    );
    // 队列几乎恒空：任一时刻积压不超过 2 字
    const maxBacklog = Math.max(...events.map((e) => e.chunk.length));
    assert.ok(maxBacklog <= 3, `单帧最大 ${maxBacklog} 字 ⇒ 慢上游被额外加延迟了`);
  });

  test("★ 慢上游时显示速度≈BASE，而不是被压得更慢", () => {
    const pushes = [];
    for (let i = 0; i < 30; i++) pushes.push({ at: i * 200, text: "字", end: i === 29 });
    const { events } = run({ pushes, totalMs: 30000 });
    // 每个字都应在到货后 150ms 内出现（1/30 秒 ≈ 33ms，留足帧粒度）
    for (let i = 0; i < 30; i++) {
      const shown = events[i];
      assert.ok(shown, `第 ${i} 个字始终没显示`);
      assert.ok(
        shown.at - i * 200 < 150,
        `第 ${i} 个字延迟了 ${shown.at - i * 200}ms 才显示 ⇒ 慢上游被加了延迟`
      );
    }
  });
});

describe("生命周期：停止/卸载/排空/无障碍", () => {
  test("★ R1 点停止：flush() 把已到内容一次性显示齐全，且此后不再逐帧放", () => {
    const { p, events, seen } = run({
      pushes: [{ at: 0, text: "字".repeat(500) }],
      totalMs: 2000,
    });
    const before = events.length;
    const shownBefore = seen.join("").length;
    assert.ok(p.backlog > 0, "此时应仍有积压，否则这条用例没测到东西");
    p.flush();
    assert.equal(p.backlog, 0);
    assert.equal(seen.join(""), "字".repeat(500), "停止后内容必须齐全，不能丢");
    assert.ok(events.length === before + 1, `flush 后又放了 ${events.length - before} 帧`);
    // 一次放完：最后一次回调拿到的就是全部剩余内容
    assert.equal(events.at(-1).chunk.length, 500 - shownBefore, "剩余内容必须一次性显示");
    // flush 之后即使还有迟到到货，也不许再慢慢放
    p.push("迟到的尾巴");
    assert.equal(seen.join(""), "字".repeat(500) + "迟到的尾巴");
  });

  test("★ R2 卸载 dispose()：停循环、不再回调、不留定时器", () => {
    const clock = makeHarness();
    const seen = [];
    const p = new PacedReveal({
      onReveal: (c) => seen.push(c),
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    p.push("字".repeat(400));
    clock.advance(500);
    const countAtDispose = seen.length;
    p.dispose();
    assert.equal(clock.scheduled, 0, "dispose 后仍排着帧 ⇒ 定时器泄漏");
    clock.advance(5000);
    assert.equal(seen.length, countAtDispose, "dispose 之后仍回调 onReveal ⇒ 幽灵更新");
    p.push("卸载后的迟到到货");
    clock.advance(1000);
    assert.equal(seen.length, countAtDispose, "dispose 之后 push 仍触发回调");
  });

  test("★ R3 whenDrained()：上游结束后仍要放完才兑现（refetch 必须等它）", async () => {
    const clock = makeHarness();
    const p = new PacedReveal({ onReveal: () => {}, now: clock.now, schedule: clock.schedule, cancel: clock.cancel });
    p.push("字".repeat(400));
    let drained = false;
    const wait = p.whenDrained().then(() => {
      drained = true;
    });
    clock.advance(500);
    await Promise.resolve();
    assert.equal(drained, false, "缓冲还没放完就兑现了 drained ⇒ refetch 会提前补全文本");
    assert.ok(p.backlog > 0);
    assert.equal(p.ended, false);
    p.end();
    clock.advance(20000);
    await wait;
    assert.equal(drained, true);
    assert.equal(p.backlog, 0);
  });

  test("★ flush() 早于 whenDrained() 也必须立刻兑现 —— 否则发送流程死等、卡在流式态", async () => {
    const clock = makeHarness();
    const p = new PacedReveal({ onReveal: () => {}, now: clock.now, schedule: clock.schedule, cancel: clock.cancel });
    p.push("字".repeat(300));
    clock.advance(200);
    // 错误分支的顺序就是「先 flush、后 await whenDrained」——兑现点已经过去
    p.flush();
    let drained = false;
    const wait = p.whenDrained().then(() => {
      drained = true;
    });
    await Promise.race([wait, new Promise((r) => setTimeout(r, 50))]);
    assert.equal(drained, true, "flush 之后再取 whenDrained 永远不会兑现 ⇒ 页面卡在流式态");
  });

  test("★ dispose() 也要兑现 whenDrained()，否则 await 它的调用方永久挂住", async () => {
    const clock = makeHarness();
    const p = new PacedReveal({ onReveal: () => {}, now: clock.now, schedule: clock.schedule, cancel: clock.cancel });
    p.push("字".repeat(100));
    let drained = false;
    const wait = p.whenDrained().then(() => {
      drained = true;
    });
    p.dispose();
    await wait;
    assert.equal(drained, true);
  });

  test("★ R4 prefers-reduced-motion：immediate 模式实时显示，不排队、不压速", () => {
    const clock = makeHarness();
    const seen = [];
    const p = new PacedReveal({
      onReveal: (c) => seen.push(c),
      immediate: true,
      now: clock.now,
      schedule: clock.schedule,
      cancel: clock.cancel,
    });
    p.push("开头");
    assert.equal(seen.join(""), "开头", "immediate 模式必须立刻显示");
    assert.equal(p.backlog, 0);
    assert.equal(clock.scheduled, 0, "immediate 模式不该排帧");
    p.push("结尾");
    p.end();
    assert.equal(seen.join(""), "开头结尾");
  });

  test("单循环：无论 push 多少次，同时最多只排一帧", () => {
    const clock = makeHarness();
    const p = new PacedReveal({ onReveal: () => {}, now: clock.now, schedule: clock.schedule, cancel: clock.cancel });
    for (let i = 0; i < 100; i++) p.push("字");
    assert.equal(clock.scheduled, 1, `排了 ${clock.scheduled} 帧 ⇒ 每个 delta 各起了一个定时器`);
    p.dispose();
  });

  test("零头不丢：慢速下每帧不足 1 字也能累积着放出来（否则 BASE 档一个字都放不出）", () => {
    const clock = makeHarness();
    const out = [];
    const p = new PacedReveal({ onReveal: (c) => out.push(c), now: clock.now, schedule: clock.schedule, cancel: clock.cancel });
    p.push("abcdefghij");
    clock.advance(1000); // 30 字/秒 × 1s = 30 字预算，队列只有 10 字 ⇒ 应全部放完
    assert.equal(out.join(""), "abcdefghij", `零头被丢了：实际放出 ${JSON.stringify(out.join(""))}`);
    p.dispose();
  });

  test("★ 代理对不被劈开：emoji 不能显示成半个乱码方块", () => {
    assert.equal(safeCut("a😀b", 2), 1, "切在 emoji 中间时必须退回 1 个单元");
    assert.equal(safeCut("a😀b", 3), 3);
    assert.equal(safeCut("abc", 2), 2);
    assert.equal(safeCut("abc", 99), 3);
    assert.equal(safeCut("😀", 1), 0);
    const clock = makeHarness();
    const out = [];
    const p = new PacedReveal({ onReveal: (c) => out.push(c), now: clock.now, schedule: clock.schedule, cancel: clock.cancel });
    p.push("😀".repeat(200));
    p.end();
    clock.advance(20000);
    assert.equal(out.join(""), "😀".repeat(200));
    for (const c of out) assert.ok(!/[\uD800-\uDBFF]$/.test(c), "出现了孤立的半个代理对");
  });

  test("切回前台的长暂停：单帧最多按 250ms 结算，不许把睡着的几十秒一次砸出来", () => {
    const clock = makeHarness();
    const out = [];
    const p = new PacedReveal({ onReveal: (c) => out.push(c), now: clock.now, schedule: clock.schedule, cancel: clock.cancel });
    p.push("字".repeat(5000));
    const firstFrame = (clock.advance(16), out.reduce((n, c) => n + c.length, 0));
    // 模拟标签页被挂起 60 秒：一帧的 dt 会被夹到 250ms
    clock.advance(60000, 60000);
    const after = out.reduce((n, c) => n + c.length, 0);
    const grew = after - firstFrame;
    const cap = releaseRate(5000) * 0.25 + 2;
    assert.ok(grew <= cap, `一帧放了 ${grew} 字，超过 250ms 预算 ${Math.round(cap)}`);
    p.dispose();
  });
});

describe("收尾对齐（R3 的另一半：本地已 flush 的内容不能被服务端列表抹掉）", () => {
  const ai = (content) => ({ role: "assistant", content });

  test("★ 服务端已有更完整的落库版本 ⇒ 用服务端（权威）", () => {
    const server = [{ role: "user", content: "问" }, ai("完整回复")];
    const local = ai("完整");
    assert.deepEqual(reconcileTail(server, local), server);
  });

  test("★ 服务端没有这条回复（中断/报错路径不落库）⇒ 把本地已 flush 的接回去，不许抹掉", () => {
    const server = [{ role: "user", content: "问" }];
    const local = ai("已生成的部分");
    const out = reconcileTail(server, local);
    assert.equal(out.length, 2);
    assert.equal(out[1].content, "已生成的部分");
  });

  test("★ 服务端末尾的助手消息比本地短 ⇒ 取本地（不能倒退成不完整的版本）", () => {
    const server = [{ role: "user", content: "问" }, ai("半句")];
    const local = ai("半句加上后来补完的部分");
    assert.equal(reconcileTail(server, local).at(-1).content, "半句加上后来补完的部分");
  });

  test("服务端末尾是用户消息（服务端尚未落助手）⇒ 接回本地", () => {
    const server = [{ role: "assistant", content: "开场白" }, { role: "user", content: "问" }];
    const out = reconcileTail(server, ai("回复"));
    assert.equal(out.at(-1).role, "assistant");
    assert.equal(out.at(-1).content, "回复");
  });

  test("本地无内容 ⇒ 原样返回服务端列表（没生成任何东西时别插空气泡）", () => {
    const server = [{ role: "user", content: "问" }];
    assert.deepEqual(reconcileTail(server, ai("")), server);
    assert.deepEqual(reconcileTail(server, null), server);
  });

  test("纯函数：不修改入参", () => {
    const server = [{ role: "user", content: "问" }];
    const snapshot = JSON.parse(JSON.stringify(server));
    reconcileTail(server, ai("回复"));
    assert.deepEqual(server, snapshot);
  });
});
