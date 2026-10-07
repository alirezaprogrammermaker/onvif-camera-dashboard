# ONVIF Camera Dashboard

A lightweight local web dashboard for viewing and controlling IP cameras that expose ONVIF and RTSP. Built with Python's standard library and a small, dependency-free web UI.

## Features

- Live camera preview, selectable stream profiles, and still-frame capture.
- ONVIF device status and capability discovery.
- Short PTZ movement commands with adjustable speed.
- Local digital zoom for the browser preview.
- Read and update supported video encoder settings.
- Local MP4 recording with configurable segment duration (10-minute default).
- Relay controls with confirmation before activation.
- Read-only ONVIF/RTSP diagnostics.

Camera-specific support depends on its firmware. Audio streaming, optical zoom, imaging controls, tracking, presets, and relay operations may not be available. The dashboard reports advertised capabilities but cannot add features missing from a camera.

## Requirements

- Windows, macOS, or Linux with Python 3.10 or newer.
- FFmpeg on `PATH` for live preview, snapshots, and recording.
- An ONVIF/RTSP camera reachable on the local network.

No third-party Python packages are required. On Windows, the native folder picker also requires Python's `tkinter` component.

## Run

From PowerShell, replace `192.0.2.10` below with the camera's LAN IPv4 address:

```powershell
python .\camera_web.py --ip 192.0.2.10
```

Open <http://127.0.0.1:8765>. The server binds to localhost only. If the port is occupied, choose another local port:

```powershell
python .\camera_web.py --ip 192.0.2.10 --port 8766
```

Stop the dashboard with `Ctrl+C`. An active recording is finalized when the server shuts down normally.

## Windows one-click installer

Download [`install.bat`](https://raw.githubusercontent.com/alirezaprogrammermaker/onvif-camera-dashboard/main/install.bat) and double-click it. The installer:

1. Installs Python 3.12 and FFmpeg for the current Windows user using `winget`.
2. Downloads the latest project files and runs ONVIF WS-Discovery on the local network.
3. Starts the dashboard, opens it in the default browser, and creates a desktop shortcut.

If discovery finds exactly one ONVIF camera, setup uses it automatically. If it finds none or multiple devices, it asks for the camera's local IPv4 address. Windows Package Manager (App Installer) and an internet connection are required. No administrator access or Python packages are required. To remove the application, delete `%LOCALAPPDATA%\Programs\ONVIFCameraDashboard`, `%LOCALAPPDATA%\CameraDashboard`, and the desktop shortcut; Python and FFmpeg can be uninstalled from Windows Settings.

## Local recording

Open the collapsed **Recording settings** section to choose a destination folder and a segment length from 1 to 1440 minutes. The default is 10 minutes. Save the settings, then use **Start recording** in the player bar. Recording saves the camera's original video without re-encoding; segment edges depend on keyframes and may exceed the configured length slightly. The camera stream used during development had no audio track, so recordings do not contain audio.

Recording preferences are stored outside the project under `%LOCALAPPDATA%\CameraDashboard\settings.json` on Windows. Video files and local settings are excluded from version control.

## Diagnostics

Run the read-only discovery checks with the same camera address:

```powershell
python .\camera_diagnostics.py --ip 192.0.2.10
```

Use `--help` for available options. The diagnostic does not move the camera, activate a relay, or change its settings. More details about the tested device's generic capabilities and limitations are in [camera_network_readme.md](./camera_network_readme.md).

## Security

- Use this dashboard only on a trusted computer and network.
- The web server binds to `127.0.0.1`; do not change that to a public interface without adding authentication and appropriate request protections.
- Configure a strong camera password and disable anonymous access where supported.
- Do not forward ONVIF or RTSP ports directly to the internet.
- Streams may contain sensitive video. Choose a recording folder with appropriate access controls.

## License

No license is currently provided. All rights are reserved unless the project owner adds a license.
