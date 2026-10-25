<#
.SYNOPSIS
  Runs an agentbox command in WSL while keeping Windows awake, with Ollama paused for the run.

.EXAMPLE
  .\Start-AgentRun.ps1 -Command "new outlines/mechcalc.md"
  .\Start-AgentRun.ps1 -Command "continue mechcalc"
  .\Start-AgentRun.ps1 -Command "continue-all"

.NOTES
  * Keeps the PC awake only while the run lasts (SetThreadExecutionState); your normal
    power plan is unchanged. The display may still turn off.
  * Quits Ollama (tray app + server) before the run so it cannot compete for RAM/CPU, and
    starts the Ollama app again afterwards if it was running (-RestoreOllama:$false to skip).
    agentctl has its own guard as a second line of defence (guards.ollama in config.yaml).
  * Starts Docker Desktop if needed and waits for it.
#>
param(
  [Parameter(Mandatory = $true)][string]$Command,
  [string]$Distro = "Ubuntu-24.04",
  [string]$AgentHome = "~/KIWI/agentbox",
  [bool]$RestoreOllama = $true,
  # Windows power mode for the run (restored afterwards). "Best power efficiency" was measured
  # to cost ~10% generation speed; Keep leaves the current mode alone.
  [ValidateSet("BestPerformance", "Keep")][string]$PowerMode = "BestPerformance"
)

$ErrorActionPreference = "Stop"
$logDir = Join-Path $env:LOCALAPPDATA "agentbox"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("run-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
Start-Transcript -Path $log | Out-Null

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Awake {
  [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
}
"@
$ES_CONTINUOUS = [uint32]"0x80000000"
$ES_SYSTEM_REQUIRED = [uint32]"0x00000001"
$ES_AWAYMODE_REQUIRED = [uint32]"0x00000040"

# Is the Docker daemon answering? `docker info` prints harmless warnings on stderr (for example
# "No blkio throttle.read_bps_device support"); with $ErrorActionPreference = "Stop" PowerShell
# turns any redirected stderr line into a terminating error, so the check must run with
# "Continue" and trust only the exit code. (Found 2026-10-18: without this the script waited
# five minutes and reported that Docker never became ready.)
function Test-Docker {
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { docker info 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) }
  catch { return $false }
  finally { $ErrorActionPreference = $previous }
}

$ollamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
$ollamaWasRunning = $false
$BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"
$previousOverlay = $null

try {
  [void][Awake]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED -bor $ES_AWAYMODE_REQUIRED)
  Write-Host "Keeping the PC awake for this run. Log: $log"

  # --- Power mode (the Settings > Power mode slider; plugged-in value)
  if ($PowerMode -eq "BestPerformance") {
    try {
      $previousOverlay = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes").ActiveOverlayAcPowerScheme
      if ($previousOverlay -ne $BEST_PERFORMANCE) {
        powercfg /overlaysetactive $BEST_PERFORMANCE
        Write-Host "Power mode set to Best performance for this run."
      } else { $previousOverlay = $null }
    } catch { Write-Warning "Could not change the power mode: $_"; $previousOverlay = $null }
  }

  # --- Ollama: quit the tray app first (it would restart the server), then the server/runners
  $procs = Get-Process -Name "ollama app", "ollama", "ollama_llama_server" -ErrorAction SilentlyContinue
  if ($procs) {
    $ollamaWasRunning = [bool]($procs | Where-Object { $_.ProcessName -eq "ollama app" })
    Write-Host "Stopping Ollama for this run: $($procs.ProcessName -join ', ')"
    Get-Process -Name "ollama app" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    Get-Process -Name "ollama", "ollama_llama_server" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    if (Get-Process -Name "ollama app", "ollama", "ollama_llama_server" -ErrorAction SilentlyContinue) {
      throw "Ollama is still running and could not be stopped. Quit it from the tray icon and retry."
    }
  }

  # --- Docker Desktop
  $dockerOk = Test-Docker
  if (-not $dockerOk) {
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) {
      Write-Host "Starting Docker Desktop..."
      Start-Process $dd
    }
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep -Seconds 5
      if (Test-Docker) { $dockerOk = $true; break }
    }
    if (-not $dockerOk) { throw "Docker Desktop did not become ready within 5 minutes." }
  }

  # --- Services (idempotent), then the agent
  $inner = "cd $AgentHome && docker compose up -d >/dev/null && agentctl $Command"
  Write-Host "wsl> $inner"
  wsl.exe -d $Distro --cd ~ -- bash -lc "$inner"
  $code = $LASTEXITCODE
  Write-Host "agentctl exited with code $code"
  exit $code
}
finally {
  [void][Awake]::SetThreadExecutionState($ES_CONTINUOUS)
  if ($previousOverlay) {
    try { powercfg /overlaysetactive $previousOverlay; Write-Host "Power mode restored." } catch { }
  }
  if ($RestoreOllama -and $ollamaWasRunning -and (Test-Path $ollamaApp)) {
    Write-Host "Restarting the Ollama app."
    Start-Process $ollamaApp
  }
  Stop-Transcript | Out-Null
}
