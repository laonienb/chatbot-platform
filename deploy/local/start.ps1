# 本机部署（不用 Docker）—— 一键起/停/看状态
#
# 适用：本机没有 Docker（或不想装）时，直接用现有 Python venv + Node 跑生产形态。
#   起：  powershell -ExecutionPolicy Bypass -File deploy\local\start.ps1
#   停：  powershell -ExecutionPolicy Bypass -File deploy\local\stop.ps1
#   状态：powershell -ExecutionPolicy Bypass -File deploy\local\status.ps1
#
# 与 `npm run dev` 的区别（为什么这不是"开发模式"）：
#   - 前端走 `next build` + standalone server.js，**不是 dev**：没有热重载、
#     没有开发工具浮标、启动 ~80ms 而非 2.4s、且不监听文件变化（省 CPU）。
#   - 后端**不开 --reload**：改代码不会半途重启，适合给别人用。
#   - 两个服务都写日志到 deploy/local/logs/，崩溃可回溯。
#
# 监听地址：默认 0.0.0.0（局域网其他设备能访问）。只在本机用可改 -BindHost 127.0.0.1。
#
# [!]️ 这仍是"单机进程"方案，**没有** Docker 的隔离与自动重启。要开机自启见 README「九」。

param(
  [int]$WebPort = 3000,
  [int]$ApiPort = 8000,
  [string]$BindHost = "0.0.0.0"
)

# [!]️ 不用 $ErrorActionPreference="Stop"：alembic / uvicorn 会把 INFO 日志写到 **stderr**，
# 而 Stop 会把原生命令的 stderr 输出当成终止性错误直接中断脚本（实测踩到：
# 脚本停在 `alembic upgrade head`，报 NativeCommandError，明明迁移是成功的）。
# 故保持默认 Continue，需要严格的地方显式检查 $LASTEXITCODE。
$ErrorActionPreference = "Continue"
# $PSScriptRoot 在个别调用方式下会为空（实测 `powershell -File ...` 嵌套调用时拿到 null）。
# 用 $MyInvocation.MyCommand.Path 兜底，并对两者都为空的情况直接报错，别让它静默变成空路径。
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $scriptDir) { Write-Host "无法定位脚本所在目录，请用绝对路径调用本脚本" -ForegroundColor Red; exit 1 }
$root = Split-Path -Parent (Split-Path -Parent $scriptDir)   # 仓库根
$logDir = Join-Path $scriptDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Info($m) { Write-Host "[start] $m" -ForegroundColor Cyan }

# ---------- 前置检查 ----------
$py = Join-Path $root "server\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  Write-Host "[start] 找不到 server\.venv\Scripts\python.exe" -ForegroundColor Red
  Write-Host "        先按 README「快速开始（后端）」建好 venv 并安装依赖。" -ForegroundColor Red
  exit 1
}
if (-not (Test-Path (Join-Path $root "web\node_modules"))) {
  Write-Host "[start] web\node_modules 不存在，请先在 web/ 下跑 npm install" -ForegroundColor Red
  exit 1
}

# 端口占用检查：避免起第二个实例互相抢端口（历史上同项目并跑两个 dev server 会共用 .next 而互相污染）
foreach ($p in @($WebPort, $ApiPort)) {
  $busy = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($busy) {
    Write-Host "[start] 端口 $p 已被占用（PID $($busy.OwningProcess -join ',')）。" -ForegroundColor Yellow
    Write-Host "        先跑 stop.ps1，或换端口：-WebPort/-ApiPort。" -ForegroundColor Yellow
    exit 1
  }
}

# ---------- 1) 数据库迁移（必须，缺列会让 /v1 直接 502） ----------
# 用 cmd 的 2>&1 抑制把 alembic 的 stderr 日志当成 PowerShell 错误；失败靠退出码判断。
Info "跑 alembic upgrade head ..."
Push-Location (Join-Path $root "server")
try {
  $alembicOut = & $py -m alembic upgrade head 2>&1
  $alembicCode = $LASTEXITCODE
} finally { Pop-Location }
if ($alembicCode -ne 0) {
  Write-Host "[start] alembic 迁移失败（退出码 $alembicCode）：" -ForegroundColor Red
  $alembicOut | Select-Object -Last 15 | ForEach-Object { "         $_" }
  exit 1
}
$alembicOut | Where-Object { $_ -match "Running upgrade|already at|Will assume" } | ForEach-Object { "         $_" }
Info "迁移完成（已是 head 则无 upgrade 行）"

# ---------- 2) 构建前端（standalone） ----------
$standalone = Join-Path $root "web\.next\standalone\server.js"
$needBuild = $true
if (Test-Path $standalone) {
  $srcNewer = Get-ChildItem (Join-Path $root "web\app"), (Join-Path $root "web\lib") -Recurse -File -ErrorAction SilentlyContinue |
              Where-Object { $_.LastWriteTime -gt (Get-Item $standalone).LastWriteTime } | Select-Object -First 1
  if (-not $srcNewer) { $needBuild = $false }
}
if ($needBuild) {
  Info "构建前端（next build，首次约 30-60s）..."
  Push-Location (Join-Path $root "web")
  try {
    $buildOut = & npm run build 2>&1
    $buildCode = $LASTEXITCODE
  } finally { Pop-Location }
  if ($buildCode -ne 0) {
    Write-Host "[start] 前端构建失败（退出码 $buildCode）：" -ForegroundColor Red
    $buildOut | Select-Object -Last 15 | ForEach-Object { "         $_" }
    exit 1
  }
  $buildOut | Select-Object -Last 2 | ForEach-Object { "         $_" }
} else {
  Info "前端构建产物是最新的，跳过 build（要强制重建就删 web\.next）"
}

# ---------- 3) 起后端 ----------
# ⚠️ 为什么用 `cmd /c start /b` 而不是 `Start-Process -RedirectStandardOutput`：
# Start-Process 的重定向会让子进程**继承本脚本的 stdout 管道句柄**，于是调用方
# `powershell -File start.ps1 | Select-Object` 这类管道会一直等句柄关闭而**挂住**
# （实测：服务其实已起好、健康检查也 200 了，外层命令 5 分钟不返回）。
# `cmd /c start /b` + shell 重定向是完全脱离（detached）的，父进程能立刻返回。
$apiLog = Join-Path $logDir "api.log"
$apiErr = Join-Path $logDir "api.err.log"
Info "起后端 :$ApiPort （日志 $apiLog）"
$apiCmd = "`"$py`" -m uvicorn app.main:app --host $BindHost --port $ApiPort > `"$apiLog`" 2> `"$apiErr`""
Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "start", "/b", "/min", "cmd.exe", "/c", $apiCmd) `
  -WorkingDirectory (Join-Path $root "server") -WindowStyle Hidden | Out-Null

# ---------- 4) 起前端（standalone server.js，复刻容器里的布局） ----------
# standalone 的 server.js 不会自带 .next/static 与 public，必须就位（否则页面无样式/无 JS）
$webRoot = Join-Path $root "web"
$sa = Join-Path $webRoot ".next\standalone"
foreach ($d in @(".next\static")) {
  $dst = Join-Path $sa $d
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item -Recurse -Force (Join-Path $webRoot $d\*) $dst -ErrorAction SilentlyContinue
}
$pubDst = Join-Path $sa "public"
New-Item -ItemType Directory -Force -Path $pubDst | Out-Null
if (Test-Path (Join-Path $webRoot "public")) {
  Copy-Item -Recurse -Force (Join-Path $webRoot "public\*") $pubDst -ErrorAction SilentlyContinue
}

$webLog = Join-Path $logDir "web.log"
$webErr = Join-Path $logDir "web.err.log"
Info "起前端 :$WebPort （日志 $webLog）"
# 同样用 detached 方式起（理由见上）。
# NODE_ENV/PORT/HOSTNAME/BACKEND_URL 由 cmd 里的 set 传给子进程，不靠本进程的环境继承
# （start /b 会拷贝当前环境，但显式写更不容易被外层环境污染）。
$webCmd = "set NODE_ENV=production&& set PORT=$WebPort&& set HOSTNAME=$BindHost&& set BACKEND_URL=http://127.0.0.1:$ApiPort&& node server.js > `"$webLog`" 2> `"$webErr`""
Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "start", "/b", "/min", "cmd.exe", "/c", $webCmd) `
  -WorkingDirectory $sa -WindowStyle Hidden | Out-Null

# ---------- 5) 探活 ----------
# 就绪后再记 pid —— 记的是**真正在监听端口**的那个进程，不是 Start-Process 的包装进程。
# 实测教训：uvicorn 会派生子进程，Start-Process 返回的 Id 与 `Get-NetTCPConnection`
# 看到的 OwningProcess 不是一个（实测 27968 vs 22760）⇒ 按包装 pid 去停会停不干净。
Info "等待就绪 ..."
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 700
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$WebPort/healthz" -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { }
}

foreach ($pair in @(@{n = "api"; p = $ApiPort }, @{n = "web"; p = $WebPort })) {
  $conn = Get-NetTCPConnection -LocalPort $pair.p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($conn) {
    $conn.OwningProcess | Set-Content (Join-Path $logDir "$($pair.n).pid")
  } else {
    # 没起来也要留个 pid 文件，stop.ps1 才能兜底清端口
    $pair.p | Set-Content (Join-Path $logDir "$($pair.n).pid")
  }
}
Write-Host ""
if ($ok) {
  Write-Host "  就绪 [OK]" -ForegroundColor Green
  Write-Host "    本机：   http://127.0.0.1:$WebPort"
  $lanIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress
  if ($lanIp) { Write-Host "    局域网： http://${lanIp}:$WebPort   <- 同网段的人用这个" -ForegroundColor Green }
  Write-Host "    接口健康：$((Invoke-WebRequest "http://127.0.0.1:$WebPort/healthz" -UseBasicParsing).Content)"
} else {
  Write-Host "  启动失败或超时，看日志：" -ForegroundColor Red
  Write-Host "    $apiLog" -ForegroundColor Red
  Write-Host "    $webLog" -ForegroundColor Red
  exit 1
}

# ⚠️ 必须显式 exit：子进程（uvicorn/node）继承了本脚本的 stdout 管道句柄，
# 若靠"脚本自然结束"，调用方 `... | Select-Object` 这类管道会一直等句柄关闭而**挂住**
# （实测：脚本已把服务起好并打印完，外层命令 5 分钟不返回）。
# 显式 exit 让父进程立即结束，子进程继续在后台跑（它们是独立进程，不随父退出而死）。
exit 0
