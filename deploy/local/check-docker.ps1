# Docker 路自检 —— 装好 Docker Desktop 后**跑这一个脚本**就能知道生产 compose 能不能用
#
# 用法（在 deploy/ 目录）：
#   powershell -ExecutionPolicy Bypass -File deploy\local\check-docker.ps1
#
# 为什么需要它：写这份 compose 的机器上**没有装 docker**，所以它只做过
# YAML 解析与结构断言，**没经过 `docker compose config` 校验**——那证明字段自洽，
# 不证明 docker 接受这个文件。本脚本把「接受不接受」这件事在**你的机器上**补上，
# 并在真正 up 之前把所有能静态查的都查一遍。

$ErrorActionPreference = "Continue"

function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }
function Ok($m) { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Bad($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:failed++ }
$script:failed = 0

Say "=== 0. docker 是否存在 ===" "Cyan"
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
  Bad "找不到 docker 命令。先装 Docker Desktop 并确保它已启动（托盘图标变绿）。"
  Say "       安装：https://www.docker.com/products/docker-desktop/"
  Say "       前置：管理员 PowerShell 跑 'wsl --install' 后重启。"
  exit 1
}
Ok "docker = $($docker.Source)"
$dv = (docker --version 2>&1) -join ' '
Ok $dv
$cv = (docker compose version 2>&1) -join ' '
Ok $cv

Say ""
Say "=== 1. docker 守护进程是否在跑 ===" "Cyan"
docker info --format "{{.ServerVersion}}" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Bad "docker 守护进程没响应。启动 Docker Desktop 并等它就绪后重试。"
  exit 1
}
Ok "守护进程在跑"

$deployDir = Split-Path -Parent $PSScriptRoot      # deploy/
$composeFile = Join-Path $deployDir "docker-compose.prod.yml"
$envFile = Join-Path $deployDir ".env.prod"

Say ""
Say "=== 2. 文件是否齐 ===" "Cyan"
foreach ($f in @($composeFile, (Join-Path $deployDir ".env.prod.example"))) {
  if (Test-Path $f) { Ok (Split-Path $f -Leaf) } else { Bad "缺失: $f" }
}
if (-not (Test-Path $envFile)) {
  Say "  [!!]   deploy\.env.prod 不存在 —— 先执行：" "Yellow"
  Say "         Copy-Item deploy\.env.prod.example deploy\.env.prod" "Yellow"
  Say "         然后填 POSTGRES_PASSWORD / SECRET_KEY / LLM_BACKEND（三个必填）" "Yellow"
  exit 1
}
Ok ".env.prod 存在"

Say ""
Say "=== 3. 必填项是否真的填了（空值会让 compose 拒绝启动）===" "Cyan"
$envText = Get-Content $envFile -Raw -Encoding UTF8
foreach ($key in @("POSTGRES_PASSWORD", "SECRET_KEY", "LLM_BACKEND")) {
  $m = [regex]::Match($envText, "(?m)^$key=(.*)$")
  if (-not $m.Success) { Bad "$key 没有出现在 .env.prod 里" ; continue }
  $val = $m.Groups[1].Value.Trim()
  if ([string]::IsNullOrWhiteSpace($val)) { Bad "$key 是空的（必须填）" }
  else { Ok "$key 已填（长度 $($val.Length)）" }
}
# 高危默认值检查
if ($envText -match "SECRET_KEY=dev-insecure-secret-change-me") { Bad "SECRET_KEY 还是代码里的 dev 默认值，公网部署等于 JWT 可被伪造" }
if ($envText -match "POSTGRES_PASSWORD=chatbot-dev-password") { Bad "POSTGRES_PASSWORD 还是开发默认值" }
$llm = [regex]::Match($envText, "(?m)^LLM_BACKEND=(.*)$").Groups[1].Value.Trim()
if ($llm -eq "mock") { Bad "LLM_BACKEND=mock：APP_ENV=prod 下会被守卫拒绝启动（红线9）" }
elseif ($llm) { Ok "LLM_BACKEND=$llm" }
$grant = [regex]::Match($envText, "(?m)^SIGNUP_GRANT_CREDITS=(.*)$").Groups[1].Value.Trim()
if ($grant -and $grant -ne "0") {
  Say "  [!!]   SIGNUP_GRANT_CREDITS=$grant —— 公网部署下 /register 公开无审核，" "Yellow"
  Say "         送额度等于让人无限注册刷额度。确认这是你要的。" "Yellow"
} else { Ok "SIGNUP_GRANT_CREDITS=$grant（0 = 不送）" }

Say ""
Say "=== 4. compose 语法与插值（关键：这一步是写 compose 的机器上没做的）===" "Cyan"
$cfg = docker compose --env-file $envFile -f $composeFile config 2>&1
if ($LASTEXITCODE -ne 0) {
  Bad "docker compose config 失败："
  $cfg | Select-Object -First 20 | ForEach-Object { Say "         $_" "Red" }
  exit 1
}
Ok "docker compose config 通过（docker 接受这个文件）"

# 确认插值真的生效（没有残留的 ${...}）
$leftover = $cfg | Select-String -Pattern '\$\{'
if ($leftover) {
  Bad "有变量没被替换（.env.prod 里可能漏了）："
  $leftover | Select-Object -First 5 | ForEach-Object { Say "         $_" "Red" }
} else { Ok "所有 `${...} 都已插值（无残留）" }

Say ""
Say "=== 5. 安全检查：公网暴露面 ===" "Cyan"
$svcList = (docker compose --env-file $envFile -f $composeFile config --services 2>&1)
Ok ("服务: " + ($svcList -join ", "))
# 用 config 的 JSON 形式精确判断哪个服务发布了端口
$json = docker compose --env-file $envFile -f $composeFile config --format json 2>&1 | Out-String
try {
  $obj = $json | ConvertFrom-Json
  $exposed = @()
  foreach ($p in $obj.services.PSObject.Properties) {
    if ($p.Value.ports -and $p.Value.ports.Count -gt 0) { $exposed += $p.Name }
  }
  if ($exposed.Count -eq 1 -and $exposed[0] -eq "web") {
    Ok "只有 web 发布宿主端口（postgres/redis/server/model-service 都在内网）"
  } else {
    Bad "预期只有 web 对外，实际: $($exposed -join ', ')"
  }
} catch {
  Say "  [!!]   无法解析 config --format json，请人工确认只有 web 有 ports：" "Yellow"
  $cfg | Select-String -Pattern "published|ports" | Select-Object -First 10 | ForEach-Object { Say "         $_" }
}

Say ""
Say "=== 6. 镜像能否构建（只 build，不 up）===" "Cyan"
Say "  这一步会下载基础镜像并装依赖，首次可能几分钟…" "DarkGray"
docker compose --env-file $envFile -f $composeFile build 2>&1 | Select-Object -Last 12 | ForEach-Object { Say "         $_" }
if ($LASTEXITCODE -ne 0) { Bad "build 失败，看上面输出" } else { Ok "三个镜像都构建成功" }

Say ""
if ($script:failed -eq 0) {
  Write-Host "全部通过。" -ForegroundColor Green
  Say ""
  Say "下一步（真正起服务）："
  Say "  docker compose --env-file deploy\.env.prod -f deploy\docker-compose.prod.yml up -d"
  Say ""
  Say "起来后按 deploy\README.md「五、验收清单」逐条验："
  Say "  1) curl.exe http://127.0.0.1:3000/healthz"
  Say "  2) 浏览器打开 http://127.0.0.1:3000 注册并发一条消息"
  Say "  3) 手机 4G 访问穿透地址，确认外网可用"
} else {
  Write-Host "有 $($script:failed) 项没通过，先解决再 up。" -ForegroundColor Red
  exit 1
}
