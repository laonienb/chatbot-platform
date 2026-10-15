# 本机部署验收 —— 一条命令确认「服务真的可用」（不是只看端口在听）
#
# 用法：powershell -ExecutionPolicy Bypass -File deploy\local\verify.ps1
#
# 检查 7 组共 20+ 项：端口 -> 健康检查 -> 局域网可访问 -> **公网 Host 兼容**（隧道前提）
# -> **真实模型端到端**（注册/人设/会话/SSE 流式/[DONE]/真实回复）-> 开机自启任务 -> 交付物是否入库。
# 任一项失败 exit 1，可直接用于部署完了没的判定。
#
# 参数：-Base 默认 http://127.0.0.1:3000；也可指向隧道地址验证公网链路。
$ErrorActionPreference = "Continue"
$base = "http://127.0.0.1:3000"
$pass = 0; $fail = 0
function Check($name, $ok, $detail) {
  if ($ok) { Write-Host ("  [OK]   {0}  {1}" -f $name, $detail) -ForegroundColor Green; $script:pass++ }
  else { Write-Host ("  [FAIL] {0}  {1}" -f $name, $detail) -ForegroundColor Red; $script:fail++ }
}

Write-Host "=== 1. 服务与端口 ===" -ForegroundColor Cyan
foreach ($p in @(3000, 8000)) {
  $b = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  Check "port $p" ([bool]$b) $(if ($b) { "listening" } else { "not listening" })
}

Write-Host ""
Write-Host "=== 2. 健康检查 ===" -ForegroundColor Cyan
try {
  $h = (Invoke-WebRequest "$base/healthz" -UseBasicParsing -TimeoutSec 8).Content
  Check "healthz" ($h -match "ok") $h
} catch { Check "healthz" $false $_.Exception.Message }

Write-Host ""
Write-Host "=== 3. 局域网地址（别人现在就能用）===" -ForegroundColor Cyan
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress
try {
  $c = (Invoke-WebRequest "http://${ip}:3000" -UseBasicParsing -TimeoutSec 8).StatusCode
  Check "LAN http://${ip}:3000" ($c -eq 200) "status=$c"
} catch { Check "LAN" $false $_.Exception.Message }

Write-Host ""
Write-Host "=== 4. 公网 Host 兼容（隧道场景）===" -ForegroundColor Cyan
$hostCode = curl.exe -s -o NUL -w "%{http_code}" -H "Host: demo.natappfree.cc" "$base/login"
Check "Host=public /login" ($hostCode -eq "200") "status=$hostCode"
$assetCode = curl.exe -s -o NUL -w "%{http_code}" -H "Host: demo.natappfree.cc" "$base/_next/static/chunks/webpack-01a8b225504b0466.js"
Check "Host=public static" ($assetCode -eq "200") "status=$assetCode"

Write-Host ""
Write-Host "=== 5. 真实模型端到端（注册->人设->会话->流式）===" -ForegroundColor Cyan
$em = "acc-$([guid]::NewGuid().ToString('N').Substring(0,8))@local.dev"
$reg = Invoke-RestMethod -Uri "$base/api/v1/auth/register" -Method POST -ContentType "application/json" -Body (@{email=$em; password="password123"} | ConvertTo-Json)
Check "register" ([bool]$reg.access_token) $em
$H = @{ Authorization = "Bearer $($reg.access_token)" }
$sl = "acc-" + [guid]::NewGuid().ToString('N').Substring(0,6)
$body = @{ name = "验收"; slug = $sl; system_prompt = "只输出被要求的词，不要加任何其他内容。" } | ConvertTo-Json
$p = Invoke-RestMethod -Uri "$base/api/v1/personas" -Method POST -Headers $H -ContentType "application/json" -Body $body
Check "persona" ([bool]$p.id) $p.slug
$c = Invoke-RestMethod -Uri "$base/api/v1/conversations" -Method POST -Headers $H -ContentType "application/json" -Body (@{persona_id=$p.id} | ConvertTo-Json)
Check "conversation" ([bool]$c.id) $c.id

$payload = @{ content = "只输出两个字：完成"; stream = $true } | ConvertTo-Json -Compress
# ⚠️ 必须把 payload 写进文件再用 `-d @file`：直接把含中文的 $payload 交给 curl.exe -d 时，
# PowerShell 会按控制台代码页（本机 GBK）传参，服务端收到坏 JSON 并报
# json_invalid / JSON decode error —— 看着像 SSE 坏了，实际是**测试脚本的编码坑**
# （本仓库 AGENTS.md 也记过同一个 curl + 中文载荷的坑）。踩过一次，别再踩。
$pf = Join-Path $env:TEMP ("chat-payload-" + [guid]::NewGuid().ToString('N') + ".json")
[System.IO.File]::WriteAllText($pf, $payload, (New-Object System.Text.UTF8Encoding($false)))
$raw = curl.exe -sN --max-time 90 -X POST "$base/api/v1/conversations/$($c.id)/messages" -H "Authorization: Bearer $($reg.access_token)" -H "Content-Type: application/json" -d "@$pf"
Remove-Item $pf -Force -ErrorAction SilentlyContinue
$lines = $raw -split "`n"
$chunks = ($lines | Where-Object { $_ -match '^data: \{' }).Count
$done = ($lines | Where-Object { $_ -match '\[DONE\]' }).Count
$text = ($lines | Where-Object { $_ -match '^data: \{' } | ForEach-Object { ($_ -replace '^data: ', '') | ConvertFrom-Json } | ForEach-Object { $_.choices[0].delta.content }) -join ""
Check "SSE chunks" ($chunks -ge 1) "chunks=$chunks"
Check "SSE [DONE]" ($done -eq 1) "done=$done"
Check "real reply" ($text.Trim().Length -gt 0) "reply=[$text]"

Write-Host ""
Write-Host "=== 6. 开机自启任务 ===" -ForegroundColor Cyan
$t = Get-ScheduledTask -TaskName "chatbot-platform" -ErrorAction SilentlyContinue
if ($t) {
  $i = Get-ScheduledTaskInfo -TaskName "chatbot-platform"
  Check "task registered" $true "state=$($t.State) lastResult=$($i.LastTaskResult)"
} else { Check "task registered" $false "not found" }

Write-Host ""
Write-Host "=== 7. 交付物齐全 ===" -ForegroundColor Cyan
foreach ($f in @("deploy/README.md", "deploy/docker-compose.prod.yml", "deploy/.env.prod.example",
                 "deploy/local/start.ps1", "deploy/local/stop.ps1", "deploy/local/status.ps1",
                 "deploy/local/autostart.ps1", "deploy/local/check-docker.ps1",
                 "web/Dockerfile", "web/.dockerignore")) {
  $tracked = git ls-files $f
  Check $f ([bool]$tracked) $(if ($tracked) { "tracked" } else { "NOT tracked" })
}

Write-Host ""
if ($fail -eq 0) {
  Write-Host "全部通过：$pass 项" -ForegroundColor Green
} else {
  Write-Host "通过 $pass 项，失败 $fail 项" -ForegroundColor Red
  exit 1
}
