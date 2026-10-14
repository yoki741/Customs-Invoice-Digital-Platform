@echo off
chcp 65001 >nul
title 构建前端 Univer 组件
cd /d "%~dp0webapp\static_src"

if not exist "node_modules" (
  echo 安装前端依赖（仅首次）...
  call npm install
  if errorlevel 1 goto :fail
)

echo 打包 Univer 网格组件...
call npm run build
if errorlevel 1 goto :fail

echo 完成：webapp\static\vendor\univer.js / univer.css
pause
exit /b 0

:fail
echo 构建失败，请确认已安装 Node.js
pause
exit /b 1
