$ErrorActionPreference = 'Stop'

$automationRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $automationRoot
$scriptPath = Join-Path $automationRoot 'main.py'
$configPath = Join-Path $automationRoot 'config.json'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Missing automation script: $scriptPath"
}
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Missing automation config: $configPath"
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$dailyTaskName = [string]$config.tasks.daily_name
$weeklyTaskName = [string]$config.tasks.weekly_name
$dailyTime = [string]$config.daily.time
$weeklyTime = [string]$config.weekly.time
$weeklyDay = [System.Enum]::Parse([System.DayOfWeek], [string]$config.weekly.weekday, $true)

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    throw 'Python 3.9 or newer was not found in PATH.'
}

$pythonPath = $pythonCommand.Source
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$timeZone = Get-TimeZone
if ($timeZone.Id -ne 'China Standard Time') {
    throw "Windows time zone must be China Standard Time for Beijing schedules; current value: $($timeZone.Id)"
}

function Register-ReviewTask {
    param(
        [string]$TaskName,
        [string]$Subcommand,
        [string]$StartTime,
        [string]$Description
    )

    $arguments = '"{0}" {1} --scheduled' -f $scriptPath, $Subcommand
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $repoRoot
    if ($Subcommand -eq 'weekly') {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weeklyDay -At $StartTime
    }
    else {
        $trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description $Description `
        -User $currentUser `
        -RunLevel Limited `
        -Force | Out-Null
}

Register-ReviewTask `
    -TaskName $dailyTaskName `
    -Subcommand 'daily' `
    -StartTime $dailyTime `
    -Description "Daily committed wrong-question report at Beijing time $dailyTime."

Register-ReviewTask `
    -TaskName $weeklyTaskName `
    -Subcommand 'weekly' `
    -StartTime $weeklyTime `
    -Description "Weekly math and 408 test at Beijing time on $($config.weekly.weekday) $weeklyTime."

Write-Output "Registered task: $dailyTaskName (daily $dailyTime, Beijing time)"
Write-Output "Registered task: $weeklyTaskName ($($config.weekly.weekday) $weeklyTime, Beijing time)"
Write-Output 'Reports are generated under the report directories and are not committed automatically.'
