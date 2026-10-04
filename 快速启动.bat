@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AgentFlow 一键启动
echo ============================================================
echo   AgentFlow - 多 Agent 协同办公数据处理平台
echo   Planner / Retrieval / Executor / Verifier / Reporter
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [x] 未找到 python，请先安装 Python 3.10+ 并加入 PATH
  pause
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [!] 未找到 .env，已生成模板。请在记事本里填入 DEEPSEEK_API_KEY，保存后重新运行。
  start "" notepad ".env"
  pause
  exit /b 1
)

findstr /c:"你的密钥" ".env" >nul 2>nul
if not errorlevel 1 (
  echo [!] .env 里还是占位符，请填入真实的 DEEPSEEK_API_KEY。
  start "" notepad ".env"
  pause
  exit /b 1
)

echo [检查依赖] ...
python -c "import flask, openpyxl, dotenv" >nul 2>nul
if errorlevel 1 (
  echo     首次运行：安装依赖（之后启动会跳过这步）...
  python -m pip install -r requirements.txt -q --disable-pip-version-check
)

if not exist "data\对账\银行流水.xlsx" (
  echo [生成演示数据] ...
  python -X utf8 make_demo_data.py
)

echo ============================================================
echo   启动 Web 面板： http://127.0.0.1:8000
echo   浏览器会自动打开；关闭本窗口即停止服务
echo ============================================================
echo.
start "" http://127.0.0.1:8000
python -X utf8 webui.py
pause
