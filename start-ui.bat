@echo off
REM FEK Watch - local interface. Double-click to start; close this window to stop.
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
python -m fekwatch ui
pause
