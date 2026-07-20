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
    Write-Warning "Current Windows time zone is $($timeZone.Id), not China Standard Time."
}

function Register-ReviewTask {
    param(
        [string]$TaskName,
        [string]$Subcommand,
        [string]$StartTime,
        [string]$Description
    )

    $arguments = '"{0}" {1}' -f $scriptPath, $Subcommand
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $repoRoot
    if ($Subcommand -eq 'weekly') {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $StartTime
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
    -StartTime '22:30' `
    -Description 'Daily committed wrong-question report at Beijing time 22:30.'

Register-ReviewTask `
    -TaskName $weeklyTaskName `
    -Subcommand 'weekly' `
    -StartTime '08:00' `
    -Description 'Weekly math and 408 test at Beijing time on Sunday 08:00.'

Write-Output "Registered task: $dailyTaskName (daily 22:30)"
Write-Output "Registered task: $weeklyTaskName (Sunday 08:00)"
Write-Output 'Reports are generated under the report directories and are not committed automatically.'
