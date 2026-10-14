@echo off
chcp 65001 >nul
title 報關發票(柬埔寨)數字化管理平台
cd /d "%~dp0"

if not exist "webapp\static\vendor\univer.js" (
  echo [提示] 首次运行需要构建前端 Univer 组件...
  call build_frontend.bat
)

echo 启动平台：http://localhost:8000  （默认管理员 admin / admin123，请登录后立即修改）
"D:\Python311\python.exe" -m uvicorn webapp.main:app --host 0.0.0.0 --port 8000
pause
