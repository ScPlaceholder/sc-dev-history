<#
unregister_video_task.ps1 - remove the scheduled task made by register_video_task.ps1.

    powershell -ExecutionPolicy Bypass -File tools\unregister_video_task.ps1

A run that is in progress is stopped first (it loses at most the video it was on; the next run redoes it).
Nothing in the repository is touched.
#>
param([string]$TaskName = "SCDevHistoryVideos")
$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    "No scheduled task named '$TaskName': nothing to remove."
    exit 0
}
if ($task.State -eq "Running") { Stop-ScheduledTask -TaskName $TaskName }
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
"Removed scheduled task '$TaskName'."
