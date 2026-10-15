# 停掉本机部署（由 deploy\local\start.ps1 起的两个进程）
#
# 用法：powershell -ExecutionPolicy Bypass -File deploy\local\stop.ps1

$ErrorActionPreference = "Continue"
$logDir = Join-Path $PSScriptRoot "logs"

foreach ($name in @("api", "web")) {
  $pidFile = Join-Path $logDir "$name.pid"
  if (-not (Test-Path $pidFile)) { Write-Host "[stop] $name 没有 pid 文件，跳过"; continue }
  $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if (-not $procId) { Write-Host "[stop] $name pid 文件是空的，跳过"; continue }

  # 只杀我们记录的 pid，且先确认它确实还在——避免误杀别的进程
  $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
  if (-not $p) {
    Write-Host "[stop] $name (PID $procId) 已不在运行"
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    continue
  }
  try {
    Stop-Process -Id $procId -Force -ErrorAction Stop
    Write-Host "[stop] $name (PID $procId) 已停止"
  } catch {
    Write-Host "[stop] $name (PID $procId) 停止失败：$($_.Exception.Message)"
  }
  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# uvicorn --reload 会派生子进程；这里兜底清掉仍监听目标端口的孤儿（仅本地端口，范围很小）
foreach ($port in @(3000, 8000)) {
  $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  foreach ($b in $busy) {
    $op = Get-Process -Id $b.OwningProcess -ErrorAction SilentlyContinue
    if ($op -and $op.ProcessName -in @("python", "node")) {
      Write-Host "[stop] 端口 $port 仍被 $($op.ProcessName) (PID $($op.Id)) 占用，清掉"
      Stop-Process -Id $op.Id -Force -ErrorAction SilentlyContinue
    }
  }
}
Write-Host "[stop] 完成"
