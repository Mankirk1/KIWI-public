<#
.SYNOPSIS
  Optional power settings for unattended overnight runs on a laptop (run in an elevated PowerShell).

.DESCRIPTION
  - Closing the lid while plugged in does nothing (instead of sleeping).
  - Allows wake timers, so the scheduled task can wake the PC.
  Sleep-while-running is already prevented by Start-AgentRun.ps1, so normal sleep settings
  can stay as they are. Undo with -Revert.
#>
param([switch]$Revert)

$SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"
$LIDACTION   = "5ca83367-6e45-459f-a27b-476b1d01c936"
$SUB_SLEEP   = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
$WAKETIMERS  = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"

if ($Revert) {
  powercfg /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $LIDACTION 1   # 1 = sleep (Windows default)
  Write-Host "Lid action on AC restored to Sleep."
} else {
  powercfg /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $LIDACTION 0   # 0 = do nothing
  powercfg /setacvalueindex SCHEME_CURRENT $SUB_SLEEP $WAKETIMERS 1    # 1 = enable wake timers
  Write-Host "Plugged in: closing the lid does nothing; wake timers enabled."
}
powercfg /setactive SCHEME_CURRENT
