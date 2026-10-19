@echo off
setlocal
cd /d "%~dp0"

echo ================================================
echo  Google AI Overview Scraper - Windows Setup
echo ================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add python.exe to PATH" during install, then run this script again.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment ".venv" ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment ".venv" already exists, reusing it.
)

call .venv\Scripts\activate.bat

echo.
echo Installing/updating dependencies from requirements.txt ...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed, see the messages above.
    pause
    exit /b 1
)

echo.
echo ================================================
echo  Setup complete!
echo ================================================
echo.
echo One more thing this script can't do for you:
echo   - Make sure Mozilla Firefox is installed.
echo     (Selenium downloads the matching 'geckodriver' automatically the
echo      first time the app runs -- nothing else to install for that.)
echo.
echo To run the app from now on, double-click run_windows.bat
echo.
pause
