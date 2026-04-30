@echo off
REM Setup script for Schulportal Hessen API + TUI (Windows)

echo.
echo ╔════════════════════════════════════════════╗
echo ║  Schulportal Hessen API + TUI Setup        ║
echo ╚════════════════════════════════════════════╝
echo.

REM Check if Node.js is installed
node --version >nul 2>&1
if errorlevel 1 (
    echo ✘ Node.js is not installed. Please install it first.
    echo    Download from: https://nodejs.org/
    pause
    exit /b 1
)

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ✘ Python is not installed. Please install it first.
    pause
    exit /b 1
)

echo ✓ Node.js version:
node --version

echo ✓ Python version:
python --version
echo.

REM Setup TUI
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo Setting up TUI (Terminal User Interface)...
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if exist package.json (
    echo Installing Node.js dependencies...
    call npm install
    echo ✓ Node.js dependencies installed
    
    echo.
    echo Building TypeScript...
    call npm run build
    echo ✓ TypeScript built
) else (
    echo ✘ package.json not found
    pause
    exit /b 1
)

echo.
echo ╔════════════════════════════════════════════╗
echo ║  Setup Complete!                          ║
echo ╚════════════════════════════════════════════╝
echo.
echo To get started:
echo.
echo 1. From the project root, start the API server:
echo    python -m uvicorn api:app --reload
echo.
echo 2. In another terminal, run the TUI from this directory:
echo    npm run dev
echo.
echo Press any key to continue...
pause
