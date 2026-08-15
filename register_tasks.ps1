# RoulCollector448 — Windows scheduled-task equivalents of the systemd units.
# Usage:  powershell -ExecutionPolicy Bypass -File register_tasks.ps1
# Remove: powershell -ExecutionPolicy Bypass -File register_tasks.ps1 -Remove
#
# Tasks (run at logon, hidden):
#   RoulCollector448-Collector  -> start_collector.bat   (24/7 capture loop)
#   RoulCollector448-Dashboard  -> start_dashboard.bat   (API + UI on :4480)
param([switch]$Remove)

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

function Register-Task($name, $bat) {
    $action  = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"$repo\$bat`"" -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description "RoulCollector448 ($bat)" -Force | Out-Null
    Write-Host "Registered: $name"
}

# Watchdog: every 5 minutes, like the Linux systemd timer. It needs the
# venv python to run scripts/watchdog_win.py.
function Register-Watchdog {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"$repo\.venv\Scripts\python.exe`" `"$repo\scripts\watchdog_win.py`"" `
        -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName "RoulCollector448-Watchdog" -Action $action `
        -Trigger $trigger -Settings $settings `
        -Description "RoulCollector448 watchdog (restart silent collector)" -Force | Out-Null
    Write-Host "Registered: RoulCollector448-Watchdog (every 5 min)"
}

if ($Remove) {
    foreach ($n in @("RoulCollector448-Collector", "RoulCollector448-Dashboard",
                     "RoulCollector448-Watchdog")) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed: $n"
    }
} else {
    Register-Task "RoulCollector448-Dashboard" "start_dashboard.bat"
    Register-Task "RoulCollector448-Collector" "start_collector.bat"
    Register-Watchdog
    Write-Host ""
    Write-Host "Done. Tasks run at logon; watchdog every 5 min. The collector"
    Write-Host "exits until credentials exist in"
    Write-Host "%USERPROFILE%\.roulette2\roulette2_collector.env (SUNBET_USER/SUNBET_PASS)."
    Write-Host "Uninstall: rerun with -Remove"
}
