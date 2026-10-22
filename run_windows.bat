@echo off
cd /d "%~dp0"

if not exist .venv (
    echo Virtual environment not found. Run install_windows.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python main.py
if errorlevel 1 (
    echo.
    echo The app exited with an error, see the messages above.
    pause
)
