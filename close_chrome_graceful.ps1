# Graceful close of user's daily Chrome so its session flushes to disk.
# CloseMainWindow = WM_CLOSE (graceful), unlike taskkill /F.
$main = Get-Process chrome -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 }
foreach ($p in $main) {
    Write-Output "Closing window PID $($p.Id): $($p.MainWindowTitle)"
    $null = $p.CloseMainWindow()
}
Start-Sleep -Seconds 8
# Chrome keeps running (background) until all windows closed + it decides to exit;
# if still alive after windows close, let it flush: wait a bit more.
Start-Sleep -Seconds 5
$left = Get-Process chrome -ErrorAction SilentlyContinue
Write-Output "Remaining chrome processes: $($left.Count)"
