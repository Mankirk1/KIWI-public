<#
.SYNOPSIS
  Registers a Windows scheduled task that runs `agentctl continue-all` every night.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\Register-NightlyTask.ps1 -At "23:30"
  powershell -ExecutionPolicy Bypass -File .\Register-NightlyTask.ps1 -Remove

.NOTES
  * Runs as you, only when you are logged on (Docker Desktop needs your session).
    Lock the screen instead of signing out.
  * Wakes the PC from sleep to start, and only starts on AC power.
  * Stops itself after -MaxHours (the orchestrator also has its own budget in config.yaml).
#>
param(
  [string]$At = "23:30",
  [int]$MaxHours = 9,
  [string]$Command = "continue-all",
  [string]$Distro = "Ubuntu-24.04",
  [switch]$Remove
)

$name = "agentbox nightly run"
if ($Remove) {
  Unregister-ScheduledTask -TaskName $name -Confirm:$false
  Write-Host "Removed '$name'."
  return
}

$script = Join-Path $PSScriptRoot "Start-AgentRun.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Command `"$Command`" -Distro $Distro"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -WakeToRun -DontStopOnIdleEnd `
  -ExecutionTimeLimit (New-TimeSpan -Hours $MaxHours) -StartWhenAvailable -MultipleInstances IgnoreNew
$settings.DisallowStartIfOnBatteries = $true
$settings.StopIfGoingOnBatteries = $true
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered '$name' daily at $At (max $MaxHours h, AC power only, wakes the PC)."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName '$name'"
