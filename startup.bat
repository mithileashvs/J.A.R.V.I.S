@echo off
title JARVIS Launcher
cd /d %~dp0

echo ==========================================
echo          JARVIS STARTUP
echo ==========================================
echo.

REM ------------------------------------------
REM Start local LiveKit server
REM ------------------------------------------

echo Starting local LiveKit...

if not exist "C:\livekit\livekit-server.exe" (
    echo ERROR: LiveKit server not found at:
    echo C:\livekit\livekit-server.exe
    echo.
    pause
    exit /b 1
)

tasklist /FI "IMAGENAME eq livekit-server.exe" 2>NUL | find /I "livekit-server.exe" >NUL

if %ERRORLEVEL% EQU 0 (
    echo LiveKit is already running.
) else (
    start "JARVIS Local LiveKit" cmd /k "cd /d C:\livekit && livekit-server.exe --dev"
    echo LiveKit started.
)

timeout /t 3 /nobreak >nul

REM ------------------------------------------
REM Start JARVIS backend
REM ------------------------------------------

echo Starting JARVIS backend...
start "JARVIS Backend" cmd /k "venv\Scripts\activate.bat && python main.py"

timeout /t 3 /nobreak >nul

REM ------------------------------------------
REM Start JARVIS frontend
REM ------------------------------------------

echo Starting JARVIS frontend...
start "JARVIS Frontend" cmd /k "cd frontend && python -m http.server 3000"

timeout /t 2 /nobreak >nul

REM ------------------------------------------
REM Open browser
REM ------------------------------------------

start "" "http://localhost:3000"

echo.
echo ==========================================
echo        JARVIS STARTUP COMPLETE
echo ==========================================
echo.
exit