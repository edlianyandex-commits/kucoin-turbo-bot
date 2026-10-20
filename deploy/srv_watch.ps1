# TB Core — фоновый мост к серверу YOUR_SERVER_IP.
# Claude пишет deploy\srv_task.sh и файл-заявку deploy\out\srv_go (внутри id заявки).
# Мост выполняет srv_task.sh на сервере под пользователем tradebot и пишет вывод в deploy\out\srv_task.log.
# Остановить: закрыть это окно.
$Dir = $PSScriptRoot
$Out = Join-Path $Dir "out"
New-Item -ItemType Directory -Force $Out | Out-Null
$Key  = "$env:USERPROFILE\.ssh\tradebot_ed25519"
$Go   = Join-Path $Out "srv_go"
$Log  = Join-Path $Out "srv_task.log"
$Task = Join-Path $Dir "srv_task.sh"
$Host.UI.RawUI.WindowTitle = "TB Core - мост к серверу (закрой окно, чтобы остановить)"
Write-Host "Мост запущен $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'). Жду заявок..."
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] bridge up" | Set-Content -Encoding utf8 (Join-Path $Out "srv_bridge.alive")
while ($true) {
    if (Test-Path $Go) {
        Start-Sleep -Milliseconds 500
        $id = ((Get-Content $Go -Raw) -as [string]).Trim()
        Remove-Item $Go -Force
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-Host "[$ts] заявка $id"
        "[$ts] start id=$id" | Set-Content -Encoding utf8 $Log
        cmd /c "ssh -i `"$Key`" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new tradebot@YOUR_SERVER_IP bash -s < `"$Task`" >> `"$Log`" 2>&1"
        $rc = $LASTEXITCODE
        "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] done id=$id exit=$rc" | Add-Content -Encoding utf8 $Log
        Write-Host "  готово, exit=$rc"
    }
    Start-Sleep -Seconds 2
}
