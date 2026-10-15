# Chatbot Platform web — Next.js 15（standalone 输出，多阶段构建）
#
# 为什么用 standalone：`next start` 需要整个 node_modules（数百 MB），而 standalone
# 只打包运行时真正 import 到的文件，镜像体积与冷启动都明显更小。代价是必须显式拷贝
# `.next/static` 与 `public/`（standalone 的 server.js 不包含它们），下面已处理。
#
# ⚠️ BACKEND_URL 是**运行时**读取的（next.config.mjs 的 rewrites 在服务启动时求值，
# 不是构建期），所以它可以由 compose 的 environment 注入，无需 --build-arg 重烘焙。
# 这一点很重要：同一份镜像可以指向不同的后端。

# ---------- 依赖层 ----------
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---------- 构建层 ----------
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Next 15 默认就是 standalone，这里显式写出以防将来默认值变化
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---------- 运行层 ----------
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# 不用 root 跑：容器被攻破时降低影响面
RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001

# standalone 的 server.js 需要 static 与 public 才完整。
# ⚠️ `public/` 在本仓库**不存在**（静态资源都在 app/ 下的路由里），所以若直接
# `COPY /app/public` 会让 docker build 失败。下面用通配 `public*` + 结尾 `/`
# 让它在缺失时退化为「拷一个空目录」而不是报错。
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public* ./public/

USER nextjs
EXPOSE 3000

# 探活：Next 没有内置健康端点，用根路径即可（200 即应用已就绪）
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
