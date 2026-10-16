# 云服务器部署规划（国内节点 · 不备案 · 高位端口）

> 本文是**规划**，不是操作手册。回答「买什么、买多大、买哪家、买完怎么装」四个问题。
> 现有 `deploy/README.md` 是本机/穿透向导（Windows + PowerShell），本文是**云服务器**一路，
> 两者不要混用。落地命令清单见本文第六节，逐条可复制。
>
> 结论都标了**验证程度**——哪些是本仓库实测、哪些是行业通识、哪些要你自己在现场确认。
> 不做「看起来很像实测」的表述。

---

## 零、先给结论

| 项目 | 建议 | 依据 |
|---|---|---|
| 节点位置 | **中国大陆**（北京/上海/广州/成都任选） | 你选了「国内用户 + 不备案 + 非标端口」 |
| 配置 | **2 核 2G，40G 系统盘**（内存 2G 是硬下限，见 §二） | 四容器 + 构建峰值，实测见 §二 |
| 带宽 | **3 Mbps 起步**，够 10 人以内；人多再加 | 页面首屏 ~1MB，见 §三 |
| 计费 | **包月/包年**，不要按量 | 长期跑，按量会被流量费咬 |
| 系统 | **Ubuntu 22.04 或 24.04 LTS** | 见 §四.1 |
| 备案 | **不备**，用 3000 等高位端口 | 你的选择；代价见 §一.3 |
| 价格 | 2核2G 约 **¥60–120/月**（新用户首年常见 ¥99–199） | ⚠️ 价格随时变，见 §五 |

**一句话**：这台机器不跑 AI 模型（模型在小米云上），它只是个**转发壳**。
所以**不需要 GPU、不需要大内存、不需要高配**——这是本方案省钱的根本原因。

---

## 一、架构：为什么这台服务器可以很便宜

### 1.1 真实的请求链路

```
用户浏览器 ──https/http──> 你的云服务器:3000
                                │  (web 容器：Next.js，同时是反向代理)
                                └──> server 容器:8000 (FastAPI，仅内网)
                                        ├──> postgres (仅内网)
                                        ├──> redis    (仅内网)
                                        └──> model-service (仅内网)
                                                │
                                                └──http──> api.xiaomimimo.com  ← 真正的模型在这里
```

**关键点：模型的算力不在你的服务器上。** `deploy/.env` 实测值：

```
LLM_BACKEND=remote                      # 平台不直连模型，走 model-service
MS_UPSTREAM=litellm                     # model-service 走真实上游
MS_LITELLM_API_BASE=https://api.xiaomimimo.com/v1
MS_UPSTREAM_VENDOR=xiaomi-mimo
```

所以这台云服务器的工作是：**收 HTTP、查库、转发、把流式响应原样吐回去**。
全是 I/O 等待，几乎不吃 CPU。这是「2 核 2G 就够」的唯一原因，也是最容易被误解的地方。

> ⚠️ 反过来说：**如果你以后改成自己部署开源模型**（vLLM/Ollama 之类），本文立刻作废——
> 那种场景要 GPU，月租从三位数跳到四位数。本文只覆盖当前这版架构。

### 1.2 上游是境内端点，这是好消息

`api.xiaomimimo.com` 是小米的**境内**服务。大陆机器调大陆 API：
延迟低、不会跨境抖动、不需要额外代理。**大陆节点这个选择因此没有副作用。**

> 如果哪天你换成 OpenAI / Anthropic 这类境外上游，大陆机器会连不上或极不稳定，
> 那时要么换香港节点、要么给 model-service 配代理。**换上游前先想这件事。**

### 1.3 不备案的代价（必须知道的硬事实）

大陆机房对域名有备案要求，**但对高位端口没有**。所以：

| 做法 | 结果 |
|---|---|
| 域名 + 80/443 | ❌ **必须备案**，否则运营商拦截，页面打不开 |
| IP + 高位端口（如 `:3000`） | ✅ **可直接用**，无需备案 |
| 域名 + 高位端口 | ⚠️ 灰区，不同机房宽严不一，别依赖 |

**代价**：用户要输 `http://1.2.3.4:3000` 这种地址，不好看、记不住、且**没有 HTTPS**。
- 没有 HTTPS ⇒ 浏览器地址栏显示「不安全」；登录密码在链路上是明文的。
- 想解决只有两条路：① 备案 + 域名 + 免费证书；② 上 CDN/反代（通常也要备案）。

**我的建议**：先按 IP + 端口跑起来，验证有人用；**确认要长期对外开放后，再补备案**。
备案免费，但需要实名 + 域名 + 3–20 天审核，且有「网站名称/内容」审核，
别在还没人用的时候就投进去。

> ⚠️ 安全提醒：**没有 HTTPS 就意味着登录口令裸奔**。如果你要拉陌生人进来用，
> 这件事应当在开放前解决（备案+证书，或先只发给熟人）。

---

## 二、选多大：用实测数据说话

### 2.1 内存：2G 是底线，别买 1G

四个容器同时驻留。本仓库**实测的本地构建产物**（用于估算，非镜像体积本身）：

| 目录 | 大小 | 说明 |
|---|---|---|
| `web/node_modules` | 382 MB | 构建期需要；运行时 standalone 产物小得多 |
| `web/.next` | 273 MB | 构建产物 |
| `server/.venv` | 296 MB | 含 litellm（依赖很重） |

运行时靠 `web/Dockerfile` 的多阶段 + `output: "standalone"` 把前端砍到只带真正
import 到的文件，所以**镜像不至于吓人**，但：

- **Next.js 的 `next build` 是内存大户**。2G 机器上构建前端有 OOM 风险，
  这是本方案最需要现场验证的一步（见 §六.3 与 §七.1）。
- `litellm` 装入 server 镜像也是一次重活（编译期吃内存与磁盘）。

**结论**：
- **2 核 2G**：能跑，但**构建时可能需要加 swap**（见 §六.2）。
- **2 核 4G**：明显更舒服，构建不用提心吊胆。**预算允许建议直接上 4G。**
- **1 核 1G**：❌ 不建议。构建大概率失败，运行也紧。

### 2.2 CPU：2 核足够

请求路径全程是 I/O 等待（等数据库、等上游 token）。2 核能扛住构建 + 日常。
**1 核会在构建时非常慢**（npm ci + next build + pip install litellm 串起来可能 20+ 分钟），
不推荐。

### 2.3 磁盘：40G 起步

- 系统 + Docker 引擎：约 5–8 G
- 四个镜像（含 node 22 与 python 3.12 基础镜像、litellm）：预计 3–6 G
- 构建缓存（BuildKit + npm/pip 缓存）：**这是隐藏大头，几个 G 起步**
- Postgres 数据：初期极小

**40G 够用**；20G 会因为构建缓存而紧张；**60G 更稳妥**，差价通常很小。

### 2.4 带宽：3 Mbps 起步

- 页面首屏（HTML+JS+CSS 压缩后）约 **1 MB 量级**。
- 3 Mbps ≈ 375 KB/s ⇒ 首屏约 3 秒。**够几个人同时用。**
- 聊天是 SSE 长连接，**每连接占用极小带宽但占用长连接**。
- 大陆轻量服务器通常按**峰值带宽**计费，不是按流量，所以长连接不额外烧钱。

**建议**：3 Mbps 起，观察实际使用再加。⚠️ 若选按流量计费的机型，
要小心 SSE 长连接 + 多人刷新的流量累积。

### 2.5 一张配置表

| 配置 | 适合 | 评价 |
|---|---|---|
| 1核1G / 20G | — | ❌ 别买，构建会挂 |
| **2核2G / 40G / 3M** | 自己 + 几个熟人 | ✅ **最低可用**，构建时加 swap |
| **2核4G / 60G / 3-5M** | 小范围公开 | ✅ **推荐**，省心 |
| 4核8G / 80G / 5M+ | 几十人日常 | 有余量；本架构不太需要 |

---

## 三、买哪家

⚠️ **验证程度**：以下为行业通识与常见形态，**不是本仓库实测**，且**价格我没有在本会话核实**
（联网检索在本会话不可用）。**下单前请自己去官网核对当前价格与条款。**

常见选择（境内、免备案用高位端口都可行）：

| 厂商 | 产品 | 特点 |
|---|---|---|
| 阿里云 | 轻量应用服务器 | 生态最全，控制台好用；新用户首年常有大幅优惠 |
| 腾讯云 | 轻量应用服务器 | 与阿里云相近，活动价常更低 |
| 华为云 | 云耀/弹性云服务器 | 常见，稳定性好 |
| 京东云 / UCloud 等 | 轻量型 | 活动价可能更低 |

**选择要点（比选哪家更重要）**：

1. **认准「轻量应用服务器」**，不要买 ECS/CVM 标准型——后者更贵且你不需要弹性能力。
2. **计费方式选包月/包年**。按量计费（尤其流量计费）在 SSE 场景不可控。
3. **系统镜像选 Ubuntu 22.04/24.04 LTS**，别选 CentOS（已停更）或 Windows（贵且无用）。
4. **确认是「中国大陆」地域**。香港/新加坡免备案，但你要国内速度 + 上游在境内，无必要。
5. **看清「峰值带宽 vs 流量包」**：轻量型多为固定带宽 + 月流量包，确认流量包够用。
6. **新用户优惠注意续费价**：首年 ¥99 的机器次年可能恢复 ¥600+，**下单前看续费价**。

> 💡 省钱建议：**先买 1 个月试**，把本文第六节跑通、确认能构建、确认上游可达，
> 再续年付。避免买了年付才发现构建 OOM 或上游不通。

---

## 四、装机

### 4.1 系统与登录

- 镜像：**Ubuntu 24.04 LTS**（或 22.04 LTS，皆可）。
- 首选 **SSH 密钥登录**；若只能密码，**立刻改成密钥并禁用密码登录**（§四.3）。
- 记住公网 IP。

### 4.2 安全组 / 防火墙：只开三个口

云厂商控制台的「安全组」（或防火墙）是**第一道门**，必须先设对：

| 端口 | 用途 | 是否必须 |
|---|---|---|
| 22 | SSH 管理 | ✅ 必须（**建议限制来源 IP 为你自己的 IP**） |
| 3000 | 网站入口（高位端口，免备案） | ✅ 必须 |
| 443 / 80 | 暂不用（未备案） | ❌ 先别开 |

**其余端口一律不开。** 特别强调 `5432`(Postgres)、`6379`(Redis)、`8000`(后端 API)：
`deploy/docker-compose.prod.yml` 已刻意**不为它们发布宿主端口**（只有 `web` 有 `ports:`，
其余仅内网可达）。这是生产文件与本地开发文件的关键差异，**不要为了调试去加端口映射**。

### 4.3 装 Docker

```bash
# 官方一键脚本（Ubuntu）
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 让当前用户免 sudo 用 docker
# 重新登录使组生效，然后验证：
docker version && docker compose version
```

> Ubuntu 上用 **Docker Engine**（上面的脚本）即可，**不需要 Docker Desktop**。
> 本机 Windows 那套装 Docker Desktop 的步骤在这里不适用。

### 4.4 收尾加固（建议）

```bash
sudo ufw allow 22/tcp && sudo ufw allow 3000/tcp && sudo ufw enable
sudo apt update && sudo apt upgrade -y
```

> ⚠️ `ufw enable` 前**务必先放行 22**，否则 SSH 会把自己关在门外。
> 另外云厂商安全组与 ufw 是两层，都要放行 3000。

---

## 五、把项目搬上去

> 🚨 **先读 §五.0：有 4 个必须先修的阻塞项。** 直接照 §五.1 往下做会在构建或首次聊天时失败。

### 5.0 部署前必须先修的阻塞项（独立复核确认）

> ✅ **B1 与 B4 已于 2026-10-16 修复**（`model-service/Dockerfile`、`server/Dockerfile`）。
> 下表保留完整记录与验证方式；B2/B3 仍属配置层，部署时按改法执行即可。

以下 4 项都有 `file:line` 证据，**不修则部署必失败或必不可用**。

| # | 症状 | 根因 | 改法 |
|---|---|---|---|
| **B1** | 能起容器，**每次聊天必挂** | `model-service/Dockerfile:5` 是 `pip install --no-cache-dir .`，**没装 `[real]` extra**；`litellm` 在 `model-service/pyproject.toml:14` 的 optional 里。而 `docker-compose.prod.yml:74` 注入 `MS_UPSTREAM=litellm`（覆盖 Dockerfile 的 `ENV MS_UPSTREAM=fake`）⇒ `msvc/provider.py:256/271/301` 的 `import litellm` 抛 `ModuleNotFoundError` | ✅ **已修**：改成 `pip install --no-cache-dir ".[real]"` |
| **B2** | **server 容器起不来**（重启循环） | 若把 `deploy/.env` 照抄成 `.env.prod`，里面 `MODEL_SERVICE_BASE_URL=http://127.0.0.1:8123` 是**本机桩地址**；容器内 `127.0.0.1` 指向自己。而 `server/app/main.py:85-86` 在 `LLM_BACKEND=remote` 时 `await startup_self_check()`，失败即**拒绝启动** | ⏳ 部署时注意：`.env.prod` 里写 **`http://model-service:8000`**（compose 默认值本身就是对的，别从 `.env` 抄） |
| **B3** | **构建阶段卡死** | 三个 Dockerfile 全走公网默认源：Docker Hub（`server:2`/`model-service:1`/`web:12,18,27`）、pypi.org（`server:8`、`model-service:5`）、registry.npmjs.org（`web:15`）。大陆 ECS 拉基础镜像基本必然超时 | ⏳ 部署时注意：配 Docker 镜像加速 + pip/npm 国内源；或**在能联网的机器 build 好再推镜像** |
| **B4** | **部署成功但用不了**：进不了管理端 ⇒ 无法启用模型 ⇒ 聊天 404 | 注册出来的永远是 `role="user"`（`auth.py:35-39` + `models/user.py:23`）；提权脚本 `server/scripts/make_admin.py` **没被 COPY 进镜像**（`server/Dockerfile:10-12` 只拷 `app`/`alembic`/`alembic.ini`；`pyproject.toml:33` 的 `packages.find` 也只含 `app*`）⇒ 容器里 `python -m scripts.make_admin` 必 `No module named 'scripts'`。且**没有任何提权 API**（`deps.py:38-39` 硬判 `role != "admin"` → 403） | ✅ **已修**：`server/Dockerfile` 加 `COPY scripts ./scripts`。**无需改 `pyproject.toml`**——`python -m` 靠 CWD 解析，已实测 |

**B1/B4 的修复验证方式（可复现，本机实测）**：

- **B1**：在临时 venv 里 `pip install --dry-run ".[real]"` ⇒ 实测拉取
  `litellm-1.105.1` 及其依赖树（`fastuuid`/`filelock`/`openai` 等）；
  而旧命令 `pip install .` 不装其中任何一个。
- **B4**：复刻容器的 `/app` 布局后执行 `python -m scripts.make_admin`：
  新布局 ⇒ 打印用法并 `exit 1`（**脚本被找到**）；
  旧布局（去掉 `scripts/`）⇒ `ModuleNotFoundError: No module named 'scripts'`（**复现原故障**）。
  这一正一反构成因果证明，不只是"改完看起来对"。

> ⚠️ 两处修改**都未经过 `docker build`**（本机无 Docker），
> 但已用「等价命令 + 布局复刻 + 反向对照」在上表证明其有效性。
> 仍留在真实构建中验证的是：Docker 层缓存顺序、`.[real]` 在 slim 镜像里的编译依赖。

**两条"部署后当场会撞到"（不是致命，但会让人以为部署失败）**：

- **`web` 首屏 502**：`docker-compose.prod.yml:116-118` 里 `web` 对 server 是
  `condition: service_started`，但 server **要先跑 `alembic upgrade head` 才 listen**
  （`server/Dockerfile:17`），而 server **没有 healthcheck**。⇒ 首次打开报
  `ECONNREFUSED`。**刷新即好**，不是永久故障。
- **`server` 冷启动崩溃重启若干轮**：server 只 `depends_on` postgres，
  **不依赖 model-service**，但启动自检要调 `/v1/capabilities`。model-service 未就绪时
  server 抛 `ProtocolVersionError` 退出，靠 `restart: unless-stopped` 自愈。
  **日志一片红但最终会好**——别误判为部署失败。

**一个建议的低成本消歧**：`web/public` 目录在本仓库不存在，而
`web/Dockerfile:43` 是 `COPY --from=builder /app/public* ./public/`。
它**推定**能工作（无匹配通配符 → 空集合 + 目标以 `/` 结尾 → 建空目录），
但本机无 docker **未实测**。建一个空的 `web/public/.gitkeep` 即可把这个不确定性归零。
（注：`web/` 属前端 agent 写权限。）

### 5.1 代码怎么上去

仓库远端在 GitHub，而**大陆机器访问 GitHub 频繁失败**（本仓库 `AGENTS.md` 〇.2 已记录
"Connection reset / 443 超时，单次可达 21s 后失败"）。所以**别指望 `git clone`**。

三个可行办法，按推荐顺序：

1. **本地打包上传**（最稳）
   ```powershell
   # 本地 Windows：打成 zip（git archive 只含已提交文件，自动排除 .env / dev.db）
   git archive --format=zip -o chatbot.zip HEAD
   ```
   ```bash
   # 服务器：从本地上传（在你自己的 Windows 上执行 scp）
   scp chatbot.zip root@<公网IP>:/root/
   ```
   ⚠️ **`git archive` 只含已提交内容**。本仓库当前 `web/` 下有**未提交改动**
   （前端 agent 在飞），`git archive HEAD` 会**丢掉它们**。要带上去就先在本地提交，
   或改用 `tar` 打包工作区（但会带上 `node_modules` 等，需排除）。

2. **走国内镜像/中转**：把仓库同步到 Gitee 后再在服务器 `git clone`。

3. **改远端**：❌ 不要为了省事改 `git remote`（违 `AGENTS.md` 〇.2-5）。

### 5.2 配置环境变量

```bash
cd /root/chatbot-platform/deploy
cp .env.prod.example .env.prod
# 生成三个随机值（≥32 字节）
openssl rand -hex 32   # → POSTGRES_PASSWORD
openssl rand -hex 32   # → SECRET_KEY
# （MS_SERVICE_TOKENS 同理）
```

**必须改的项**（模板 95 行、29 个变量已有说明，下列是**必须逐项确认**的）：

| 变量 | 值 | 为什么 |
|---|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` | compose 用 `${VAR:?}` 强制，不设直接拒绝启动 |
| `SECRET_KEY` | `openssl rand -hex 32` | **≥32 字节**。本机 dev 的只有 29 字节，已触发 `InsecureKeyLengthWarning`；生产必须换 |
| `MODEL_SERVICE_TOKEN` | `openssl rand -hex 32` | 平台→模型服务的静态 Bearer，**与 `MS_SERVICE_TOKENS` 必须成对**（值相同） |
| `MS_SERVICE_TOKENS` | 同上的值 | 服务侧校验；两边不一致 ⇒ 模型服务 401 `service_auth_failed` |
| `SIGNUP_GRANT_CREDITS` | `0` | `/register` 公开无频控，送额度 = 让人刷（模板默认已是 0） |
| `LLM_BACKEND` | `remote` | `APP_ENV=prod` 下为 `mock` 会**拒绝启动**（红线 9 守卫） |
| `MS_UPSTREAM` | **`litellm`** | ⚠️ 模板出厂是 `fake`，**不改就只有假回复**（见下方陷阱） |
| `MS_LITELLM_API_BASE` | `https://api.xiaomimimo.com/v1` | 上游端点 |
| `MS_LITELLM_API_KEY` | `<你的小米 key>` | **真凭证，只落这个文件，别提交** |
| `MS_LITELLM_DEFAULT_MODEL` | **`openai/mimo-v2.6-flash`** | ⚠️ **必须带 `openai/` 前缀**，理由见下方「目录校验」陷阱 |
| `MS_UPSTREAM_VENDOR` | `xiaomi-mimo` | 归因；命中拒绝名单会拒绝启动 |
| `MS_CATALOG_MODELS` | 保持默认，**或**加进你要用的无前缀模型名 | 无前缀 model 走目录校验，漏了即 404 |

> ⚠️ **「目录校验」陷阱（第二个静默坑，且症状是 404）**：
> `model-service/msvc/main.py:175` 的判定是
> `if model and model not in settings.models and "/" not in model: → 404 model_not_found`
> —— 即 **model 名里带 `/` 就跳过目录校验**，不带 `/` 就必须列在 `MS_CATALOG_MODELS` 里。
>
> 本机 `server/dev.db` 实测的默认模型是 **`openai/mimo-v2.6-flash`（带前缀）**，
> 而模板默认目录是 `deepseek-chat,qwen-plus`（不带 `mimo`）。
> 所以：
> - 填 `openai/mimo-v2.6-flash` → 带 `/`，免检，**可用** ✅
> - 填 `mimo-v2.6-flash`（漏了前缀）→ 走目录校验、不在名单里 → **404 `model_not_found`** ❌
>
> 你若坚持用不带前缀的名字，就必须同时把它加进 `MS_CATALOG_MODELS`。

⚠️ **`MS_COMPLIANCE_DENY_PROVIDERS` 默认含 `openai,anthropic`**，`xiaomi-mimo` 不在其中，正常。
若你换了上游供应商名，**先确认它不在拒绝名单里**，否则启动即失败
（`model-service/msvc/config.py:113-119`，**仅当 `MS_UPSTREAM=litellm` 时才检查**；
配 `fake` 时这条守卫直接跳过——这也是为什么它挡不住上一段那个假上游问题）。

> ⚠️ **一个静默陷阱（一定要改）**：模板里 `MS_UPSTREAM` 出厂值是 **`fake`**
> （离线假上游：链路全真但**回复是假的**）。忘了改成 `litellm` 的话，
> 网站能正常打开、能注册、能聊天、**回复看起来也像话**，但全是假的，且不产生真实费用。
> 这是最容易被漏掉、又最难察觉的一项——**上线后务必按 §六.3 确认真模型在回话**。

⚠️ **红线 8**：`MS_UPSTREAM_FIRST_TOKEN_TIMEOUT`(15) / `CHUNK_IDLE`(30) / `TOTAL`(60)
必须都**严格小于**平台流式空闲上限（默认 **90s**），**等于也拒绝**
（`server/app/llm/model_service_ops.py` 用 `if inner >= outer: raise`）。
模板默认已满足，**别手动调大**。

两点补充（同一段代码，值得知道）：
- **外层不是恒为 90s**：真实基准取决于 `features.stream_keepalive`——
  上游**会发心跳**时外层放宽到 **180s**，不发时才是 90s。
  所以「调大内层超时」这件事**不能一概而论**，要看你的上游档位。
- **这四项键缺一即拒启动**（断言是"缺键即失败"，不是"缺键跳过"）。
- 另一个独立约束：`MS_IDEMPOTENCY_ERROR_TTL_SECONDS` **必须小于**
  `MS_IDEMPOTENCY_WINDOW_SECONDS`（RB-5），否则服务拒绝启动。模板默认 60 < 600，正常。

### 5.3 一个容易踩的坑：`.env.prod` 的传入方式

compose 里的 `${VAR}` 不是自动读 `.env.prod`，而是靠命令行指定：

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

**`--env-file .env.prod` 不能省**（文件名是 `.env.prod` 而非常规的 `.env`，compose 不会自动加载）。
省了就会因 `${VAR:?}` 直接报错拒绝启动——这是**故意的**设计，不是 bug。

### 5.4 起服务

```bash
cd /root/chatbot-platform/deploy
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
```

---

## 六、验收（照做，别跳）

### 6.1 服务与端口

```bash
# 1) 四个容器都应 Up
docker compose --env-file .env.prod -f docker-compose.prod.yml ps

# 2) 只有 web 发布了宿主端口（关键安全项）
docker compose --env-file .env.prod -f docker-compose.prod.yml ps --format "{{.Service}} {{.Ports}}"
#    期望：只有 web 显示 0.0.0.0:3000->3000；postgres/redis/server/model-service 为空

# 3) 数据库不能从公网直连（应被拒绝/超时，而不是连上）
nc -zv <公网IP> 5432     # 期望：失败

# 4) 入口健康
curl http://127.0.0.1:3000/healthz
#    期望：{"status":"ok","backend":"remote"}
```

### 6.2 公网可达性（从你自己的电脑，不是服务器上）

```powershell
curl.exe http://<公网IP>:3000/healthz
```

打不通依次查：**安全组** → **ufw** → **容器是否 Up** → **web 是否绑 0.0.0.0**。

### 6.3 端到端（最关键的一步）

浏览器打开 `http://<公网IP>:3000`：
1. 注册账号
2. 建一个人设（**注意：必须先启用一个模型，否则开流即 404 `model_not_found`**——见下方说明）
3. 发一条消息，确认**回复是流式吐出来的**，不是一次性出现

> 流式是关键验收点。若回复卡住不吐字，多半是**上游不可达**或**红线 8 超时配置**问题。

**怎么判断回的是真模型还是假上游**（很关键，见 §五.2 的静默陷阱）：

```bash
# 看 model-service 实际用的是哪个上游
docker compose --env-file .env.prod -f docker-compose.prod.yml \
  exec model-service printenv MS_UPSTREAM
#   期望：litellm        ❌ 若是 fake ⇒ 回复是假的，回去改 .env.prod
```

另可看上游请求是否真的发生（有出站请求/耗时才对）：

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml logs --tail=50 model-service
```

> ⚠️ **注意：`/healthz` 查不出这件事。** `server/app/main.py` 的 `/healthz` 返回的
> `backend` 字段是 **`LLM_BACKEND`**（`remote`），**不是** `MS_UPSTREAM`。
> 所以 §六.1 第 4 条那条 `{"status":"ok","backend":"remote"}` **只能证明平台侧配置对**，
> 配了假上游它也照样返回 `remote`。**判断真假上游只能靠上面那句 `printenv`。**

### 6.3.1 关于「必须先启用模型」（云端新库必踩）

这是**空库首次部署的必然现象**，不是你配错了。机制链（已核对代码）：

1. 模型表靠**惰性播种**：首次访问 `/api/v1/models` 时 `_seed_if_empty`
   （`server/app/api/native/models.py:23-29`）插入 3 行，其中默认项是
   **`gpt-4o-mini`**（`SEED_MODELS`，`models.py:16-20`，`is_default=True`）。
   注意 `LlmModel.enabled` 默认是 **`True`**（`server/app/models/llm_model.py:25`），
   所以**播种出来的行是启用的**——"没有启用的模型"并不是这里的坑。
2. 于是平台默认用 `gpt-4o-mini`（`server/app/services/chat.py:130-139` 的
   `default_model_string` 取 `is_default=True` 那行）。
3. 但 `gpt-4o-mini` **不在**模型服务目录里且**不含 `/`** ⇒ 被
   `model-service/msvc/main.py:175` 的目录校验挡下 ⇒ **404 `model_not_found`**。

**本机能用不代表云端能用**：本机 `server/dev.db` 里 `llm_models` 有 5 行且默认是
`openai/mimo-v2.6-flash`（带 `openai/` 前缀 ⇒ 免检）；云端是**全新的卷、全新的库**，
必然从上面那条链开始。

### 6.3.2 拿到管理员账号（**这是真正的前置**，B4）

**注册出来的账号永远是普通用户**（`auth.py:35-39` 不传 role），而
「启用模型」的接口全是 admin-only（`deps.py:38-39` 硬判 `role != "admin"` → 403）。
所以你必须先把自己提成管理员，否则进不了管理端、也就没法完成 6.3.1 那一步。

**若 `server/Dockerfile` 已按 B4 补了 `COPY scripts ./scripts`**：

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml \
  exec server python -m scripts.make_admin 你的邮箱@example.com
```

**若没有补（当前仓库状态）**，脚本进不了容器，只能直接改库：

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml \
  exec postgres psql -U chatbot -d chatbot \
  -c "UPDATE users SET role='admin' WHERE email='你的邮箱@example.com';"
```

改完**重新登录**（旧 JWT 里带的还是老 role），确认能进管理端，再启用模型。

### 6.3.3 启用哪个模型

启用一个**能与上游对上**的模型。按 §五.2 的「目录校验」规则，两条路都行：

- 用**带前缀**的 `openai/mimo-v2.6-flash`（带 `/` ⇒ 免目录校验），或
- 用不带前缀的名字，但**同时**把它加进 `MS_CATALOG_MODELS`。

启用后把它设为默认，或建人设时显式指定，再聊天。

### 6.4 内存与构建观察

```bash
free -h              # 看构建后剩余内存
df -h /              # 看磁盘，构建缓存可能吃掉几个 G
docker stats --no-stream
```

若构建期 OOM，见 §七.1。

### 6.5 开机自启

compose 里各服务都写了 `restart: unless-stopped` ⇒ **Docker 启动后容器自动拉回**。
但**前提是 Docker 守护进程本身开机自启**（官方安装包默认已启用）：
```bash
sudo systemctl is-enabled docker    # 期望：enabled
```
> ⚠️ 本机 Windows 那套 `deploy/local/autostart.ps1`（计划任务）**在 Linux 上用不到**，
> 也不要照搬——见 §八「已知差异」。

---

## 七、风险与预案

### 7.1 构建时 OOM（最可能出的问题）

`next build` 与 `pip install litellm` 都是内存大户。2G 机器上若被杀：

```bash
# 加 2G swap（临时缓解；有 swap 会慢但能过）
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

根治办法是升到 2核4G。**若你买的是 2G，请预期要加 swap。**

### 7.2 上游不可达 / key 失效

现象：聊天无回复或报错。查：
```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml logs --tail=100 model-service
```
常见原因：key 过期、余额不足、`MS_LITELLM_API_BASE` 写错、供应商名撞拒绝名单。

### 7.3 端口被封或运营商拦截

极少数机房对某些端口有限制。**换一个高位端口**（如 8080、3000、18080）试。
若 80/443 被要求备案提示，属于正常，**别去开 80**。

### 7.4 磁盘被构建缓存吃满

```bash
docker system df          # 看占用
docker builder prune      # 清构建缓存（谨慎，下次构建变慢）
```

### 7.5 无 HTTPS 的安全风险

**这是本方案已知的最大短板**。IP + 端口 = 明文传输，登录口令可被中间人截获。
- 只发给熟人 ⇒ 风险可接受。
- 要公开 ⇒ **先补备案 + 域名 + 证书**（`web` 前面再加一层 Nginx/Caddy 做 TLS 终止）。

### 7.6 对当前本机的影响（重要）

上线云服务器**不会**影响你本机那套。两者独立。
本机服务此前已停止、计划任务已被你删除——**云端部署与它无关，不需要动本机任何东西**。

---

## 八、与本机部署的已知差异（别照搬）

| 本机（`deploy/README.md`） | 云服务器（本文） |
|---|---|
| Windows + PowerShell | Linux + bash |
| Docker Desktop（需 WSL2、管理员） | Docker Engine（一键脚本，无需管理员） |
| 公网穿透（natapp 等） | **不需要**，服务器本身有公网 IP |
| 计划任务开机自启 | Docker `restart: unless-stopped` + systemd |
| `deploy/local/*.ps1` 脚本 | ❌ **用不到**，是 Windows 专用 |
| 只给局域网用 | 给公网用（安全组必须收紧） |

> ⚠️ `deploy/local/` 下 5 个 `.ps1` 与 `deploy/README.md` 第八节是**本机专用**产物。
> 本文不复用它们，也不需要修改它们。

---

## 八点五、后续怎么更新服务器

### 8.5.1 先理解三件事（决定了更新的做法）

**① 代码是"烤"进镜像的，不是挂载的。**
`docker-compose.prod.yml` 里三个服务全是 `build:`，**没有任何源码挂载**
（`server` 那段还有注释明说"刻意不挂载 `../server/app`"）。
⇒ **改任何代码都必须重建镜像**，改完不重建等于没改。这跟本机 `next dev` 热重载完全不同。

**② 数据在命名卷里，重建镜像不会丢。**
`pgdata`（`compose:29`）与 `redisdata`（`compose:41`）是**命名卷**，
由 compose 管理，**不随 `down` 删除**。⇒ 更新代码不会丢账号、会话、人设。
⚠️ 但 `docker compose down -v` 会**删卷**（`-v` 就是删卷）——**永远不要加 `-v`**。

**③ 迁移会在容器启动时自动跑。**
`server/Dockerfile:17` 的 CMD 是 `alembic upgrade head && uvicorn ...`。
⇒ 更新后 server 一启动就自动升级数据库结构，**你不用手动跑迁移**。
好处是省事；代价是**有问题的迁移会自己生效**（见 8.5.5 回滚）。

### 8.5.2 标准更新流程（四步）

```bash
cd /root/chatbot-platform          # 或你的实际路径

# 1) 把新代码弄上去（见 8.5.3，别用 git clone）
#    假设你已上传并解压到当前目录

# 2) 重建并重启（只重建有变化的服务）
docker compose --env-file deploy/.env.prod -f deploy/docker-compose.prod.yml \
  up -d --build

# 3) 看结果
docker compose --env-file deploy/.env.prod -f deploy/docker-compose.prod.yml ps
curl http://127.0.0.1:3000/healthz

# 4) 冒烟测试：浏览器打开，发一条消息，确认流式正常
```

**`up -d --build` 的语义**：重建所有**有 build 的**服务的镜像（postgres/redis 用现成
image 不受影响），然后**只重启变化了的容器**——没变的容器不动，所以**停机时间很短**。
但注意 `--build` 会重建全部三个自建镜像；想更快见下面 8.5.4。

### 8.5.3 代码怎么上传（大陆机器的现实）

**别用 `git clone`**：本仓库远端在 GitHub，大陆机器频繁失败
（`AGENTS.md` 〇.2 记录 `Connection reset` / 443 超时）。

**推荐：本地打包 → scp**

```powershell
# 在你的 Windows 上（工作区根目录）
git archive --format=zip -o deploy.zip HEAD
scp deploy.zip root@<公网IP>:/root/
```

```bash
# 在服务器上
cd /root && rm -rf chatbot-platform && mkdir chatbot-platform
cd chatbot-platform && unzip -q /root/deploy.zip
```

⚠️ **`git archive HEAD` 只含已提交内容**。本仓库 `web/` 下**现在有 11 项未提交改动**
（8 改 + 3 新增，前端 agent 在飞）——`git archive` 会**丢掉它们**。
要带上就先在本地提交，或改用 `tar` 打包工作区（记得排除 `node_modules`/`.next`/`.venv`）。

⚠️ **别用 `scp -r` 整目录**。实测 `server/dev.db` 里 **`llm_models` 有 2 行带明文
`api_key`**（长度 51 / 35，实测查得），另有 `server/.env`、`deploy/.env` 等真凭证。
`git archive` 自动排除这些（已核实 `.gitignore` 拦截）。

⚠️ **改动 `deploy/.env.prod` 后**：`environment` 段变了，`up -d` 会重建容器生效，
**不需要** `--build`（配置不是烤进镜像的）。

### 8.5.4 只更新一个服务（省时间）

**只改前端**（最常见，也最慢——`next build` 是构建大头）：

```bash
docker compose --env-file deploy/.env.prod -f deploy/docker-compose.prod.yml \
  up -d --build web
```

**只改后端**：

```bash
docker compose --env-file deploy/.env.prod -f deploy/docker-compose.prod.yml \
  up -d --build server
```

**只改模型服务**（`MS_*` 配置或 `msvc/` 代码）：

```bash
docker compose --env-file deploy/.env.prod -f deploy/docker-compose.prod.yml \
  up -d --build model-service
```

> 注意 `web` 依赖 `server`：只重建 `web` 时 `server` 若也在变，要一起带上，
> 否则前端会连到旧后端。

### 8.5.5 出问题怎么回滚

**先看日志再动手**：

```bash
docker compose --env-file deploy/.env.prod -f docker-compose.prod.yml logs --tail=80 server
```

**代码回滚**：`git checkout` 到旧 commit → 重新 `up -d --build`。
因为镜像是从源码重建的，回滚 = 用旧源码重建。

**数据库回滚**（更需谨慎）：7 个迁移**都有 `downgrade`**，理论可 `alembic downgrade -1`。
但：

```bash
# ⚠️ 容器内执行；且先备份！
docker compose --env-file deploy/.env.prod -f docker-compose.prod.yml \
  exec server alembic downgrade -1
```

⚠️ **`downgrade` 只还原"结构"，不还原"数据"**。例如某迁移删了一列，
`downgrade` 会把列加回来，**但原来那列里的数据回不来**。本仓库 `docs/dw5-credential-migration-plan.md`
对这类不可逆性有明确记录（"U4 之后不存在无损回滚"）。⇒ **别把 downgrade 当备份用。**

**唯一的真备份是 pg_dump**（见 8.5.6）。

### 8.5.6 更新前该做的备份（建议养成习惯）

```bash
# 备份数据库到宿主（更新前跑一次，几秒钟）
docker compose --env-file deploy/.env.prod -f docker-compose.prod.yml \
  exec -T postgres pg_dump -U chatbot chatbot > backup-$(date +%F-%H%M).sql

# 确认文件非空（空文件说明备份失败）
ls -lh backup-*.sql
```

**恢复**：

```bash
docker compose --env-file deploy/.env.prod -f docker-compose.prod.yml \
  exec -T postgres psql -U chatbot -d chatbot < backup-2026-10-16-1930.sql
```

> `-T` 是必需的（禁用 tty 分配，否则重定向会坏）。
> ⚠️ 本仓库**没有自动备份机制**——这一步纯靠你手动，别跳过。

### 8.5.7 一张速查表

| 你改了什么 | 怎么做 | 会停机吗 | 数据会丢吗 |
|---|---|---|---|
| `web/` 前端代码 | `up -d --build web` | 秒级 | 否 |
| `server/` 后端代码 | `up -d --build server` | 秒级 | 否（迁移自动跑） |
| `model-service/` 代码 | `up -d --build model-service` | 秒级 | 否 |
| `deploy/.env.prod` 配置 | `up -d`（**不加 `--build`**） | 秒级 | 否 |
| 只重启不重建 | `restart <服务>` | 秒级 | 否 |
| 加了新迁移 | 自动跑（`server` 启动时） | — | 否 |
| `down`（不加 `-v`） | 停并删容器，**卷保留** | 停机 | **否** |
| `down -v` | ⚠️ **删卷** | 停机 | **是！永远别用** |

### 8.5.8 两个坑

**坑 1：更新时别忘了重建模型服务镜像。**
若只改了 `model-service/` 却跑了 `up -d --build web`，模型服务的镜像**不会更新**，
你会以为改了却没生效。⇒ 按 §8.5.7 对照"你改了什么"来选服务名。
（原 B1「litellm 没装」已于 2026-10-16 修复，不再是复现项。）

**坑 2：更新后第一次打开网页可能报 502。**
原因同 §五.0：`web` 抢跑、server 还在跑 `alembic`。**刷新即可**，不是更新失败。


---

## 九、待办清单（按顺序）

> 🚨 **在把地址发给任何陌生人之前，先读 §七.5。**
> 本方案没有 HTTPS，而 `/register` 是公开、无审核、**无频控**的接口。
> 「先跑起来看看」只适合发给熟人；**要对外开放就必须先补 TLS**（备案 + 域名 + 证书）。
> 这个次序不要颠倒。

- [ ] **1. 决定预算与厂商**（§三），确认**续费价**再下单
- [x] **1.5 修阻塞项**：B1（litellm）与 B4（管理员）**已于 2026-10-16 修复并验证**（§五.0）；
      B2/B3 属配置层，在下面第 6、7 步处理
- [ ] **2. 买机器**：大陆节点 / Ubuntu 24.04 / **2核4G 优先**（2核2G 亦可，需加 swap）/ 40G+ / 3M+
- [ ] **3. 配安全组**：只开 22（限你 IP）+ 3000（§四.2）
- [ ] **4. 装 Docker Engine** 并验证 `docker compose version`（§四.3）
- [ ] **5. 传代码**：`git archive` 打包 + `scp`（⚠️ 当前 `web/` 有未提交改动，见 §五.1）
- [ ] **6. 写 `.env.prod`**：三个随机值 + 上游 key + `SIGNUP_GRANT_CREDITS=0`（§五.2）
- [ ] **7. `up -d --build`** 并确认四个容器 Up（§五.4）
- [ ] **8. 跑验收** §六.1 → §六.3（**端到端流式聊天必须过**）
- [ ] **8.5 提管理员 + 启用模型**（§六.3.2 / §六.3.3）—— 没有这步聊天必 404
- [ ] **9. 用手机 4G 访问一次**，确认非同一网络可用
- [ ] **10. 决定是否备案**：要长期公开就必须解决 HTTPS（§七.5）

---

## 十、验证程度声明

**本仓库实测（可复现）**：
- `deploy/docker-compose.prod.yml` 只有 `web` 发布宿主端口；其余服务仅内网可达
  （`web` 有 `ports:`；postgres/redis/server 处是「刻意不写」的注释；model-service 仅 `expose`）。
- `${VAR:?}` 强制注入：缺 `POSTGRES_PASSWORD`/`SECRET_KEY`/`LLM_BACKEND` 时拒绝启动（仅此三个）。
- `APP_ENV=prod` + `LLM_BACKEND=mock` 会拒绝启动（红线 9，`server/app/main.py:72-77`）。
- `deploy/.env` 实际值：`LLM_BACKEND=remote`、`MS_UPSTREAM=litellm`、上游 `api.xiaomimimo.com`、
  `MS_LITELLM_DEFAULT_MODEL=mimo-v2.6-flash`、`MS_UPSTREAM_VENDOR=xiaomi-mimo`。
- 构建产物尺寸（独立复测两次，误差 <0.5%）：`web/node_modules` 382MB / `web/.next` 273MB / `server/.venv` 296MB。
- `web/next.config.mjs` 用 `output: "standalone"`，`web/Dockerfile` 为多阶段。
- 应用对 Host 头不敏感，HTML 资源引用全为相对路径。
- 红线 8 断言用 `>=`（等于也拒绝）；外层基准随 `features.stream_keepalive` 在 90s/180s 间变化。
- RB-5：`MS_IDEMPOTENCY_ERROR_TTL_SECONDS` ≥ `MS_IDEMPOTENCY_WINDOW_SECONDS` 即拒启动
  （`model-service/msvc/config.py:140`）。
- 目录校验：`model-service/msvc/main.py:175` 对**无 `/`** 的 model 走 `MS_CATALOG_MODELS` 白名单，不在则 404。
- 合规拒绝名单默认 `openai,anthropic`（`msvc/config.py:37`），且该守卫仅在 `MS_UPSTREAM=litellm` 时生效。
- `SIGNUP_GRANT_CREDITS` 在模板与 compose 两处都默认为 0。
- `web/` 下现有 11 项未提交改动（8 改 + 3 新增）⇒ `git archive HEAD` 会丢掉它们。

> 本节多数条目由一名独立审计员在本仓库逐条只读复核（10 项断言全部成立），
> 其中两条（`MS_CATALOG_MODELS` 前缀语义、`/healthz` 查不出 `MS_UPSTREAM`）是复核时才发现的，
> 已据此修正 §五.2 与 §六.3。

**未验证（务必现场确认）**：
- ⚠️ `docker-compose.prod.yml` **从未真正 `docker compose config` 过，也从未 `up` 过**
  ——本机没装 Docker。§六.1 的端口断言是**结构性推定**，不是跑出来的。
- ⚠️ `web/Dockerfile` **从未 `docker build` 过**（runner 目录布局是本地复刻验证的）。
- ⚠️ 各厂商**当前价格、活动、续费价**——本会话无法联网核实。
- ⚠️ 2 核 2G 上构建**是否真的 OOM** —— 未实测，属风险提示；`server/.venv` 是 **Windows** venv
  （含平台 wheel），**不能**当 Linux 镜像体积。
- ⚠️ 云端空库首次部署的 `model_not_found` 链路是按代码推定的，本机 `dev.db` 非空、**未复现**。

**行业通识（非本仓库结论）**：内存/带宽经验值、swap 做法、安全组与 ufw 双层、各厂商活动形态。

---

## 附：为什么最终没走「穿透」

你上一次的方案是**本机 + 公网穿透**。本方案换成云服务器后，穿透整条路线就不需要了：

| | 本机 + 穿透 | 云服务器 |
|---|---|---|
| 依赖你家电脑开着 | ✅ 必须一直开 | ❌ 不需要 |
| 地址 | 厂商给的临时/固定域名 | 你的 IP + 端口 |
| 稳定性 | 受家庭宽带上行/断电影响 | 机房 7×24 |
| 成本 | 穿透费（natapp VIP 约 ¥9/月） | 服务器费（¥60–120/月） |
| HTTPS | 部分套餐支持 | 需自备（未备案则无） |
| 备案 | 不需要 | 大陆节点用高位端口也不需要 |

**穿透适合「先给别人看一眼」，云服务器适合「真的要长期开着」。**
你现在的意图是后者，所以选云服务器是对的。
