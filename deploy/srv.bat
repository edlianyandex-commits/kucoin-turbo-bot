@echo off
chcp 65001 >nul
rem Мост к серверу: выполняет deploy\srv_task.sh на YOUR_SERVER_IP и пишет вывод в deploy\out\srv_task.log
set "KEY=%USERPROFILE%\.ssh\tradebot_ed25519"
set "OUT=%~dp0out"
if not exist "%OUT%" mkdir "%OUT%"
echo [%DATE% %TIME%] start > "%OUT%\srv_task.log"
ssh -i "%KEY%" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new tradebot@YOUR_SERVER_IP bash -s < "%~dp0srv_task.sh" >> "%OUT%\srv_task.log" 2>&1
echo [%DATE% %TIME%] exit=%ERRORLEVEL% >> "%OUT%\srv_task.log"
type "%OUT%\srv_task.log"
timeout /t 5
