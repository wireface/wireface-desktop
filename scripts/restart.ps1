# Restart Wireface: wait for the current reply to finish, stop the app, start it again.
# The conversation is picked up where it left off (the app saves its session id after every turn).
#
# Run it detached, so stopping the app does not stop this script too:
#   Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
#       CommandLine = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File B:\Claude\scripts\restart.ps1' }
param([int]$Delay = 15)          # seconds to let the reply be spoken before stopping

$root = Split-Path -Parent $PSScriptRoot
Start-Sleep -Seconds $Delay

function Get-Face {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'pythonw.exe' -and $_.CommandLine -match '-m\s+wireface') -or
        ($_.Name -eq 'electron.exe' -and $_.ExecutablePath -like "$root\deps\shell\*")
    }
}

foreach ($p in Get-Face) { & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null }
for ($i = 0; $i -lt 20 -and (Get-Face); $i++) { Start-Sleep -Milliseconds 500 }

Start-Process -FilePath (Join-Path $root 'Wireface.cmd') -WorkingDirectory $root -WindowStyle Hidden
