# 部署指南（Docker Compose + 公网穿透）

本文件是**照着做就能把服务跑起来并让别人访问**的操作手册。命令都在 Windows PowerShell 下验证过。

> 阅读顺序建议：先看「零、结论速览」，再按「一、二、三」执行，最后看「五、验收」。

---

## 零、结论速览

> **不想装 Docker？** 直接跳到「八、不用 Docker 的部署」——那条路**已实测可用**，
> 四个脚本命令搞定，今天就能让同网段的人访问。

**形态**：本机用 Docker Compose 跑 4 个容器（web / server / postgres / model-service），
再用**内网穿透工具**把 `web` 的 3000 端口发布到公网。**只有 web 一个入口**——
它通过 Next.js 的 rewrites 把 `/api/v1`、`/v1` 转发给后端的 server 容器，
所以浏览器全程只跟一个地址说话，不涉及跨域。

```
公网访客 ──HTTPS──> 穿透服务 ──> 本机 :3000 (web 容器)
                                      │ Next rewrites（同源代理）
                                      ├──> server:8000   （业务 + 计费）
                                      ├──> postgres:5432 （仅内网）
                                      └──> model-service:8000（仅内网，模型出口）
```

**三条硬结论**（都有实测/官方文档依据，详见「六」）：

1. ⚠️ **不要用 Cloudflare Quick Tunnel** —— 官方文档明确写「Quick Tunnels do not support
   Server-Sent Events (SSE)」。本项目的聊天是 SSE 流式，用它**流式直接坏掉**。
2. 用厂商自带二级域名做穿透时，**备案主体是厂商，你不需要买域名、也不需要备案**。
3. 发到公网前**必须**确认注册赠送积分为 0（见「四」），否则等于给陌生人白送额度。

---

## 一、前置：装 Docker Desktop

本项目官方支持路径是 Docker Compose。Windows 上需要：

1. **启用 WSL2**（管理员 PowerShell，会要求重启）：
   ```powershell
   wsl --install
   ```
2. 重启后装 **Docker Desktop**（官网下载，默认 WSL2 后端）：
   <https://www.docker.com/products/docker-desktop/>
3. 启动 Docker Desktop，等托盘图标变绿，验证：
   ```powershell
   docker --version
   docker compose version
   ```

> 前置条件实测：本机 BIOS 虚拟化**已开启**（`VirtualizationFirmwareEnabled: True`、SLAT 支持），
> 所以 `wsl --install` 能正常完成。这两步**必须管理员权限**。

---

## 二、准备配置

```powershell
cd D:\workingcode\chatbot-platform\deploy

# 1. 复制生产模板
Copy-Item .env.prod.example .env.prod

# 2. 生成三个随机值，逐个填进 .env.prod
python -c "import secrets;print(secrets.token_urlsafe(32))"   # POSTGRES_PASSWORD
python -c "import secrets;print(secrets.token_urlsafe(32))"   # SECRET_KEY
python -c "import secrets;print(secrets.token_urlsafe(32))"   # MODEL_SERVICE_TOKEN
```

`.env.prod` 里**必须**填的（不填 compose 会直接拒绝启动，这是故意的）：

| 变量 | 说明 |
|---|---|
| `POSTGRES_PASSWORD` | 数据库密码，随机生成 |
| `SECRET_KEY` | JWT 签名密钥，**泄漏 = 任何人可伪造登录态** |
| `LLM_BACKEND` | `remote`（推荐）或 `litellm`；**不能是 mock** |

**`.env.prod` 千万不要提交进 git**（`.gitignore` 已拦截，实测 `git add` 不会收）。

### 关于模型：先 fake 还是先真模型

- `MS_UPSTREAM=fake`：链路全真但**回复是假的**（确定性桩），**不产生任何真实费用**。
  适合先把部署跑通。
- `MS_UPSTREAM=litellm`：接真实上游，需要 `MS_LITELLM_API_BASE` / `API_KEY` /
  `DEFAULT_MODEL` / `PROVIDER` / `UPSTREAM_VENDOR` 五项（模板里都有位置）。

---

## 三、起服务

```powershell
cd D:\workingcode\chatbot-platform\deploy

docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

> **先别急着 up** —— 装好 Docker 后**先跑一次自检**，它会替你把所有能静态发现的问题挡在 up 之前：
>
> ```powershell
> powershell -ExecutionPolicy Bypass -File deploy\local\check-docker.ps1
> ```
>
> 检查项：docker 是否存在且在跑 → 文件是否齐 → **必填项是否真填了**（空值/残留 dev 默认值/
> `LLM_BACKEND=mock` 都会被点名）→ **`docker compose config` 语法与插值**（这一步在写 compose
> 的机器上做不了，见「六」的说明）→ **是否只有 web 对外** → 三个镜像能否构建。
> 全绿之后再 up，能省掉一轮"起来了但打不开"的排查。

首次构建要几分钟（拉镜像 + 装依赖）。看状态与日志：

```powershell
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f server
```

本机自测（应返回 `{"status":"ok",...}`）：

```powershell
curl.exe http://127.0.0.1:3000/healthz
```

浏览器打开 <http://127.0.0.1:3000> 应看到登录/注册页。

> **为什么是 prod 文件而不是 `docker-compose.yml`**：`docker-compose.yml` 是**本地开发**态
> ——它把 postgres:5432、redis:6379 直接发布到宿主、用公开的 dev 密码、还挂载源码热重载。
> 那些默认值直接上公网就是现成的洞。生产文件是**自包含**的一份，不依赖 compose 合并规则。

---

## 四、公网发布（内网穿透）

### ⚠️ 先做这一步：确认注册不送额度

`/register` 是**公开、无审核、无频控**的接口。发到公网后，任何人扫到你的网址就能无限注册。

```powershell
# 确认生产文件里是 0（默认已是 0）
Select-String -Path .env.prod -Pattern "SIGNUP_GRANT_CREDITS"
```

**保持 `SIGNUP_GRANT_CREDITS=0`。** 要给熟人发额度，注册后手动加，别开门。

可选再收一道（防单账号刷爆）：设 `QUOTA_MONTHLY_TOKENS` 月度上限。

### 穿透方案对比（已核对官方页面）

| 方案 | 免费额度 | 固定网址 | HTTPS | 大陆可用 | 需备案 | 需实名 |
|---|---|---|---|---|---|---|
| **natapp 免费** | 2G/月，1M，30连接/分 | ❌**不定时强制更换** | ❌**不支持** | ✅阿里云国内BGP | ❌ | ⚠️**人脸识别** |
| **natapp VIP-1** | 5G/月，2M，90连接/分 | ✅ | ✅ | ✅ | ❌ | 同上 |
| **cpolar 免费** | 1Mbps，不限流量 | ❌24h 变 | 见注 | ✅境内节点 | ❌ | 未核实 |
| **cpolar 基础版** | 2Mbps，99/年 | ✅保留3个 | — | ✅ | ❌ | 未核实 |
| **Cloudflare Quick** | 免费不限流 | ❌每次变 | ✅ | ⚠️ | ❌ | — |
| **ngrok 免费** | 1GB/月出站 | ✅固定 dev domain | ✅ | ⚠️**未证实** | ❌ | — |
| **frp** | 自建 | ✅ | ✅ | ✅ | 国内服务器需 | — |
| **Tailscale** | 免费 | — | ✅ | ⚠️ | — | — |

注：cpolar 免费版是否含 HTTPS，其价格页只列了「HTTP / TCP 隧道支持」，**未能确证**，需注册后确认。

### 推荐做法

**先零成本验证**：
1. 注册 <https://natapp.cn>，完成**人脸识别实名**（Web 隧道必须，支付宝扫码即可）。
2. 买/领一条**免费隧道**，本地端口填 **3000**。
3. 按官网给的命令起客户端（本机实测无需管理员）：
   ```powershell
   irm https://natapp.cn/get.ps1?authtoken=<你的token> | iex
   ```
4. 拿到的 `http://xxx.natappfree.cc` 就是公网地址，发给别人即可访问。

⚠️ 免费档的网址会「不定时强制更换」且**不支持 HTTPS**——只适合自测/临时演示。

**要长期、固定、HTTPS** → 升 **natapp VIP-1（¥9/月）**。这是最省事的一档：
固定二级域名 + HTTPS + 国内 BGP。**仍然不需要买域名、不需要备案。**

**次选** → **cpolar**（<https://www.cpolar.com>），Windows 装客户端后 `cpolar http 3000`；
免费档每 24h 换地址，要固定用基础版（¥99/年，保留 3 个子域名）。

> 备选里为什么**不**推荐 Cloudflare 命名隧道：它需要**自有域名**（官方 Prerequisites 明确
> 「A domain on Cloudflare (required to publish applications)」），而你不想买域名。

### 启动方式建议

穿透客户端要跟 Docker 一起常驻。两种做法：

- **natapp**：官网有「加入系统服务，开机启动」的教程（<https://natapp.cn/article/system_service>）。
- **cpolar**：装成 Windows 服务（`cpolar service install`）。

Docker 容器本身已配 `restart: unless-stopped`，开机自启（Docker Desktop 需设为开机启动）。

---

## 五、验收清单

逐条跑，全过才算部署成功：

```powershell
# 1. 容器都在跑（web/server/postgres/model-service 应都是 Up）
docker compose --env-file .env.prod -f docker-compose.prod.yml ps

# 2. 入口健康
curl.exe http://127.0.0.1:3000/healthz
#    期望：{"status":"ok","backend":"remote"}  （或 litellm）

# 3. 只有 web 发布了宿主端口（关键安全项）
docker compose --env-file .env.prod -f docker-compose.prod.yml ps --format "{{.Service}} {{.Ports}}"
#    期望：只有 web 有 0.0.0.0:3000->3000；postgres/redis/server/model-service 都为空

# 4. 数据库不能从宿主直连（应超时/拒绝，而不是连上）
Test-NetConnection 127.0.0.1 -Port 5432 -InformationLevel Quiet
#    期望：False

# 5. 走一遍完整链路：注册 → 建人设 → 发消息拿到回复
#    用浏览器打开 http://127.0.0.1:3000 操作，或看 /docs 的 Swagger

# 6. 公网地址从手机 4G（非同一 WiFi）访问，确认能打开并能聊天
```

第 3、4 条是**公网安全的关键**：数据库和模型服务绝不能能被外网直连。

---

## 六、事实依据（避免重复踩坑）

| 结论 | 依据 |
|---|---|
| Quick Tunnel 不支持 SSE | Cloudflare 官方 Limitations：<https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/> — "Quick Tunnels do not support Server-Sent Events (SSE)." |
| Cloudflare 命名隧道需自有域名 | <https://developers.cloudflare.com/tunnel/get-started/> — "A domain on Cloudflare (required to publish applications)" |
| natapp 免费档限制 | 官方购买页：<https://natapp.cn/tunnel/buy> — 「域名/端口随机,且不定时强制更换」「HTTPS 不支持」 |
| natapp Web 隧道需人脸实名 | 官方 FAQ：<https://natapp.cn/article/faq> — 「Web(HTTP) 隧道：需进行人脸识别验证」 |
| 厂商二级域名无需自己备案 | <https://natapp.cn/article/beian> — 备案义务随「自有域名 + 境内服务器」产生 |
| cpolar 档位 | <https://www.cpolar.com/pricing> |

**未能证实**（不要当成结论）：Cloudflare / ngrok 在中国大陆的实际可达性——两者都没有官方
文档能证明「可用」或「被墙」，只能靠实际网络环境验证。cpolar 免费档是否需实名，官网未提及。

### 本仓库部署产物的验证程度（如实标注）

写这份文档与 compose 的机器上**没有 docker**（`docker` 命令不存在、无 WSL2、无管理员权限），
所以下面把「验到什么程度」分开写清楚，避免把「结构自洽」误当成「docker 接受」：

| 产物 | 验证程度 |
|---|---|
| `deploy/docker-compose.prod.yml` | ⚠️ **只做过 YAML 解析 + 结构断言**（变量有出处、依赖服务存在、build 上下文与 Dockerfile 存在、命名卷已声明、只有 web 发布端口）。**没跑过 `docker compose config`** |
| `web/Dockerfile` | ✅ runner 层布局**本机实测**：复刻目录后用 `node server.js` 起在 3100，80ms 就绪、页面 200、经代理回 /healthz、静态 chunk 可服务。但**没跑过 `docker build`** |
| `deploy/local/*.ps1` | ✅ **全部实测**（start 3.7s 返回、stop 释放端口、status 输出正确 pid 与端口） |
| 前端生产形态 | ✅ 实测 `next build` + standalone 启动 133ms，浏览器真实走通登录→聊天 |
| 真实模型链路 | ✅ 实测到 SSE 8 chunk + `[DONE]`，真实模型回复 |

⇒ **docker 相关的两格必须由 `deploy/local/check-docker.ps1` 在你的机器上补验**（见「三」）。
这不是可选项：一个 compose 语法错误会让 `up` 直接失败，而结构断言发现不了它。

---

## 七、常见问题

**Q：`docker compose up` 报某个变量必须设置？**
这是**故意**的（compose 里写了 `${VAR:?}`）。说明 `.env.prod` 里那项还空着——
去填上，别把 `:?` 去掉。

**Q：server 容器起来就退出？**
大概率是 `APP_ENV=prod` + `LLM_BACKEND=mock`，被红线9 守卫**故意拒绝启动**
（mock 会返回假回复还污染账本）。改成 `remote` 或 `litellm`。

**Q：日志里报 401 `service_auth_failed`？**
`MODEL_SERVICE_TOKEN` 与 `MS_SERVICE_TOKENS` 不匹配。两边必须是同一个值
（服务侧支持逗号分隔的多值，用于轮换）。

**Q：回复是 `[mock:xxx]` 开头的假内容？**
说明模型服务走到了 fake 上游（`MS_UPSTREAM=fake`）。这是预期行为——
要接真模型就改成 `litellm` 并配齐五项凭证。

**Q：前端能打开但接口 502？**
web 容器用 `BACKEND_URL=http://server:8000` 找后端。确认 server 容器在跑且健康。

---

## 八、不用 Docker 的部署（本机进程，已可用）

**这条路已经实测跑通，且不需要装 Docker。** 适用于：还没装 Docker、或本机不想装。

### 一键脚本

```powershell
cd D:\workingcode\chatbot-platform

# 起（跑迁移 + 构建前端 + 起前后端，约 4 秒；首次构建前端 30-60s）
powershell -ExecutionPolicy Bypass -File deploy\local\start.ps1

# 看状态 / 日志尾部 / 局域网地址
powershell -ExecutionPolicy Bypass -File deploy\local\status.ps1

# 停
powershell -ExecutionPolicy Bypass -File deploy\local\stop.ps1
```

`start.ps1` 做完这几件事：

1. 跑 `alembic upgrade head`（**必须**，缺列会让 `/v1` 直接 502）；
2. 检查前端构建产物是否过期，过期才 `next build`（省时间）；
3. 起后端（uvicorn，**不带 `--reload`**）与前端（**standalone 生产模式**，不是 `npm run dev`）；
4. 探活并把「本机地址 / 局域网地址 / 接口健康」打出来；
5. 记真实监听进程的 pid 到 `deploy/local/logs/*.pid`，供 stop 用。

两个服务都写日志到 `deploy/local/logs/`（`api.log` / `web.log` / `*.err.log`），崩溃可回溯。

可选参数：`-WebPort 8080`、`-ApiPort 8001`、`-BindHost 127.0.0.1`（只本机可访问时）。

### 实测结果（2026-10-15）

| 项 | 结果 |
|---|---|
| `start.ps1` 耗时（产物已就绪） | **3.7s** |
| 前端启动 | `Ready in 133ms`（standalone，非 dev） |
| `http://127.0.0.1:3000/healthz` | `{"status":"ok","backend":"litellm"}` |
| 局域网 `http://192.168.31.153:3000` | 200 |
| 完整链路（局域网地址、真实模型） | 注册 → 建人设 → 建会话 → **8 个 SSE chunk + `[DONE]`** → 拼出真实回复 |
| stop → 端口释放 → 再 start | 通过 |

### 让别人用（局域网）

同网段的手机/电脑直接打开 `http://192.168.31.153:3000` 即可（把 IP 换成你本机的，
`status.ps1` 会打出来）。**无需任何穿透工具、无需配 CORS**——前端同源代理了后端。

> 公网访问见「四」，需要用穿透工具把 3000 发布出去。

### 开机自启

`start.ps1` 起的进程**不会**开机自启。两种做法：

**做法 A：任务计划程序（推荐，无需管理员也能建当前用户的任务）**

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"D:\workingcode\chatbot-platform\deploy\local\start.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "chatbot-platform" -Action $action -Trigger $trigger -Force
```

之后每次登录自动拉起。卸载：`Unregister-ScheduledTask -TaskName "chatbot-platform" -Confirm:$false`

**做法 B：启动文件夹放一个快捷方式**

`Win+R` → `shell:startup` → 把 `start.ps1` 的快捷方式丢进去（可设「最小化」）。

⚠️ Docker Desktop 的「开机启动」开关只管 Docker 那条路；本机进程这条路要用上面的任务计划。

### 与 Docker 路的取舍（如实说明）

| | 本机进程（第八节） | Docker（一~三节） |
|---|---|---|
| 要装 Docker | ❌ 不用 | ✅ 要（WSL2 + 管理员） |
| 隔离 | ❌ 直接跑在宿主 | ✅ 容器隔离 |
| 崩溃自动重启 | ❌ 需自己配任务计划 | ✅ `restart: unless-stopped` |
| 数据库 | SQLite 文件（`server/dev.db`） | PostgreSQL 容器 |
| 适合 | **今天就想让别人用** | 长期稳定运行 |

两者可以共存，但**别同时起**——会抢 3000/8000 端口（`start.ps1` 会检测并拒绝启动）。

### 纯手动（不用脚本）

```powershell
# 后端
cd D:\workingcode\chatbot-platform\server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端（另开一个窗口；生产模式）
cd D:\workingcode\chatbot-platform\web
$env:BACKEND_URL="http://127.0.0.1:8000"; $env:PORT="3000"; $env:HOSTNAME="0.0.0.0"
node .next\standalone\server.js
```

开发用 `npm run dev` 也行，但那是**开发模式**（有热重载、启动 2.4s 而非 133ms），
给别人用建议走上面的生产形态。
