$procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"
$seen = @{}
foreach ($p in $procs) {
    $cl = $p.CommandLine
    if (-not $cl) { continue }
    $key = $cl -replace '\s+', ' '
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = $true
    $u = if ($cl -match '--user-data-dir=([^"]+)"?') { $matches[1] } else { "(default)" }
    $prof = if ($cl -match '--profile-directory=([^"]+)"?') { $matches[1] } else { "(default)" }
    $dbg = if ($cl -match '--remote-debugging-port=(\d+)') { $matches[1] } else { "none" }
    Write-Output "PID $($p.ProcessId) | profile=$prof | user-data=$u | debug=$dbg"
}
