#!/bin/sh
# FEK Watch - local interface (macOS/Linux). Double-click or run ./start-ui.command
cd "$(dirname "$0")"
python3 -c "import pymupdf, requests, fontTools" 2>/dev/null || python3 -m pip install -q --disable-pip-version-check --no-warn-script-location -r requirements.txt
python3 -m fekwatch sync
python3 -m fekwatch ui
