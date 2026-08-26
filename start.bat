@echo off
cls
echo.
echo ============================================================
echo   Sales Intelligence Platform
echo ============================================================
echo.
echo   Starting server...
echo   Frontend: http://localhost:8000
echo   API:      http://localhost:8000/api
echo.
echo   Press Ctrl+C to stop
echo ============================================================
echo.

cd "%~dp0"
python backend\main.py

pause
