@echo off
title BambooHR clone (Quota roster) - http://127.0.0.1:8080
set "ROOT=%~dp0"
cd /d "%ROOT%app\backend\backend-generation-v1"
set "QUOTA_ROSTER=C:\Users\aadit\Downloads\Quota\out\hr_roster.json"
set "CLONE_ADMIN_TOKEN=dev"
set "PORT=8080"
set "HOST=127.0.0.1"
set "STATIC_DIR=%ROOT%app\frontend\react-clone\dist"
set "DATABASE_URL=file:./.local-data/clone.db"
set "BUN=bun"
where bun >NUL 2>NUL || set "BUN=%USERPROFILE%\.bun\bin\bun.exe"
if not exist "%QUOTA_ROSTER%" echo Roster not found: %QUOTA_ROSTER% & pause & exit /b 1
if not exist node_modules call "%BUN%" install
if not exist generated\prisma call "%BUN%" x prisma generate --schema prisma/schema.prisma
if not exist "%STATIC_DIR%\index.html" (pushd "%ROOT%app\frontend\react-clone" & call "%BUN%" install & call "%BUN%" run build & popd)
echo Starting BambooHR clone with Quota roster...
"%BUN%" server.mjs
echo.
echo The clone stopped. Press any key to close.
pause >nul
