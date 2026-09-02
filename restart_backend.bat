@echo off
REM Clean restart - Stop all Python processes and start fresh

echo.
echo ============================================================
echo   Stopping all servers...
echo ============================================================

REM Stop all Python processes
taskkill /F /IM python.exe /T 2>nul

echo Waiting for processes to close...
timeout /t 3 /nobreak >nul

echo.
echo ============================================================
echo   Starting Sales Intelligence Backend
echo ============================================================
echo.
echo   Opening: http://localhost:8000
echo.

REM Start the main backend
python backend/main.py
