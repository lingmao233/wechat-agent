@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 未找到 .venv\Scripts\python.exe，请先创建虚拟环境并安装依赖。
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m wechat_agent desktop --open-browser
pause
