@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "API_HOST=0.0.0.0"
set "API_PORT=8000"
set "WEB_HOST=0.0.0.0"
set "WEB_PORT=3000"
set "UVICORN=%~dp0.venv\Scripts\uvicorn.exe"
set "FFMPEG_BIN="

if not exist "%UVICORN%" (
  echo [ERROR] Virtualenv not found: .venv\Scripts\uvicorn.exe
  echo Create it first, then install requirements. See README.md.
  pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found in PATH. Install Node.js 20+ first.
  pause
  exit /b 1
)

if not exist "%~dp0apps\web\node_modules\" (
  echo [ERROR] Frontend dependencies missing: apps\web\node_modules
  echo Run: npm --prefix apps\web install
  pause
  exit /b 1
)

if not exist "%~dp0.env" (
  echo [WARN] .env not found. Copy env.txt.example to .env and configure it first.
) else (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%~dp0.env") do (
    if /I "%%~A"=="FFMPEG_PATH" set "FFMPEG_EXE=%%~B"
  )
)

if defined FFMPEG_EXE (
  for %%I in ("!FFMPEG_EXE!") do set "FFMPEG_BIN=%%~dpI"
)

if defined FFMPEG_BIN (
  if exist "!FFMPEG_BIN!ffmpeg.exe" (
    set "PATH=!FFMPEG_BIN!;!PATH!"
    echo Using FFmpeg from: !FFMPEG_BIN!
  ) else (
    echo [WARN] FFMPEG_PATH bin dir not found: !FFMPEG_BIN!
    echo TorchCodec needs the FFmpeg full-shared DLL directory on PATH.
  )
) else (
  where ffmpeg >nul 2>nul
  if errorlevel 1 (
    echo [WARN] FFMPEG_PATH is not set and ffmpeg was not found in PATH.
    echo Audio separation may fail until FFmpeg full-shared is configured.
  )
)

echo Starting YouDub WebUI...
echo   API: http://localhost:%API_PORT%
echo   Web: http://localhost:%WEB_PORT%
echo.

rem Only watch backend/ for reload. Watching the whole repo restarts the API when
rem yt-dlp writes into workfolder/, which aborts downloads in a restart loop.
start "YouDub API" cmd /k ""%UVICORN%" backend.app.main:app --reload --reload-dir backend --host %API_HOST% --port %API_PORT%"
start "YouDub Web" cmd /k "npm --prefix apps\web run dev -- --hostname %WEB_HOST% --port %WEB_PORT%"

timeout /t 2 /nobreak >nul
start "" "http://localhost:%WEB_PORT%"

echo Services launched in separate windows.
echo Close those windows to stop the services.
endlocal
