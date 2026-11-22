# Formivane Intelligence Centre - Windows Task Scheduler setup.
# Run in PowerShell from the repo root. Windows uses local time: set the PC to (UTC) Dublin, Edinburgh, Lisbon, London.
# Requires Python 3.11+ and Claude Code (`claude`) on PATH, logged in once interactively. The PC must be on.
$repo = (Get-Location).Path
$py = (Get-Command python).Source
function Add-FicTask($name, $trigger, $cmd) {
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c cd /d `"$repo`" && $cmd >> logs\$name.log 2>&1"
  Register-ScheduledTask -TaskName "FIC-$name" -Action $action -Trigger $trigger -Force | Out-Null
  Write-Host "Registered FIC-$name"
}
New-Item -ItemType Directory -Force -Path "$repo\logs" | Out-Null
$t4h = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 4)
Add-FicTask "collect" $t4h "`"$py`" -m fic collect"
Add-FicTask "daily"  (New-ScheduledTaskTrigger -Daily -At 09:07) "claude -p /fic-daily --allowedTools `"Bash(python -m fic:*)`" Agent Read WebFetch WebSearch"
Add-FicTask "weekly" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 18:07) "`"$py`" -m fic learn && claude -p /fic-weekly --allowedTools `"Bash(python -m fic:*)`" Agent Read WebFetch WebSearch"
Write-Host "Verify with: Get-ScheduledTask -TaskName FIC-*"
