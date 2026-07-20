$ErrorActionPreference = 'Stop'

$automationRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $automationRoot 'config.json'
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

foreach ($taskName in @([string]$config.tasks.daily_name, [string]$config.tasks.weekly_name)) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed task: $taskName"
}
