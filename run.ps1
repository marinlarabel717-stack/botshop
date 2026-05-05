$ErrorActionPreference = 'Stop'

if (!(Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host '已创建 .env，请先编辑里面的 BOT_TOKEN 和 MongoDB 配置。'
  exit 1
}

Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $name, $value = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
}

python haopubot.py
