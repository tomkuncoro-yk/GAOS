#!/bin/bash
cd "$(dirname "$0")"

echo "================================================"
echo " Building standalone macOS app"
echo "================================================"
echo

if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 was not found."
    echo "Install Python 3.10+ from https://www.python.org/downloads/ (or 'brew install python3') and run this again."
    read -p "Press Enter to close this window..."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment \".venv\" ..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing/updating dependencies ..."
pip install --upgrade pip
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "[ERROR] Dependency installation failed, see the messages above."
    read -p "Press Enter to close this window..."
    exit 1
fi

echo
echo "Running PyInstaller (this can take a few minutes) ..."
python3 build.py
if [ $? -ne 0 ]; then
    echo "[ERROR] Build failed, see the messages above."
    read -p "Press Enter to close this window..."
    exit 1
fi

echo
echo "================================================"
echo " Done!"
echo "================================================"
echo "Your installer is at: dist/SearchEnginesAIOverviewer.dmg"
echo "Double-click it, then drag the app onto the Applications"
echo "shortcut inside -- same as any normal downloaded Mac app."
echo "(Firefox still needs to be installed separately on whichever Mac"
echo " runs it -- Selenium downloads the matching geckodriver by itself"
echo " the first time the app runs.)"
echo
echo "First launch: macOS will likely block the app as from an"
echo "unidentified developer (it isn't code-signed/notarized)."
echo "Right-click it, choose Open, then confirm -- only needed once."
echo
read -p "Press Enter to close this window..."
