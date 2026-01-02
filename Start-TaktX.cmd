@echo off
title ClipVault Server
echo Starting ClipVault...
echo.
echo Make sure yt-dlp.exe and ffmpeg.exe are in this folder!
echo.
pip install -r requirements.txt
start "" "http://localhost:8000"
python server.py
pause