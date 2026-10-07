@echo off
setlocal
set "INSTALLER=%TEMP%\onvif-camera-dashboard-install.ps1"
echo Downloading the ONVIF Camera Dashboard installer...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/alirezaprogrammermaker/onvif-camera-dashboard/main/install.ps1' -OutFile '%INSTALLER%' } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
  echo.
  echo Could not download the installer. Check your internet connection and try again.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%"
set "RESULT=%ERRORLEVEL%"
del "%INSTALLER%" >nul 2>nul
if not "%RESULT%"=="0" (
  echo.
  echo Installation did not complete. Read the error above and try again.
  pause
)
exit /b %RESULT%
