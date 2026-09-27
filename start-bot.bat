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

rem Rebuild the Lavalink image once a day so yt-dlp picks up YouTube fixes.
rem The date is the cache key: same day = instant, new day = fresh download.
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
echo Checking for yt-dlp updates (first launch of the day takes a little longer)...
docker compose build --build-arg YTDLP_REFRESH=%TODAY% lavalink
if errorlevel 1 echo Could not refresh yt-dlp - carrying on with the copy already installed.

rem Each daily rebuild leaves the previous image and its download cache behind.
rem Clear those leftovers so they don't slowly fill the disk.
docker image prune -f >nul 2>&1
docker builder prune -f --filter "until=72h" >nul 2>&1

echo Starting Lavalink (fresh, so config changes take effect)...
docker compose down >nul 2>&1
docker compose up -d

echo Giving Lavalink a few seconds to boot...
timeout /t 15 /nobreak >nul

echo Starting the bot. Close this window to stop it.
".venv\Scripts\python.exe" bot.py

echo.
echo The bot has stopped.
pause
