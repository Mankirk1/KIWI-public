<#
.SYNOPSIS
  Starts the agentbox phone panel (`agentctl serve`) whenever you sign in to Windows,
  and (optionally) publishes it to your phone over Tailscale.

.EXAMPLE
  # Windows blocks downloaded scripts by default; -ExecutionPolicy Bypass allows this one run only.
  powershell -ExecutionPolicy Bypass -File .\Register-PanelTask.ps1              # register + start now
  powershell -ExecutionPolicy Bypass -File .\Register-PanelTask.ps1 -Tailscale   # also `tailscale serve` for the phone
  powershell -ExecutionPolicy Bypass -File .\Register-PanelTask.ps1 -Stop        # stop the panel until next sign-in
  powershell -ExecutionPolicy Bypass -File .\Register-PanelTask.ps1 -Remove

.NOTES
  * The panel listens on 127.0.0.1:8765 inside WSL. WSL forwards that to
    http://localhost:8765 on Windows, and `tailscale serve` publishes it at
    https://<this-pc>.<your-tailnet>.ts.net to your own devices only (never the internet).
  * The task keeps one hidden wsl.exe session open, which also keeps the Ubuntu distro
    running so the panel is reachable whenever the PC is awake.
  * Sign the phone in with `agentctl pair` in Ubuntu.
#>
param(
  [string]$Distro = "Ubuntu-24.04",
  [string]$AgentHome = "~/KIWI/agentbox",
  [int]$Port = 8765,
  [switch]$Tailscale,
  [switch]$Stop,
  [switch]$Remove
)

$name = "agentbox panel"
if ($Stop -or $Remove) {
  # Stopping the task ends the hidden PowerShell, but the panel keeps running inside WSL.
  Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  # Match only the panel's own process (python .../agentctl serve), never a shell or editor
  # whose command line merely mentions it.
  wsl.exe -d $Distro -e pkill -f '^[^ ]*python[^ ]* [^ ]*/agentctl serve( |$)'
  if ($LASTEXITCODE -gt 1) { Write-Warning "Could not stop the panel inside WSL (pkill exit code $LASTEXITCODE)." }
  if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Removed '$name'. (Undo the Tailscale address with: tailscale serve reset)"
  } else {
    Write-Host "Stopped '$name'. It starts again at your next sign-in, or now with: Start-ScheduledTask '$name'"
  }
  return
}

$inner = "cd $AgentHome && exec agentctl serve --port $Port"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -Command `"wsl.exe -d $Distro --cd ~ -- bash -lc '$inner'`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "Registered and started '$name' (runs at every sign-in)."

$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Seconds 2
  try { if ((Invoke-WebRequest -UseBasicParsing "http://localhost:$Port/healthz" -TimeoutSec 3).StatusCode -eq 200) { $ok = $true; break } } catch { }
}
if ($ok) { Write-Host "Panel is up: http://localhost:$Port" }
else { Write-Warning "Panel did not answer on http://localhost:$Port yet. In Ubuntu run: agentctl serve   (and read the error)" }

if ($Tailscale) {
  $ts = Get-Command tailscale -ErrorAction SilentlyContinue
  if (-not $ts) { $ts = Get-Item "$env:ProgramFiles\Tailscale\tailscale.exe" -ErrorAction SilentlyContinue }
  if (-not $ts) { Write-Warning "Tailscale is not installed. Get it from https://tailscale.com/download/windows, sign in, then re-run with -Tailscale."; return }
  $tsExe = if ($ts -is [System.Management.Automation.CommandInfo]) { $ts.Source } else { $ts.FullName }
  & $tsExe serve --bg $Port
  & $tsExe serve status
  Write-Host ""
  Write-Host "Put the https://...ts.net address above into config.yaml as control.public_url, then open it on your phone."
}
