@echo off
cd /d "%~dp0"
echo Connecting your Claude Pro/Max subscription...
echo A link will appear. Open it, sign in with your Claude account, and paste the code back here if asked.
echo.
docker compose -f docker-compose.local.yml --env-file .env.local exec -it worker claude setup-token
echo.
echo Copy the token above (it starts with sk-ant-oat01-) and paste it in the portal:
echo   Settings - Research engine - Subscription token - Save
pause
