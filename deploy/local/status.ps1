# 看本机部署状态（不用 Docker 时）
#
# 用法：powershell -ExecutionPolicy Bypass -File deploy\local\status.ps1

$logDir = Join-Path $PSScriptRoot "logs"
Write-Host "=== 进程 ===" -ForegroundColor Cyan
foreach ($name in @("api", "web")) {
  $pidFile = Join-Path $logDir "$name.pid"
  if (-not (Test-Path $pidFile)) { Write-Host ("  {0,-4} 未启动（无 pid 文件）" -f $name); continue }
  $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
  if ($p) {
    Write-Host ("  {0,-4} 运行中  PID={1}  启动于 {2}" -f $name, $p.Id, $p.StartTime) -ForegroundColor Green
  } else {
    Write-Host ("  {0,-4} 已退出（pid 文件残留，跑 stop.ps1 清理）" -f $name) -ForegroundColor Yellow
  }
}

Write-Host ""
Write-Host "=== 端口 ===" -ForegroundColor Cyan
foreach ($port in @(3000, 8000)) {
  $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($busy) {
    $op = Get-Process -Id $busy.OwningProcess -ErrorAction SilentlyContinue
    Write-Host ("  {0}: 监听中 ({1}, PID {2})" -f $port, $op.ProcessName, $op.Id)
  } else {
    Write-Host ("  {0}: 空闲" -f $port)
  }
}

Write-Host ""
Write-Host "=== 健康检查 ===" -ForegroundColor Cyan
try {
  $h = Invoke-WebRequest "http://127.0.0.1:3000/healthz" -UseBasicParsing -TimeoutSec 5
  Write-Host ("  经 web 代理 -> $($h.Content)") -ForegroundColor Green
} catch { Write-Host "  web 不可达：$($_.Exception.Message)" -ForegroundColor Red }

Write-Host ""
Write-Host "=== 局域网地址 ===" -ForegroundColor Cyan
$ips = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }).IPAddress
foreach ($ip in $ips) { Write-Host "  http://${ip}:3000" }

Write-Host ""
Write-Host "=== 日志尾部（各 5 行）===" -ForegroundColor Cyan
foreach ($name in @("api", "web", "api.err", "web.err")) {
  $f = Join-Path $logDir "$name.log"
  Write-Host "--- $name ---"
  # Next/uvicorn 的日志是 UTF-8；Windows PowerShell 默认按 GBK 读会把 ✓ 之类读成乱码，
  # 显式指定编码读，否则日志看着像坏了（实测 web.log 的 "✓ Starting..." 显示成 "鉁?Starting..."）。
  if (Test-Path $f) {
    $lines = Get-Content $f -Tail 5 -Encoding UTF8 -ErrorAction SilentlyContinue
    if ($lines) { $lines | ForEach-Object { "  $_" } } else { Write-Host "  （空）" }
  } else { Write-Host "  （无日志）" }
}
