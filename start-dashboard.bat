@echo off
cd /d "%~dp0"
echo Market dashboard: http://localhost:8000  (close this window to stop)
python server.py
pause
