@echo off
chcp 65001 >nul
title TradeBot - отчёт с сервера
ssh -i "%USERPROFILE%\.ssh\tradebot_ed25519" tradebot@YOUR_SERVER_IP "cat /opt/tradebot/reports/latest.txt 2>/dev/null || echo 'Отчёта ещё нет (первый — в 07:05 МСК).'; echo; echo '--- последние тревоги:'; tail -n 15 /opt/tradebot/reports/alerts.log 2>/dev/null || echo 'тревог не было'"
echo.
pause
