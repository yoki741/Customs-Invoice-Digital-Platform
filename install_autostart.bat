@echo off
chcp 65001 >nul
rem 安装开机自启任务：登录 Windows 后自动运行服务守护进程（含崩溃自动重启）
rem 需要以当前用户身份运行一次即可；如需移除：schtasks /Delete /TN "InvoiceWebWatchdog" /F

schtasks /Create /TN "InvoiceWebWatchdog" ^
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%~dp0service_watchdog.ps1\"" ^
  /SC ONLOGON /F

if %errorlevel%==0 (
  echo [OK] 已创建开机自启任务 InvoiceWebWatchdog
  schtasks /Run /TN "InvoiceWebWatchdog"
  echo [OK] 已立即启动守护进程（服务地址 http://localhost:8000）
) else (
  echo [失败] 创建任务失败，请以管理员身份运行此脚本
)
pause
