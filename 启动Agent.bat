@echo off
chcp 65001 >nul
title AgentFlow - 多 Agent 协同办公数据处理平台
echo ============================================
echo   AgentFlow
echo   Planner / Retrieval / Executor / Verifier / Reporter
echo   正在启动 Web 界面...
echo   请勿关闭本窗口（关闭即停止服务）
echo   启动后浏览器会自动打开 http://localhost:8000
echo ============================================
start "" http://localhost:8000
python -X utf8 webui.py
pause
