@echo off
REM One-click launcher for the 6 portfolio demos (pure ASCII on purpose).
REM Do NOT add chcp / Chinese text here: this file must stay ASCII-safe.
setlocal
set BASE=%~dp0
set PY=%BASE%.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo ============================================================
echo   Portfolio demos - starting all 6 services
echo ============================================================
"%PY%" "%BASE%start_all.py"
echo.
echo   Book lending      http://127.0.0.1:8001
echo   Online mall       http://127.0.0.1:8002
echo   Inventory+logis   http://127.0.0.1:8003
echo   Ledger + BI       http://127.0.0.1:8004
echo   Multi-tenant SaaS http://127.0.0.1:8005
echo   AI recommender    http://127.0.0.1:8006
echo.
echo   Accounts: admin/admin123  ops/ops123  demo/demo123
echo ============================================================
pause
