$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut([Environment]::GetFolderPath("Desktop") + "\Chrome (JARVIS).lnk")
$lnk.TargetPath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
# Quotes REQUIRED around --user-data-dir value: "User Data" has a space.
# Port 9223: Chrome 151 silently ignores 9222 on this machine.
$lnk.Arguments = '"--user-data-dir=C:\Users\deped\AppData\Local\Google\Chrome\User Data" --remote-debugging-port=9223'
$lnk.Description = "Chrome with remote debugging for JARVIS (Default profile)"
$lnk.Save()
Write-Output "Shortcut updated: quoted path + port 9223"
