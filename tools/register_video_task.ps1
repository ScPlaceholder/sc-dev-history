<#
register_video_task.ps1 - make tools\collect_videos.py start by itself on this PC.

Registers one Windows Task Scheduler task for the current user:
  * starts a few minutes after you log on (default 5), then again every few hours (default 6)
  * hidden (pythonw.exe, no console window), below-normal priority
  * never two at once (the scheduler ignores a new start while one runs; the tool also holds its own lock)
  * stopped by the scheduler if a run somehow lasts longer than 8 hours

    powershell -ExecutionPolicy Bypass -File tools\register_video_task.ps1 -Show   # print the task, register nothing
    powershell -ExecutionPolicy Bypass -File tools\register_video_task.ps1         # register it
    powershell -ExecutionPolicy Bypass -File tools\unregister_video_task.ps1       # remove it

No admin rights needed. The task first fires at your next logon; to start it right away after registering:
    Start-ScheduledTask -TaskName SCDevHistoryVideos
The tool only works while the branch named in tools\collect_videos.json ("corpus") is checked out, and it only
pushes when "push" is true there.
#>
param(
    [string]$Python = "",
    [int]$EveryHours = 6,
    [int]$DelayMinutes = 5,
    [string]$TaskName = "SCDevHistoryVideos",
    [switch]$Show
)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "collect_videos.py"
if (-not (Test-Path $script)) { throw "collect_videos.py is not next to this script ($script)" }

# The Python that has yt-dlp and faster-whisper. pythonw.exe = same interpreter without a console window.
if (-not $Python) {
    $py = (Get-Command python -ErrorAction Stop).Source
    $Python = Join-Path (Split-Path -Parent $py) "pythonw.exe"
    if (-not (Test-Path $Python)) { $Python = $py }
}
$check = Join-Path (Split-Path -Parent $Python) "python.exe"
if (-not (Test-Path $check)) { $check = $Python }
& $check -c "import yt_dlp, faster_whisper"
if ($LASTEXITCODE -ne 0) {
    throw "$check cannot import yt_dlp and faster_whisper. Pass the right interpreter with -Python <path to pythonw.exe>."
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}"' -f $script) -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT${DelayMinutes}M"
# A logon trigger has no -RepetitionInterval parameter: borrow the repetition block from a throwaway trigger.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Hours $EveryHours)).Repetition

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -Hidden -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) -Priority 7
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "SC Dev History: transcribe CIG's new videos and finished streams into the corpus (tools\collect_videos.py). CPU only, below-normal priority."

if ($Show) {
    "Task name : $TaskName   (NOT registered: -Show)"
    "Runs      : $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
    "Start in  : $($task.Actions[0].WorkingDirectory)"
    "Trigger   : at logon of $($task.Triggers[0].UserId), delay $($task.Triggers[0].Delay), then every $($task.Triggers[0].Repetition.Interval) (duration: '$($task.Triggers[0].Repetition.Duration)' = indefinitely)"
    "Settings  : hidden=$($task.Settings.Hidden) priority=$($task.Settings.Priority) multiple=$($task.Settings.MultipleInstances) timelimit=$($task.Settings.ExecutionTimeLimit) startWhenAvailable=$($task.Settings.StartWhenAvailable)"
    "Principal : $($task.Principal.UserId) logon=$($task.Principal.LogonType) runlevel=$($task.Principal.RunLevel)"
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
"Registered '$TaskName': $DelayMinutes min after logon, then every $EveryHours h. Remove it with tools\unregister_video_task.ps1"
