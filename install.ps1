$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repository = "https://github.com/alirezaprogrammermaker/onvif-camera-dashboard/archive/refs/heads/main.zip"
$installDirectory = Join-Path $env:LOCALAPPDATA "Programs\ONVIFCameraDashboard"
$settingsDirectory = Join-Path $env:LOCALAPPDATA "CameraDashboard"
$settingsPath = Join-Path $settingsDirectory "camera.json"
$workingDirectory = Join-Path $env:TEMP ("CameraDashboard-" + [guid]::NewGuid().ToString("N"))

function Write-Step([string]$Message) {
  Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Get-WinGetPath {
  $candidate = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\winget.exe"
  if (Test-Path $candidate) { return $candidate }
  $command = Get-Command winget.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  throw "Windows Package Manager (winget) was not found. Install or update 'App Installer' from Microsoft Store, then run install.bat again."
}

function Install-WinGetPackage([string]$WinGet, [string]$Id) {
  & $WinGet install --exact --id $Id --scope user --silent `
    --accept-package-agreements --accept-source-agreements --disable-interactivity
  if ($LASTEXITCODE -ne 0) {
    throw "Installing $Id failed (winget exit code $LASTEXITCODE). Check the Windows App Installer and internet connection, then retry."
  }
}

function Get-PythonPath {
  $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
  if (Test-Path $candidate) { return $candidate }
  $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
  if (Test-Path $candidate) { return $candidate }
  $command = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($command -and $command.Source -notlike "*WindowsApps*") { return $command.Source }
  throw "Python was installed but could not be located. Open a new terminal and retry the installer."
}

function Get-FfmpegDirectory {
  $links = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"
  if (Test-Path (Join-Path $links "ffmpeg.exe")) { return $links }
  $command = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
  if ($command) { return Split-Path $command.Source }
  $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
  $match = Get-ChildItem $packageRoot -Directory -Filter "Gyan.FFmpeg.Shared_*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
  if ($match) {
    $binary = Get-ChildItem $match.FullName -Filter ffmpeg.exe -File -Recurse -ErrorAction SilentlyContinue |
      Select-Object -First 1
    if ($binary) { return $binary.DirectoryName }
  }
  throw "FFmpeg installation completed, but ffmpeg.exe was not found."
}

try {
  Write-Host "ONVIF Camera Dashboard - first-time setup" -ForegroundColor Green
  if (-not $env:LOCALAPPDATA) { throw "This installer requires a Windows user profile." }
  $winget = Get-WinGetPath
  New-Item -ItemType Directory -Path $workingDirectory -Force | Out-Null

  Write-Step "Installing Python and FFmpeg (no administrator access required)"
  try { $python = Get-PythonPath } catch { Install-WinGetPackage $winget "Python.Python.3.12" }
  try { $ffmpegDirectory = Get-FfmpegDirectory } catch { Install-WinGetPackage $winget "Gyan.FFmpeg.Shared" }
  $python = Get-PythonPath
  $ffmpegDirectory = Get-FfmpegDirectory
  $env:PATH = "$ffmpegDirectory;$env:LOCALAPPDATA\Programs\Python\Python312;$env:PATH"
  & $python --version
  if ($LASTEXITCODE -ne 0) { throw "Python did not start correctly." }
  & (Join-Path $ffmpegDirectory "ffmpeg.exe") -version 2>&1 | Select-Object -First 1
  if ($LASTEXITCODE -ne 0) { throw "FFmpeg did not start correctly." }

  Write-Step "Downloading the dashboard"
  $archive = Join-Path $workingDirectory "project.zip"
  Invoke-WebRequest -UseBasicParsing -Uri $repository -OutFile $archive
  Expand-Archive -LiteralPath $archive -DestinationPath $workingDirectory -Force
  $sourceDirectory = Get-ChildItem $workingDirectory -Directory |
    Where-Object { $_.Name -like "onvif-camera-dashboard-*" } |
    Select-Object -First 1
  if (-not $sourceDirectory) { throw "The downloaded project archive did not contain the expected files." }
  New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
  foreach ($file in @("camera_web.py", "camera_diagnostics.py", "discover_camera.py", "launch_dashboard.ps1", "README.md")) {
    $sourceFile = Join-Path $sourceDirectory.FullName $file
    if (-not (Test-Path $sourceFile)) { throw "A required project file is missing: $file" }
    Copy-Item -LiteralPath $sourceFile -Destination $installDirectory -Force
  }
  $sourceWeb = Join-Path $sourceDirectory.FullName "web"
  $targetWeb = Join-Path $installDirectory "web"
  if (Test-Path $targetWeb) { Remove-Item -LiteralPath $targetWeb -Recurse -Force }
  Copy-Item -LiteralPath $sourceWeb -Destination $targetWeb -Recurse -Force

  Write-Step "Finding the camera on your local network"
  $subnets = @(
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object {
        $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and
        $_.InterfaceAlias -notmatch 'vEthernet|WSL|Loopback|Default Switch' -and
        $_.PrefixLength -ge 22 -and $_.PrefixLength -le 24
      } |
      ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)" } |
      Select-Object -Unique
  )
  $discoveryScript = Join-Path $installDirectory "discover_camera.py"
  $discoveryArguments = @($discoveryScript, "--timeout", "4")
  foreach ($subnet in $subnets) {
    $discoveryArguments += @("--subnet", $subnet)
  }
  $discoverOutput = @(& $python @discoveryArguments 2>$null)
  if ($LASTEXITCODE -ne 0) { $discoverOutput = @() }
  $cameras = @($discoverOutput | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^\d{1,3}(\.\d{1,3}){3}$' } | Select-Object -Unique)
  if ($cameras.Count -eq 1) {
    $cameraIp = $cameras[0]
    Write-Host "Found camera at $cameraIp"
  } else {
    if ($cameras.Count -gt 1) {
      Write-Host "Several ONVIF devices were found: $($cameras -join ', ')" -ForegroundColor Yellow
    } else {
      Write-Host "Automatic discovery found no ONVIF camera. Ensure the camera is on this network." -ForegroundColor Yellow
    }
    do {
      $cameraIp = Read-Host "Enter the camera's local IPv4 address"
      $parsedAddress = $null
      $validAddress = [System.Net.IPAddress]::TryParse($cameraIp, [ref]$parsedAddress) -and
        $parsedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork
      if (-not $validAddress) { Write-Host "Enter a valid IPv4 address, for example 192.168.1.20." -ForegroundColor Yellow }
    } until ($validAddress)
  }

  Write-Step "Saving setup and creating a desktop shortcut"
  New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null
  $settings = @{
    cameraIp = $cameraIp
    pythonPath = $python
    ffmpegDirectory = $ffmpegDirectory
    port = 8765
  }
  $settings | ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding UTF8
  $shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "ONVIF Camera Dashboard.lnk"
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($shortcutPath)
  $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installDirectory\launch_dashboard.ps1`""
  $shortcut.WorkingDirectory = $installDirectory
  $shortcut.Description = "Start the local ONVIF camera dashboard"
  $shortcut.Save()

  Write-Step "Starting the dashboard"
  & (Join-Path $installDirectory "launch_dashboard.ps1") -OpenBrowser
  Write-Host "`nSetup complete. Use the desktop shortcut to start the dashboard next time." -ForegroundColor Green
} catch {
  Write-Host "`nSetup failed: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host "No camera settings or credentials were uploaded. Fix the reported issue and run install.bat again."
  exit 1
} finally {
  if (Test-Path $workingDirectory) {
    Remove-Item -LiteralPath $workingDirectory -Recurse -Force -ErrorAction SilentlyContinue
  }
}
