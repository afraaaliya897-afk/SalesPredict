# Sales Intelligence Platform Startup Script
$scriptPath = $PSScriptRoot

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  Sales Intelligence Platform" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "`n  Starting server..." -ForegroundColor Yellow
Write-Host "  Frontend: http://localhost:8000" -ForegroundColor Green
Write-Host "  API:      http://localhost:8000/api" -ForegroundColor Green
Write-Host "`n  Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host "============================================================`n" -ForegroundColor Cyan

Set-Location $scriptPath
python backend/main.py
