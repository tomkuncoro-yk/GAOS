#!/bin/bash
cd "$(dirname "$0")"

echo "================================================"
echo " Search Engines AI Overviewer - macOS Setup"
echo "================================================"
echo

if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 was not found."
    echo "Install Python 3.10+ from https://www.python.org/downloads/"
    echo "(or 'brew install python3' if you use Homebrew), then run this script again."
    read -p "Press Enter to close this window..."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment \".venv\" ..."
    python3 -m venv .venv
else
    echo "Virtual environment \".venv\" already exists, reusing it."
fi

source .venv/bin/activate

echo
echo "Installing/updating dependencies from requirements.txt ..."
pip install --upgrade pip
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "[ERROR] Dependency installation failed, see the messages above."
    read -p "Press Enter to close this window..."
    exit 1
fi

echo
echo "================================================"
echo " Setup complete!"
echo "================================================"
echo
echo "One more thing this script can't do for you:"
echo "  - Make sure Mozilla Firefox is installed."
echo "    (Selenium downloads the matching geckodriver automatically the"
echo "     first time the app runs -- nothing else to install for that.)"
echo
echo "To run the app from now on, double-click run_mac.command"
echo "(If macOS refuses to open it the first time: right-click it,"
echo " choose Open, then confirm -- only needed once.)"
echo
read -p "Press Enter to close this window..."
