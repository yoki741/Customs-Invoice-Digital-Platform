# 报关发票平台服务守护进程：崩溃自动重启
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File service_watchdog.ps1
$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = 'D:\Python311\python.exe'
$Port = 8000
$LogFile = Join-Path $Root 'logs\watchdog.log'

function Write-Log([string]$Msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $Msg"
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

Write-Log "===== 守护进程启动 ====="

while ($true) {
    # 端口已被占用则等待（可能是手动启动的服务或残留进程）
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Log "端口 $Port 已被占用，等待释放..."
        Start-Sleep -Seconds 10
        continue
    }

    $proc = Start-Process -FilePath $Python `
        -ArgumentList '-m', 'uvicorn', 'webapp.main:app', '--host', '0.0.0.0', '--port', "$Port" `
        -WorkingDirectory $Root -PassThru -WindowStyle Hidden
    Write-Log "服务已启动 PID=$($proc.Id)"

    $proc.WaitForExit()
    $code = $proc.ExitCode
    Write-Log "服务退出（退出码 $code），5 秒后自动重启"
    Start-Sleep -Seconds 5
}
