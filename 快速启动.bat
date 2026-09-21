@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AgentFlow 快速启动
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
  echo [!] 未找到 .env，已生成模板。请填入 DEEPSEEK_API_KEY 后重新运行。
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

echo [1/3] 检查依赖...
python -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
  echo [!] 依赖安装未成功（可能离线或已安装）。若稍后启动报缺少模块，请手动执行：
  echo     python -m pip install -r requirements.txt
  echo.
)

echo [2/3] 生成演示数据（对账 / 清洗）...
python -X utf8 make_demo_data.py
if errorlevel 1 (
  echo [!] 演示数据生成失败
  pause
  exit /b 1
)

echo [3/3] 启动 Web 界面： http://127.0.0.1:8000
echo     关闭本窗口即停止服务。
echo.
start "" http://127.0.0.1:8000
python -X utf8 webui.py
pause
