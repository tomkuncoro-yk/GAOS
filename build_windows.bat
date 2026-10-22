@echo off
setlocal
cd /d "%~dp0"

echo ================================================
echo  Building standalone Windows app
echo ================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment ".venv" ...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing/updating dependencies ...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed, see the messages above.
    pause
    exit /b 1
)

echo.
echo Running PyInstaller and building the installer (this can take a
echo few minutes) ...
python build.py
if errorlevel 1 (
    echo [ERROR] Build failed, see the messages above.
    pause
    exit /b 1
)

echo.
echo ================================================
echo  Done!
echo ================================================
echo Your installer is at: dist\SearchEnginesAIOverviewerSetup.exe
echo Share that one file. Running it walks through a normal setup
echo wizard -- installs the app, adds a Start Menu shortcut, and
echo registers a clean uninstaller in "Add or remove programs".
echo (Firefox still needs to be installed separately on whichever
echo  machine runs it -- Selenium downloads the matching geckodriver
echo  by itself the first time the app runs.)
echo.
pause
