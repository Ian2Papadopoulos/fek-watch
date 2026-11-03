#!/bin/sh
# FEK Watch - local interface (macOS/Linux). Double-click or run ./start-ui.command
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
python3 -m fekwatch ui
