#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Double-click install_mac.command first."
    read -p "Press Enter to close this window..."
    exit 1
fi

source .venv/bin/activate
python3 main.py
status=$?
if [ $status -ne 0 ]; then
    echo
    echo "The app exited with an error, see the messages above."
    read -p "Press Enter to close this window..."
fi
