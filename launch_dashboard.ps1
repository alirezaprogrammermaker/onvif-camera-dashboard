param([switch]$OpenBrowser)

$ErrorActionPreference = "Stop"
$settingsPath = Join-Path $env:LOCALAPPDATA "CameraDashboard\camera.json"
if (-not (Test-Path $settingsPath)) {
  throw "Dashboard setup was not found. Run install.bat first."
}
$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
if (-not $settings.cameraIp -or -not (Test-Path $settings.pythonPath)) {
  throw "The saved camera or Python configuration is invalid. Run install.bat again."
}
$env:PATH = "$($settings.ffmpegDirectory);$env:PATH"
$port = [int]$settings.port
$baseUrl = "http://127.0.0.1:$port"
$logDirectory = Join-Path (Split-Path $settingsPath) "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

$alreadyRunning = $false
try {
  $recordingStatus = Invoke-RestMethod -Uri "$baseUrl/api/recording" -TimeoutSec 2
  if ($null -ne $recordingStatus.recording) { $alreadyRunning = $true }
} catch {
  $alreadyRunning = $false
}

if (-not $alreadyRunning) {
  $stdout = Join-Path $logDirectory "dashboard.log"
  $stderr = Join-Path $logDirectory "dashboard-error.log"
  $arguments = @(
    "`"$PSScriptRoot\camera_web.py`"",
    "--ip", "`"$($settings.cameraIp)`"",
    "--port", "$port"
  )
  Start-Process -FilePath $settings.pythonPath -ArgumentList $arguments `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

  $ready = $false
  for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
      $recordingStatus = Invoke-RestMethod -Uri "$baseUrl/api/recording" -TimeoutSec 2
      if ($null -ne $recordingStatus.recording) { $ready = $true; break }
    } catch {
      $lastStartError = $_.Exception.Message
    }
  }
  if (-not $ready) {
    $details = ""
    if (Test-Path $stderr) { $details = Get-Content -LiteralPath $stderr -Raw }
    throw "Dashboard did not start. See $stderr. $details $lastStartError"
  }
}

if ($OpenBrowser) {
  Start-Process $baseUrl
  Write-Host "Dashboard is running at $baseUrl"
}
