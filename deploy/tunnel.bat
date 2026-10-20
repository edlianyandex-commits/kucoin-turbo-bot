@echo off
chcp 65001 >nul
title TradeBot - туннель к серверу
echo Туннель к серверу YOUR_SERVER_IP открыт, пока открыто это окно.
echo   Основной бот на сервере:  http://127.0.0.1:8780
echo   Турбо-бот на сервере:     http://127.0.0.1:8781
echo.
start "" http://127.0.0.1:8780
ssh -i "%USERPROFILE%\.ssh\tradebot_ed25519" -N -o ServerAliveInterval=30 -L 8780:127.0.0.1:8770 -L 8781:127.0.0.1:8771 tradebot@YOUR_SERVER_IP
pause
