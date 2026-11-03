@echo off
REM FEK Watch - local interface. Double-click to start; close this window to stop.
cd /d "%~dp0"
python -c "import pymupdf, requests" 2>nul || python -m pip install -q --disable-pip-version-check --no-warn-script-location -r requirements.txt
python -m fekwatch ui
pause
