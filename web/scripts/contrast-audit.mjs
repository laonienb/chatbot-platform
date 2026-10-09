#!/usr/bin/env node
/**
 * WCAG 文字对比度审计（常驻工具，不是一次性脚本）。
 *
 * 零依赖：直接驱动系统 Chrome/Edge 的 DevTools 协议，不需要 npm install 任何包。
 *
 * 用法（前端 dev server 需在跑）：
 *   node scripts/contrast-audit.mjs
 *   node scripts/contrast-audit.mjs --pages /chat,/market --themes light
 *   node scripts/contrast-audit.mjs --email 别的号 --password ...   # 换登录身份
 *   node scripts/contrast-audit.mjs --email none                    # 游客态
 *   node scripts/contrast-audit.mjs --no-hover      # 只测静止态（快）
 *   node scripts/contrast-audit.mjs --json          # 机器可读
 * 退出码：0 全部达标；1 发现不达标项；2 工具自身跑不起来（浏览器缺失、服务未起等）。
 *
 * 判读口径（决定"什么算失败"，都会影响结论，故写明）：
 * - 正文 4.5:1；大字号（≥24px，或 ≥18.66px 且 bold≥700）3:1 —— WCAG AA。
 * - 底色按真实绘制顺序自下而上合成：祖先背景 → 本元素 background-color →
 *   background-image 各层（CSS 里第一层画在最上面）。**渐变不整体排除**：每个色标
 *   各成一个候选像素，取与文字对比最差的那个 —— 上一版把渐变一律排除，会漏掉
 *   压在渐变浅端上的真失败；也把"祖先纯色底"当成候选，会拿白字比白页报出 1.01 这种假值。
 * - group opacity 计入：每层的有效不透明度乘以该层宿主及其所有祖先的 opacity 连乘。
 * - CSS filter 计入：沿祖先链把 brightness()/contrast() 的系数连乘，同时作用于
 *   「字+底」合成后的像素（通道截断在 255）——这正是 hover 提亮会把白字底比值拉低的原因。
 * - **hover 态也测**（默认开）：用 CDP CSS.forcePseudoState 强制 :hover，再逐元素复测。
 *   受影响的样式挂在 class 上，故同一 class 签名只测首个实例；测前注入
 *   `transition:none` —— 否则 getComputedStyle 会取到过渡中间的插值色。
 *   这条不是可选装饰：静态态达标、悬停被通用 button:hover 换成半透明底，是两类真缺陷。
 * - 排除三类并单独计数，不当作通过：
 *     transparent-text  渐变裁切文字（color 透明，solid 数学测不了实际像素）
 *     graphic-only      纯 emoji/符号（文本里没有字母数字，不受 1.4.3 约束）
 *     inactive-ui       :disabled 控件内的文字（WCAG 1.4.3 对非活动界面组件豁免）
 * - 已知近似：伪元素（body::before 的柔光层）拿不到，故不计入其叠加效果；text-shadow、
 *   backdrop-filter、跨列背景图同样未建模；invert/sepia/grayscale 一类矩阵滤镜未建模。
 *   另：只测 :hover，不测 :focus-visible / :active（后两者一般只改描边与位移）。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const DEFAULTS = {
  base: "http://127.0.0.1:3000",
  pages: ["/chat", "/market", "/keys", "/models", "/admin", "/login"],
  themes: ["light", "dark"],
  settle: 1200,
  // 默认用本地验证测试号（2026-10-09 前端会话自注册，只读页面，不发消息）。
  // 不带凭据时受保护页面会统一跳 /login，六页测成同一页等于没测。
  email: "ui-probe@local.dev",
  password: "uiprobe123",
  json: false,
  hover: true,
};

function parseArgs(argv) {
  const opts = { ...DEFAULTS };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    if (a === "--base") opts.base = next();
    else if (a === "--pages") opts.pages = next().split(",").map((p) => (p.startsWith("/") ? p : `/${p}`));
    else if (a === "--themes") opts.themes = next().split(",");
    else if (a === "--settle") opts.settle = Number(next());
    else if (a === "--email") opts.email = next();
    else if (a === "--password") opts.password = next();
    else if (a === "--json") opts.json = true;
    else if (a === "--no-hover") opts.hover = false;
    else if (a === "--help" || a === "-h") {
      console.log(
        [
          "用法: node scripts/contrast-audit.mjs [--base URL] [--pages a,b] [--themes light,dark]",
          "         [--email 账号 --password 密码] [--settle 毫秒] [--no-hover] [--json]",
          "默认审计 6 页 × 深浅两主题的静止态与 hover 态，用本地测试号登录态。",
        ].join("\n")
      );
      process.exit(0);
    } else {
      console.error(`未知参数: ${a}`);
      process.exit(2);
    }
  }
  return opts;
}

// ---------- 浏览器定位 ----------

const BROWSER_CANDIDATES = {
  win32: [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`,
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  ],
  darwin: ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
  linux: ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"],
};

function findBrowser() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  return (BROWSER_CANDIDATES[process.platform] ?? BROWSER_CANDIDATES.linux).find(existsSync);
}

function launchBrowser(bin) {
  const profile = mkdtempSync(join(tmpdir(), "contrast-audit-"));
  const child = spawn(
    bin,
    [
      "--headless=new",
      "--remote-debugging-port=0",
      "--remote-allow-origins=*",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--hide-scrollbars",
      `--user-data-dir=${profile}`,
      "about:blank",
    ],
    { stdio: ["ignore", "pipe", "pipe"] }
  );
  const endpoint = new Promise((resolve, reject) => {
    let buf = "";
    const onData = (chunk) => {
      buf += chunk.toString("utf8");
      const m = buf.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (m) {
        clearTimeout(timer);
        resolve(m[1]);
      }
    };
    child.stderr.on("data", onData);
    child.stdout.on("data", onData);
    child.on("exit", (code) => reject(new Error(`浏览器退出 (code ${code})，未打印 DevTools 端点`)));
    const timer = setTimeout(() => reject(new Error("10s 内未拿到 DevTools 端点")), 10_000);
  });
  return { child, endpoint, profile };
}

/** 极简 CDP 会话：只用 send 与 once。 */
class Session {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener("message", (ev) => this._onMessage(ev.data));
  }
  _onMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(typeof raw === "string" ? raw : String(raw));
    } catch {
      return;
    }
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      msg.error ? reject(new Error(`${msg.error.message} (code ${msg.error.code})`)) : resolve(msg.result);
    } else if (msg.method && this.handlers.has(msg.method)) {
      this.handlers.get(msg.method)(msg.params);
    }
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  once(event, timeoutMs = 20_000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.handlers.delete(event);
        reject(new Error(`等待 ${event} 超时`));
      }, timeoutMs);
      this.handlers.set(event, (params) => {
        clearTimeout(timer);
        this.handlers.delete(event);
        resolve(params);
      });
    });
  }
}

async function openPageSession(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error(`CDP WebSocket 连接失败: ${wsUrl}`)), { once: true });
  });
  return new Session(ws);
}

async function newPageTarget(httpBase) {
  const res = await fetch(`${httpBase}/json/new?url=about:blank`, { method: "PUT" });
  if (!res.ok) throw new Error(`创建页面目标失败: HTTP ${res.status}`);
  return res.json();
}

// ---------- 页内探针 ----------

const HARNESS = () => {
  const MAX_CANDIDATES = 48;

  const parseColor = (c) => {
    const m = c.match(/-?[\d.]+/g);
    const p = m ? m.map(Number) : [];
    return [p[0] ?? 0, p[1] ?? 0, p[2] ?? 0, p[3] === undefined ? 1 : p[3]];
  };
  const clamp255 = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);
  const over = (src, dst) => {
    const a = src[3];
    return [
      clamp255(src[0] * a + dst[0] * (1 - a)),
      clamp255(src[1] * a + dst[1] * (1 - a)),
      clamp255(src[2] * a + dst[2] * (1 - a)),
      1,
    ];
  };
  const hexToRgba = (h) => {
    const s = h.slice(1);
    const f = s.length <= 4 ? [...s].map((ch) => ch + ch) : [s.slice(0, 2), s.slice(2, 4), s.slice(4, 6), s.slice(6, 8) || "ff"];
    return [parseInt(f[0], 16), parseInt(f[1], 16), parseInt(f[2], 16), parseInt(f[3], 16) / 255];
  };
  // background-image 是多层列表，CSS 顺序里第一层画在最上面；按括号深度切顶层逗号
  const splitLayers = (value) => {
    const parts = [];
    let depth = 0;
    let cur = "";
    for (const ch of value) {
      if (ch === "(") depth++;
      else if (ch === ")") depth--;
      if (ch === "," && depth === 0) {
        parts.push(cur);
        cur = "";
      } else cur += ch;
    }
    parts.push(cur);
    return parts;
  };
  // 该元素可见的绘制层，自下而上：background-color 最底，然后末张图 …… 首张图
  const layersOf = (cs) => {
    const out = [];
    const bgc = parseColor(cs.backgroundColor);
    if (bgc[3] > 0) out.push([bgc]);
    for (const layer of splitLayers(cs.backgroundImage).reverse()) {
      if (/^\s*none/.test(layer)) continue;
      const stops = [];
      const re = /rgba?\([^)]+\)|#[0-9a-fA-F]{3,8}\b/g;
      let m;
      while ((m = re.exec(layer))) {
        const c = m[0][0] === "#" ? hexToRgba(m[0]) : parseColor(m[0]);
        if (c[3] > 0) stops.push(c);
      }
      // url() 之类的装饰图无可采色标 → 跳过（不当作底色）
      if (stops.length) out.push(stops);
    }
    return out;
  };
  const lin = (v) => Math.pow((v / 255 + 0.055) / 1.055, 2.4);
  const luminance = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
  const contrast = (fg, bg) => {
    const a = luminance(fg);
    const b = luminance(bg);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const describe = (el) => {
    const c = el.getAttribute("class");
    return c === null ? el.tagName : `${el.tagName}.${c.split(" ")[0]}`;
  };
  // CSS filter 作用在「该元素渲染出来的整组像素」上（字和底一起缩放），所以必须同时
  // 乘到前景与底色上；通道截断在 255 意味着提亮只抬底色不抬白字 —— 比值随之下降。
  const filterFactor = (nodes) => {
    let k = 1;
    for (const n of nodes) {
      const f = getComputedStyle(n).filter;
      if (!f || f === "none") continue;
      const re = /(brightness|contrast)\(([-\d.eE+]+)\)/g;
      let m;
      while ((m = re.exec(f))) {
        const v = Number(m[2]);
        if (Number.isFinite(v)) k *= v;
      }
    }
    return k;
  };
  const scaleCh = (c, k) => (k === 1 ? c : [clamp255(c[0] * k), clamp255(c[1] * k), clamp255(c[2] * k), c[3]]);
  // 只取元素自身的直接文本，避免同一句话在祖先链上重复计一次
  const ownText = (el) => {
    let t = "";
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
    return t.trim();
  };
  const chainOf = (el) => {
    const nodes = [];
    for (let n = el; n !== null; n = n.parentElement) nodes.push(n);
    return nodes; // [el, parent, ..., html]
  };
  // 第 i 个节点（从内数）的绘制结果要再被它自己及更外的 group opacity 压向底：连乘
  const opacityProducts = (nodes) => {
    const acc = [];
    let p = 1;
    for (const n of nodes) {
      const o = Number(getComputedStyle(n).opacity);
      p *= Number.isFinite(o) ? (o < 0 ? 0 : o > 1 ? 1 : o) : 1;
      acc.push(p);
    }
    return acc;
  };

  // 文字背后所有可能的像素：每个渐变层的色标各展开一个"世界"，最差者决定判读
  const backgroundWorlds = (nodes, opAcc) => {
    let worlds = [[255, 255, 255, 1]];
    for (let i = nodes.length - 1; i >= 0; i--) {
      const layers = layersOf(getComputedStyle(nodes[i]));
      if (!layers.length) continue;
      const factor = opAcc[i];
      const next = [];
      for (const base of worlds) {
        // 本元素内多层色标的组合（多层渐变少见，数量级很小）
        let combos = [[]];
        let truncated = false;
        for (const stops of layers) {
          const expanded = [];
          for (const c of combos) for (const s of stops) expanded.push([...c, s]);
          if (expanded.length * worlds.length > MAX_CANDIDATES) {
            truncated = true;
            break;
          }
          combos = expanded;
        }
        if (truncated) combos = [layers.map((s) => s[0])]; // 组合爆炸时退化为每层取首色标
        for (const choice of combos) {
          let px = base;
          for (const c of choice) px = over([c[0], c[1], c[2], c[3] * factor], px);
          next.push(px);
        }
      }
      worlds = next.slice(0, MAX_CANDIDATES);
    }
    return worlds;
  };

  const measureEl = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return null;
    const text = ownText(el);
    if (!text) return null;

    const color = parseColor(cs.color);
    if (color[3] === 0) return { exempt: "transparent-text" };
    if (!/[\p{L}\p{N}]/u.test(text)) return { exempt: "graphic-only" };
    if (el.closest(':disabled, [aria-disabled="true"]')) return { exempt: "inactive-ui" };

    const nodes = chainOf(el);
    const opAcc = opacityProducts(nodes);
    if (opAcc[0] === 0) return null; // 整组不可见

    const fs = parseFloat(cs.fontSize);
    const fw = Number(cs.fontWeight);
    const need = fs >= 24 || (fw >= 700 && fs >= 18.66) ? 3 : 4.5;
    const k = filterFactor(nodes);
    const fg = [color[0], color[1], color[2], color[3] * opAcc[0]];

    let worst = Infinity;
    let worstBg = null;
    for (const raw of backgroundWorlds(nodes, opAcc)) {
      const bg = scaleCh(raw, k);
      const r = contrast(scaleCh(over(fg, raw), k), bg);
      if (r < worst) {
        worst = r;
        worstBg = bg;
      }
    }
    if (worst >= need) return { ok: true };
    return {
      sel: describe(el),
      text: text.slice(0, 24),
      ratio: Number(worst.toFixed(2)),
      need,
      px: Math.round(fs * 10) / 10,
      weight: fw,
      // 附上真实取色，否则"为什么不达标"没法查、也没法验证工具自己算错没有
      fg: `rgb(${color.slice(0, 3).map(Math.round).join(" ")})`,
      bg: `rgb(${worstBg.slice(0, 3).map(Math.round).join(" ")})`,
    };
  };

  const all = () => {
    const fails = [];
    const excluded = { "transparent-text": 0, "graphic-only": 0, "inactive-ui": 0 };
    const seen = new Set();
    let checked = 0;

    for (const el of document.querySelectorAll("body *")) {
      const r = measureEl(el);
      if (!r) continue;
      if (r.exempt) {
        excluded[r.exempt]++;
        continue;
      }
      if (r.ok) {
        checked++;
        continue;
      }
      checked++;
      const key = `${r.sel}|${r.text.slice(0, 12)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      fails.push(r);
    }

    fails.sort((a, b) => a.ratio - b.ratio);
    return {
      theme: document.documentElement.dataset.theme,
      path: location.pathname,
      checked,
      failTotal: fails.length,
      fails: fails.slice(0, 12),
      excluded,
    };
  };

  return { measureEl, all };
};

// ---------- 主流程 ----------

async function login(base, email, password) {
  const res = await fetch(`${base}/api/v1/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${await res.text()}`);
  return res.json();
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function goto(s, url) {
  const loaded = s.once("Page.loadEventFired");
  await s.send("Page.navigate", { url });
  await loaded;
}

const HOVER_SELECTOR = 'button, a[href], [role="button"], .chip';

/** 装上页内探针，并冻结过渡/动画：forcePseudoState 是瞬时切换，留着 transition
 *  getComputedStyle 会读到插值中的中间色，测出来的是"半个 hover 态"。 */
async function installProbe(s) {
  await s.send("Runtime.evaluate", { expression: `window.__cp = (${HARNESS.toString()})()` });
  await s.send("Runtime.evaluate", {
    expression: `(() => {
      const st = document.createElement("style");
      st.textContent = "*,*::before,*::after{transition:none!important;animation:none!important}";
      document.head.appendChild(st);
      return 1;
    })()`,
  });
}

/** 逐元素强制 :hover 复测。同一 class 签名只测首个可见实例 —— hover 规则挂在 class 上，
 *  逐实例重复测不出新东西，却会把一轮审计拖成几百次往返。 */
async function hoverPass(s) {
  const { root } = await s.send("DOM.getDocument", { depth: -1 });
  const { nodeIds } = await s.send("DOM.querySelectorAll", { nodeId: root.nodeId, selector: HOVER_SELECTOR });
  const seen = new Set();
  const fails = [];
  let probed = 0;

  for (const nodeId of nodeIds) {
    let object;
    try {
      ({ object } = await s.send("DOM.resolveNode", { nodeId }));
    } catch {
      continue; // 客户端渲染可能已经把该节点换掉
    }
    const sig = await s.send("Runtime.callFunctionOn", {
      functionDeclaration:
        "function(){return this.tagName+'.'+((this.getAttribute('class')||'').split(/\\s+/).filter(Boolean).sort().join('.'))}",
      objectId: object.objectId,
      returnByValue: true,
    });
    const key = sig.result.value;
    if (seen.has(key)) continue;

    await s.send("CSS.forcePseudoState", { nodeId, forcedPseudoClasses: ["hover"] });
    let r = null;
    try {
      const m = await s.send("Runtime.callFunctionOn", {
        functionDeclaration: "function(){return window.__cp.measureEl(this)}",
        objectId: object.objectId,
        returnByValue: true,
      });
      r = m.result.value;
    } finally {
      await s.send("CSS.forcePseudoState", { nodeId, forcedPseudoClasses: [] });
    }
    if (!r) continue; // 不可见/无自持文字：同签名的别的实例可能可见，不记签名
    seen.add(key);
    probed++;
    if (!r.ok && !r.exempt) fails.push({ ...r, state: "hover", key });
  }
  return { probed, fails };
}

async function gotoAndProbe(s, url, settle, withHover) {
  const loaded = s.once("Page.loadEventFired");
  await s.send("Page.navigate", { url });
  await loaded;
  await sleep(settle); // 客户端取数之后才定稿的文字也要覆盖
  await installProbe(s);
  const res = await s.send("Runtime.evaluate", {
    expression: `window.__cp.all()`,
    returnByValue: true,
  });
  if (res.exceptionDetails) {
    throw new Error(`探针在 ${url} 抛错：${res.exceptionDetails.exception?.description ?? "未知异常"}`);
  }
  const out = res.result.value;
  if (withHover) {
    const h = await hoverPass(s);
    out.hover = h;
  }
  return out;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));

  const bin = findBrowser();
  if (!bin) {
    console.error("找不到 Chrome/Edge。设 CHROME_PATH 指向可执行文件后重试。");
    process.exit(2);
  }

  let tokens = null;
  if (opts.email && opts.password && opts.email !== "none") {
    try {
      tokens = await login(opts.base, opts.email, opts.password);
    } catch (e) {
      console.error(`以 ${opts.email} 登录失败（${e.message}），改以游客态审计 —— 受保护页面会落到 /login。`);
    }
  } else {
    console.error("游客态：未注入 token，受登录保护的页面会落到 /login。");
  }

  const { child, endpoint, profile } = launchBrowser(bin);
  let browserWs;
  try {
    browserWs = await endpoint;
  } catch (e) {
    console.error(`浏览器启动失败：${e.message}`);
    child.kill();
    process.exit(2);
  }
  // URL.origin 对 ws: 会原样保留 ws scheme，而 fetch 只认 http，故按端口自己拼
  const httpBase = `http://127.0.0.1:${new URL(browserWs).port}`;

  const results = [];
  try {
    const target = await newPageTarget(httpBase);
    const s = await openPageSession(target.webSocketDebuggerUrl);
    await s.send("Page.enable");
    await s.send("Runtime.enable");
    if (opts.hover) {
      await s.send("DOM.enable");
      await s.send("CSS.enable");
    }

    await goto(s, `${opts.base}/login`); // 先落到同源，才能写该源的 localStorage
    await s.send("Runtime.evaluate", {
      expression: `(() => {
        const t = ${JSON.stringify(tokens)};
        if (t) {
          localStorage.setItem("cp_access_token", t.access_token);
          localStorage.setItem("cp_refresh_token", t.refresh_token);
        } else {
          localStorage.removeItem("cp_access_token");
          localStorage.removeItem("cp_refresh_token");
        }
        return 1;
      })()`,
      returnByValue: true,
    });

    for (const theme of opts.themes) {
      for (const page of opts.pages) {
        await s.send("Runtime.evaluate", {
          expression: `localStorage.setItem("cp_theme", ${JSON.stringify(theme)})`,
        });
        const r = await gotoAndProbe(s, `${opts.base}${page}`, opts.settle, opts.hover);
        r.theme = theme; // 首屏引导脚本可能尚未应用，以驱动意图为准
        results.push(r);
        if (!opts.json) printRow(r);
      }
    }
    s.ws.close();
  } finally {
    child.kill();
    try {
      rmSync(profile, { recursive: true, force: true, maxRetries: 3 });
    } catch {
      /* Windows 上偶发文件占用，临时目录交给系统回收 */
    }
  }

  const sum = (r) => (r.failTotal ?? 0) + (r.hover?.fails?.length ?? 0);
  const failTotal = results.reduce((n, r) => n + sum(r), 0);
  if (opts.json) {
    console.log(JSON.stringify({ base: opts.base, failTotal, results }, null, 2));
  } else {
    console.log(`\n${results.length} 格（页面 × 主题），不达标 ${failTotal} 处。`);
    if (failTotal === 0) console.log("全部达标（WCAG AA：正文 4.5:1 / 大字 3:1）。");
    else console.log("修法优先动令牌或本组件作用域，别散着补 13 个选择器。");
  }
  process.exit(failTotal > 0 ? 1 : 0);
}

function printRow(r) {
  const ex = Object.entries(r.excluded ?? {})
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${k}×${n}`)
    .join(", ");
  const hover = r.hover;
  const head = `${r.theme.padEnd(5)} ${r.path.padEnd(8)} 已测 ${String(r.checked).padStart(3)} · 不达标 ${r.failTotal}${ex ? `  （豁免：${ex}）` : ""}`;
  console.log(hover ? `${head}｜hover 复测 ${hover.probed} 类 · 不达标 ${hover.fails.length}` : head);
  for (const f of r.fails) {
    console.log(`   ${String(f.ratio).padStart(5)} < ${f.need}  ${f.sel}  ${f.px}px/${f.weight}  字色 ${f.fg} 底 ${f.bg}  「${f.text}」`);
  }
  for (const f of hover?.fails ?? []) {
    console.log(`   ${String(f.ratio).padStart(5)} < ${f.need}  ${f.sel}  ${f.px}px/${f.weight}  字色 ${f.fg} 底 ${f.bg}  「${f.text}」  ← hover 态`);
  }
}

main().catch((e) => {
  console.error(`审计工具异常终止：${e.message}`);
  process.exit(2);
});
