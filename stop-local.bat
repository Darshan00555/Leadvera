@echo off
cd /d "%~dp0"
docker compose -f docker-compose.local.yml --env-file .env.local down
echo Stopped. Your data is kept. Start again with start-local.bat
pause
