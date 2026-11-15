@echo off
rem MemeDesk on Windows: runs the desk and restarts it automatically if it crashes.
rem Start: double-click this file, or in cmd:  scripts\run_windows.bat
rem Stop:  Ctrl+C, then answer Y. Closing this window also stops the desk.
cd /d "%~dp0.."
:loop
python -m memedesk run
if %errorlevel%==0 goto end
echo %date% %time% desk stopped with an error, restarting in 15 s... >> data\crashes.log
timeout /t 15 /nobreak >nul
goto loop
:end
