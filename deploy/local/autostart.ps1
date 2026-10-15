# 装开机自启（登录时自动拉起本机部署）
#
# 用法：powershell -ExecutionPolicy Bypass -File deploy\local\autostart.ps1
# 卸载：powershell -ExecutionPolicy Bypass -File deploy\local\autostart.ps1 -Remove
#
# ⚠️ 实测澄清：README 早先写的「无需管理员也能建当前用户的任务」**是错的**。
#    裸调 `Register-ScheduledTask`（不指定 -Principal）实测报 **Access is denied.**
#    （本机非管理员）。可行的是**显式指定 principal**：
#        -LogonType Interactive -RunLevel Limited
#    这样无需提权即可注册（实测注册成功、触发后 LastTaskResult=0、两个服务被拉起）。
#    本脚本就是按这个可用配方写的。

param(
  [switch]$Remove,
  [string]$TaskName = "chatbot-platform",
  [string]$StartScript = ""
)

$ErrorActionPreference = "Continue"
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $StartScript) { $StartScript = Join-Path $scriptDir "start.ps1" }

if ($Remove) {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Host "[autostart] 任务 '$TaskName' 不存在，无需卸载"; exit 0 }
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "[autostart] 卸载失败（可能需要管理员）" -ForegroundColor Red; exit 1
  }
  Write-Host "[autostart] 已卸载任务 '$TaskName'" -ForegroundColor Green
  Write-Host "            （已起的服务不会自动停，需要的话跑 stop.ps1）"
  exit 0
}

if (-not (Test-Path $StartScript)) {
  Write-Host "[autostart] 找不到 start.ps1: $StartScript" -ForegroundColor Red; exit 1
}

$me = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "[autostart] 当前用户: $me"

# -WindowStyle Hidden：登录时不要弹出一个黑窗口
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me

# 关键：显式 principal（Interactive + Limited）—— 这是非管理员能注册成功的原因
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

# 登录后别立刻跑：给网络/桌面环境几秒（Docker Desktop 那条路尤其需要）
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10))

try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
} catch {
  Write-Host "[autostart] 注册失败: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "            若是 Access is denied，请用**管理员** PowerShell 重跑本脚本。" -ForegroundColor Yellow
  exit 1
}

Write-Host "[autostart] 注册成功：$TaskName" -ForegroundColor Green
Write-Host "            触发条件：$me 登录时"
Write-Host "            执行：$StartScript"
Write-Host ""
Write-Host "  立即试跑一次（不等下次登录）：" -ForegroundColor Cyan
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host "    Get-ScheduledTaskInfo -TaskName $TaskName    # LastTaskResult 0 = 成功"
Write-Host ""
Write-Host "  卸载：powershell -ExecutionPolicy Bypass -File deploy\local\autostart.ps1 -Remove" -ForegroundColor Cyan
