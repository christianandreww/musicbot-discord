@echo off
title Music Bot
rem Runs from whatever folder this file sits in - keep it in the project folder.
cd /d "%~dp0"


echo Waiting for Docker to be ready...
:waitdocker
docker info >nul 2>&1
if errorlevel 1 (
    timeout /t 3 /nobreak >nul
    goto waitdocker
)

echo Starting Lavalink (fresh, so config changes take effect)...
docker compose down >nul 2>&1
docker compose up -d

echo Giving Lavalink a few seconds to boot...
timeout /t 10 /nobreak >nul

echo Starting the bot. Close this window to stop it.
".venv\Scripts\python.exe" bot.py

echo.
echo The bot has stopped.
pause
