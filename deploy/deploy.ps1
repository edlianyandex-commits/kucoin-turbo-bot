# Выкладка ботов на сервер одной командой: упаковать → отправить → установить → перезапустить службы.
#   powershell -ExecutionPolicy Bypass -File "C:\bot\deploy\deploy.ps1"
# Ключи API, настройки панели, журналы и свечи на сервере не затрагиваются.
param([string]$Server = "YOUR_SERVER_IP", [switch]$SkipInstall)
$ErrorActionPreference = "Stop"
$Key = "$env:USERPROFILE\.ssh\tradebot_ed25519"
$Out = Join-Path $PSScriptRoot "out"
New-Item -ItemType Directory -Force $Out | Out-Null
$Ssh = @("-i", $Key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
$Excludes = @("venv", "caches", "backtests", "optimize_results", "_passivbot_extras", "logs", "monitor",
              "passivbot-rust/target", "tb4/panel/data", "configs/tb4", "configs/turbo", "tb4/results",
              "configs/turbo_dd", "turbo/results/dd_curves", "turbo/results/coin_pick", "turbo/results/coin_algos",
              "turbo/results/paper_window", "turbo/results/coin_prices_1h*.json", "turbo/results/btc_1h*.json",
              "turbo/results/risk_rules.json", "turbo/results/daily_curves*", "turbo/results/daily_panic*.json",
              "turbo/results/chain_rules.json", "turbo/results/engine_month", "turbo/results/engine_daily",
              "turbo/results/param_search", "turbo/results/ohlc_1h*.npz", "turbo/results/universe_test",
              "turbo/results/listings_binance.npz", "turbo/results/daily_curves*",
              "turbo/results/main_variants_fills", "turbo/results/funding_*.json", "turbo/results/real_test",
              "turbo/results/drawdown_rules_v*_all.json", "traders",
              "turbo/results/coin_hsl", "turbo/results/exit_rules", "turbo/results/btc_link",
              "turbo/results/coins_1h*.npz", "turbo/results/reversal_signals.csv.gz", "turbo/results/eth_1h.json", "turbo/results/v4_research",
              "turbo/results/kucoin_turnover_daily.json", "turbo/results/kucoin_daily_hl.json",
              "turbo/results/super", "turbo/results/super_*.json", "turbo/results/x2_bets.json", "turbo/results/kucoin_turnover_daily_full.json", "turbo/results/kucoin_daily_hl_full.json", "turbo/results/delisted_*.json", "turbo/results/full_losses.json",   # данные движка разгона (1,2 ГБ)
              "__pycache__", "*.log", "api-keys.json", "deploy/out", "*.pkl", "*.egg-info",
              "tb4/kucoin_coins.json", "tb4/kucoin_min_cost.json",   # список монет сервер обновляет сам раз в месяц
              "./боты",       # история чата и описание ботов — только на компьютере
              "./turbo")      # исследования Turbo (скрипты и результаты) — панели на сервере они не нужны
$Apps = @{ "turbo" = "C:\bot\turbo" }
# Общая обвязка (15.10.2026): раньше install-скрипты, отчёты, стенд и телеграм-бот ехали внутри папки
# основного бота. Бот остался один, обвязка лежит рядом с ним и выкладывается отдельными архивами
# в /opt/tradebot/deploy и /opt/tradebot/telegram.
$Shared = @{ "deploy" = "C:\bot\deploy"; "telegram" = "C:\bot\telegram" }
$SharedExcludes = @("out", "__pycache__", "config.json", "state.json")   # настройки и состояние телеграма — серверные

foreach ($app in $Apps.Keys) {
    $archive = Join-Path $Out "tb_$app.tar.gz"
    $targs = @("-czf", $archive) + ($Excludes | ForEach-Object { "--exclude=$_" }) + @("-C", $Apps[$app], ".")
    & tar @targs
    if ($LASTEXITCODE) { throw "tar: ошибка упаковки $app" }
    "{0}: архив {1:N1} МБ" -f $app, ((Get-Item $archive).Length / 1MB)
    & scp @Ssh $archive "tradebot@${Server}:/tmp/"
    if ($LASTEXITCODE) { throw "не удалось отправить $archive" }
}
foreach ($name in $Shared.Keys) {
    $archive = Join-Path $Out "tb_$name.tar.gz"
    $targs = @("-czf", $archive) + ($SharedExcludes | ForEach-Object { "--exclude=$_" }) + @("-C", $Shared[$name], ".")
    & tar @targs
    if ($LASTEXITCODE) { throw "tar: ошибка упаковки $name" }
    "{0}: общая обвязка, архив {1:N2} МБ" -f $name, ((Get-Item $archive).Length / 1MB)
    & scp @Ssh $archive "tradebot@${Server}:/tmp/"
    if ($LASTEXITCODE) { throw "не удалось отправить $archive" }
}
if (-not $SkipInstall) {
    cmd /c "ssh -i `"$Key`" -o BatchMode=yes tradebot@$Server bash -s < `"$PSScriptRoot\server_install.sh`""
    if ($LASTEXITCODE) { throw "установка на сервере завершилась с ошибкой" }
}
cmd /c "ssh -i `"$Key`" -o BatchMode=yes root@$Server bash -s < `"$PSScriptRoot\server_memory.sh`""
cmd /c "ssh -i `"$Key`" -o BatchMode=yes root@$Server bash -s < `"$PSScriptRoot\server_services.sh`""
cmd /c "ssh -i `"$Key`" -o BatchMode=yes tradebot@$Server bash -s < `"$PSScriptRoot\server_cron.sh`""
"Готово. Панель на сервере: откройте туннель (deploy\tunnel.bat) и зайдите на http://127.0.0.1:8771"
