# Camera Capability Notes

This document records representative results from testing one ONVIF-compatible IP camera. Device-specific addresses, serial numbers, and identifying details are intentionally omitted. Camera firmware varies, so treat these results as examples rather than guarantees.

## Tested video streams

Two H.264 media profiles were discovered:

| Example profile | RTSP path | Resolution | Frame rate | Bitrate limit | H.264 profile |
|---|---|---:|---:|---:|---|
| `stream0_0` | `/live/ch00_1` | 1280x720 | 15 fps | 1400 kbps | High |
| `stream0_1` | `/live/ch00_0` | 640x480 | 25 fps | 640 kbps | Main |

Both profiles reported quality 5 and GOP length 60. RTSP `OPTIONS` and `DESCRIBE` succeeded; SDP identified H.264 video (`H264/90000`). FFmpeg opened both streams and decoded a frame.

The tested camera advertised RTP multicast, RTP over TCP, and RTP/RTSP over TCP. PTZ configuration options were readable, and short pan/tilt commands were accepted. Zoom commands were accepted but did not visibly change the image. Device I/O advertised a relay, but relay state and control methods were not implemented by this firmware; no relay was actuated.

No usable audio was confirmed: there were no reported audio sources or outputs, no audio encoder attached to the video profiles, and no audio track in RTSP SDP. Events, imaging, and analytics services were not listed. In this tested firmware, microphone/speaker streaming, camera-side exposure controls, and automatic subject tracking were unavailable through ONVIF.

## Dashboard

The dashboard provides live MJPEG preview, snapshots, short PTZ movements, local preview zoom, relay controls, and read/write video-encoder settings where supported by the camera. Camera settings are validated against ranges reported by the device and read back after writes.

Local recording writes the original video stream to timestamped MP4 segments without re-encoding. The default segment length is 10 minutes and is configurable from 1 to 1440 minutes in the dashboard's recording settings. Segment boundaries depend on camera keyframes, so a segment may run slightly longer than the configured duration. Since the tested camera's RTSP stream has no audio, these recordings are video-only.

The dashboard binds to loopback (`127.0.0.1`) and does not save camera credentials. RTSP and ONVIF access may nevertheless be unauthenticated on some cameras; configure camera access controls and do not expose camera ports directly to the internet.

## Repeatable diagnostic

Run the read-only ONVIF and RTSP checks, replacing the example address with the camera's LAN IPv4 address:

```powershell
python .\camera_diagnostics.py --ip 192.0.2.10
```

The diagnostic reports unsupported methods separately from failures. It does not move the camera, activate relays, or change settings. If FFmpeg is on `PATH`, it also tries decoding one frame from each returned stream.

On Windows, `install.bat` installs Python and FFmpeg, downloads the dashboard, attempts ONVIF WS-Discovery and a bounded scan of local subnets up to 1024 addresses on port 8899, then starts the app. If automatic discovery cannot uniquely identify a camera, it asks for its LAN IPv4 address.
