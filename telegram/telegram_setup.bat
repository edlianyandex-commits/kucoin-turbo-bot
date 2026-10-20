@echo off
chcp 65001 >nul
title TradeBot - настройка Telegram
set KEY=%USERPROFILE%\.ssh\tradebot_ed25519
set SRV=tradebot@YOUR_SERVER_IP
echo Настройка Telegram-бота для уведомлений с сервера.
echo.
echo   1 - первая настройка или новый токен
echo   2 - привязать другой аккаунт Telegram (токен прежний)
echo   3 - прислать пробное сообщение
echo.
set /p CH=Выберите 1, 2 или 3 и нажмите Enter:
if "%CH%"=="2" goto pair
if "%CH%"=="3" goto test

echo.
echo 1. В Telegram откройте @BotFather и отправьте ему /newbot
echo 2. Придумайте имя бота, затем адрес (латиницей, в конце обязательно bot, например my_trade_42_bot)
echo 3. BotFather пришлёт токен вида 123456789:AAH... - скопируйте его целиком
echo.
set TOKEN=
set /p TOKEN=Вставьте токен (правая кнопка мыши) и нажмите Enter:
if "%TOKEN%"=="" goto end
echo %TOKEN%| ssh -i "%KEY%" %SRV% "python3 /opt/tradebot/telegram/telegram_bot.py setup"
set TOKEN=
goto end

:pair
ssh -i "%KEY%" %SRV% "python3 /opt/tradebot/telegram/telegram_bot.py pair"
goto end

:test
ssh -i "%KEY%" %SRV% "python3 /opt/tradebot/telegram/telegram_bot.py test"

:end
echo.
pause
