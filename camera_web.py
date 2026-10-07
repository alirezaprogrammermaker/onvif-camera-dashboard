#!/usr/bin/env python3
"""Local web dashboard for an ONVIF/RTSP camera."""

import argparse
import copy
from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from camera_diagnostics import (
    DEVICE_IO_NS,
    DEVICE_NS,
    MEDIA_NS,
    PTZ_NS,
    Camera,
    descendants,
    first_text,
)


WEB_DIR = Path(__file__).resolve().parent / "web"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
STREAM_PORT = 554
MOVE_TIMEOUT = "PT0H0M0.300S"
MIN_PTZ_SPEED = 0.03
MAX_PTZ_SPEED = 0.15
DEFAULT_SEGMENT_MINUTES = 10
MIN_SEGMENT_MINUTES = 1
MAX_SEGMENT_MINUTES = 1440
JPEG_BOUNDARY = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


def app_settings_path():
    app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return app_data / "CameraDashboard" / "settings.json"


class RecordingManager:
    def __init__(self, settings_path):
        self.settings_path = Path(settings_path)
        self.lock = threading.RLock()
        self.process = None
        self.stopping = False
        self.stderr_thread = None
        self.stderr_tail = bytearray()
        self.started_at = None
        self.profile = None
        self.file_pattern = None
        self.last_error = None
        self.settings = self._load_settings()

    def _load_settings(self):
        defaults = {
            "folder": str(Path.home() / "Videos" / "CameraRecordings"),
            "segmentMinutes": DEFAULT_SEGMENT_MINUTES,
        }
        if not self.settings_path.exists():
            return defaults
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not read dashboard settings: {error}") from error
        if not isinstance(saved, dict):
            raise RuntimeError("Dashboard settings must contain a JSON object.")
        folder = saved.get("folder")
        minutes = saved.get("segmentMinutes")
        if not isinstance(folder, str) or not Path(folder).is_absolute():
            raise RuntimeError("Saved recording folder is not an absolute path.")
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, int)
            or not MIN_SEGMENT_MINUTES <= minutes <= MAX_SEGMENT_MINUTES
        ):
            raise RuntimeError("Saved recording segment duration is invalid.")
        return {"folder": folder, "segmentMinutes": minutes}

    def save_settings(self, folder, segment_minutes):
        if not isinstance(folder, str) or not folder.strip() or "\0" in folder:
            raise ValueError("یک پوشه‌ی معتبر برای ضبط انتخاب کنید.")
        path = Path(folder.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("مسیر پوشه باید کامل و مطلق باشد.")
        if isinstance(segment_minutes, bool) or not isinstance(segment_minutes, int):
            raise ValueError("مدت هر فایل باید عدد صحیح و برحسب دقیقه باشد.")
        if not MIN_SEGMENT_MINUTES <= segment_minutes <= MAX_SEGMENT_MINUTES:
            raise ValueError(
                f"مدت هر فایل باید بین {MIN_SEGMENT_MINUTES} و {MAX_SEGMENT_MINUTES} دقیقه باشد."
            )
        with self.lock:
            self._refresh_process_state()
            if self.process is not None or self.stopping:
                raise RuntimeError("هنگام ضبط نمی‌توان تنظیمات ضبط را تغییر داد.")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise ValueError("مسیر انتخاب‌شده پوشه نیست.")
            updated = {"folder": str(path.resolve()), "segmentMinutes": segment_minutes}
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            try:
                temporary.write_text(
                    json.dumps(updated, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary.replace(self.settings_path)
            except OSError:
                temporary.unlink(missing_ok=True)
                raise
            self.settings = updated
            return dict(self.settings)

    def status(self):
        with self.lock:
            self._refresh_process_state()
            recording = self.process is not None and self.process.poll() is None
            elapsed = int(time.monotonic() - self.started_at) if recording else 0
            return {
                "recording": recording,
                "folder": self.settings["folder"],
                "segmentMinutes": self.settings["segmentMinutes"],
                "profile": self.profile if recording else None,
                "startedAt": self.started_at,
                "elapsedSeconds": elapsed,
                "filePattern": self.file_pattern if recording else None,
                "error": self.last_error,
            }

    def _refresh_process_state(self):
        if self.process is None or self.process.poll() is None:
            return
        return_code = self.process.returncode
        self.process = None
        self.profile = None
        self.started_at = None
        if return_code:
            detail = self.stderr_tail.decode("utf-8", errors="replace").strip()
            self.last_error = detail or f"FFmpeg exited with code {return_code}."
        self.file_pattern = None

    def _drain_stderr(self, pipe):
        try:
            while True:
                chunk = pipe.read(1024)
                if not chunk:
                    break
                with self.lock:
                    self.stderr_tail.extend(chunk)
                    if len(self.stderr_tail) > 4096:
                        del self.stderr_tail[:-4096]
        finally:
            pipe.close()

    def start(self, uri, profile):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg در PATH پیدا نشد.")
        parsed = urlparse(uri)
        if parsed.scheme != "rtsp" or not parsed.hostname:
            raise ValueError("آدرس استریم دوربین معتبر نیست.")
        with self.lock:
            self._refresh_process_state()
            if self.process is not None or self.stopping:
                raise RuntimeError("ضبط در حال اجراست.")
            folder = Path(self.settings["folder"]).expanduser()
            if not folder.is_absolute():
                raise ValueError("پوشه‌ی مقصد ضبط باید یک مسیر کامل باشد.")
            folder.mkdir(parents=True, exist_ok=True)
            if not folder.is_dir():
                raise ValueError("پوشه‌ی مقصد ضبط معتبر نیست.")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            file_pattern = folder / f"camera_{stamp}_part%03d.mp4"
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "warning",
                "-rtsp_transport", "tcp", "-i", uri,
                "-map", "0:v:0", "-c", "copy",
                "-f", "segment",
                "-segment_time", str(self.settings["segmentMinutes"] * 60),
                "-segment_format", "mp4",
                "-reset_timestamps", "1",
                str(file_pattern),
            ]
            self.stderr_tail.clear()
            self.last_error = None
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except OSError as error:
                raise RuntimeError(f"شروع FFmpeg ناموفق بود: {error}") from error
            self.process = process
            self.profile = profile
            self.started_at = time.monotonic()
            self.file_pattern = file_pattern.name.replace("%03d", "###")
            self.stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(process.stderr,),
                daemon=True,
            )
            self.stderr_thread.start()
            time.sleep(0.3)
            if process.poll() is not None:
                self._refresh_process_state()
                detail = self.last_error or "FFmpeg could not open the camera stream."
                raise RuntimeError(detail)
            return self.status()

    def stop(self):
        with self.lock:
            self._refresh_process_state()
            process = self.process
            if process is None:
                return self.status()
            if self.stopping:
                raise RuntimeError("فرمان توقف ضبط در حال اجراست.")
            self.stopping = True
            try:
                if process.stdin:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            with self.lock:
                if process.stdin:
                    process.stdin.close()
                self._refresh_process_state()
                stderr_thread = self.stderr_thread
            if stderr_thread:
                stderr_thread.join(timeout=1)
            with self.lock:
                self.stderr_thread = None
                self.stopping = False
        return self.status()


def camera_call(ip, timeout, endpoint, namespace, operation, body=""):
    client = Camera(ip, timeout)
    root, error = client.soap(endpoint, namespace, operation, body)
    if error:
        raise RuntimeError(error)
    return root


def get_profiles(ip, timeout):
    root = camera_call(ip, timeout, "/Media", MEDIA_NS, "GetProfiles")
    profiles = []
    for profile in descendants(root, "Profiles"):
        token = profile.attrib.get("token", "")
        if not TOKEN_PATTERN.fullmatch(token):
            continue
        resolution = next(iter(descendants(profile, "Resolution")), None)
        width = first_text(resolution, "Width") if resolution is not None else "-"
        height = first_text(resolution, "Height") if resolution is not None else "-"
        encoder = next(iter(descendants(profile, "VideoEncoderConfiguration")), None)
        encoding = first_text(encoder, "Encoding") if encoder is not None else "-"
        fps = first_text(encoder, "FrameRateLimit") if encoder is not None else "-"
        encoding_interval = first_text(encoder, "EncodingInterval") if encoder is not None else "-"
        quality = first_text(encoder, "Quality") if encoder is not None else "-"
        rate_control = next(iter(descendants(encoder, "RateControl")), None) if encoder is not None else None
        bitrate = first_text(rate_control, "BitrateLimit") if rate_control is not None else "-"
        h264 = next(iter(descendants(encoder, "H264")), None) if encoder is not None else None
        gop = first_text(h264, "GovLength") if h264 is not None else "-"
        h264_profile = first_text(h264, "H264Profile") if h264 is not None else "-"
        encoder_token = encoder.attrib.get("token") if encoder is not None else None
        ptz = next(iter(descendants(profile, "PTZConfiguration")), None)
        profiles.append({
            "token": token,
            "name": first_text(profile, "Name", token),
            "width": width,
            "height": height,
            "encoding": encoding,
            "fps": fps,
            "encodingInterval": encoding_interval,
            "quality": quality,
            "bitrate": bitrate,
            "gop": gop,
            "h264Profile": h264_profile,
            "encoderToken": encoder_token,
            "ptzToken": ptz.attrib.get("token") if ptz is not None else None,
        })
    return profiles


def integer_range(node, name):
    item = next(iter(descendants(node, name)), None)
    if item is None:
        return None
    minimum = first_text(item, "Min", "")
    maximum = first_text(item, "Max", "")
    try:
        return {"min": int(minimum), "max": int(maximum)}
    except ValueError:
        return None


def read_encoder_settings(ip, timeout):
    profiles = get_profiles(ip, timeout)
    root = camera_call(ip, timeout, "/Media", MEDIA_NS, "GetVideoEncoderConfigurations")
    configurations = {
        item.attrib["token"]: item
        for item in descendants(root, "Configurations")
        if item.attrib.get("token")
    }
    settings = []
    for profile in profiles:
        token = profile.get("encoderToken")
        config = configurations.get(token)
        if config is None:
            continue
        option_body = f"<m:ConfigurationToken>{escape(token)}</m:ConfigurationToken>"
        options_root = camera_call(
            ip, timeout, "/Media", MEDIA_NS,
            "GetVideoEncoderConfigurationOptions", option_body,
        )
        options = next(iter(descendants(options_root, "Options")), None)
        if options is None:
            continue
        h264_options = next(iter(descendants(options, "H264")), None)
        resolutions = []
        if h264_options is not None:
            for item in descendants(h264_options, "ResolutionsAvailable"):
                try:
                    resolutions.append({
                        "width": int(first_text(item, "Width", "")),
                        "height": int(first_text(item, "Height", "")),
                    })
                except ValueError:
                    continue
        config_resolution = next(iter(descendants(config, "Resolution")), None)
        rate_control = next(iter(descendants(config, "RateControl")), None)
        h264 = next(iter(descendants(config, "H264")), None)
        settings.append({
            "profileToken": profile["token"],
            "profileName": profile["name"],
            "encoderToken": token,
            "encoding": first_text(config, "Encoding"),
            "h264Profile": first_text(h264, "H264Profile") if h264 is not None else "-",
            "resolution": {
                "width": int(first_text(config_resolution, "Width", "0")),
                "height": int(first_text(config_resolution, "Height", "0")),
            } if config_resolution is not None else None,
            "frameRate": int(first_text(rate_control, "FrameRateLimit", "0")),
            "encodingInterval": int(first_text(rate_control, "EncodingInterval", "0")),
            "quality": int(first_text(config, "Quality", "0")),
            "bitrate": int(first_text(rate_control, "BitrateLimit", "0")),
            "gop": int(first_text(h264, "GovLength", "0")),
            "ranges": {
                "frameRate": integer_range(options, "FrameRateRange"),
                "encodingInterval": integer_range(options, "EncodingIntervalRange"),
                "quality": integer_range(options, "QualityRange"),
                "gop": integer_range(options, "GovLengthRange"),
            },
            "resolutions": resolutions,
        })
    return settings


def update_encoder_settings(ip, timeout, data):
    profile_token = data.get("profileToken", "")
    if not isinstance(profile_token, str) or not TOKEN_PATTERN.fullmatch(profile_token):
        raise ValueError("Invalid profile.")
    settings = read_encoder_settings(ip, timeout)
    profile = next((item for item in settings if item["profileToken"] == profile_token), None)
    if profile is None:
        raise ValueError("Encoder settings for this profile are not available.")
    root = camera_call(ip, timeout, "/Media", MEDIA_NS, "GetVideoEncoderConfigurations")
    config = next(
        (
            item for item in descendants(root, "Configurations")
            if item.attrib.get("token") == profile["encoderToken"]
        ),
        None,
    )
    if config is None:
        raise RuntimeError("Camera encoder configuration was not returned.")
    config = copy.deepcopy(config)
    config.tag = f"{{{MEDIA_NS}}}Configuration"

    def bounded_integer(key, range_key):
        value = data.get(key)
        bounds = profile["ranges"].get(range_key)
        if isinstance(value, bool) or not isinstance(value, int) or bounds is None:
            raise ValueError(f"Invalid {key} value.")
        if not bounds["min"] <= value <= bounds["max"]:
            raise ValueError(f"{key} must be between {bounds['min']} and {bounds['max']}.")
        return value

    resolution = data.get("resolution")
    if (
        not isinstance(resolution, dict)
        or isinstance(resolution.get("width"), bool)
        or isinstance(resolution.get("height"), bool)
        or not isinstance(resolution.get("width"), int)
        or not isinstance(resolution.get("height"), int)
        or resolution not in profile["resolutions"]
    ):
        raise ValueError("Resolution must be one of the options reported by the camera.")
    frame_rate = bounded_integer("frameRate", "frameRate")
    quality = bounded_integer("quality", "quality")
    gop = bounded_integer("gop", "gop")

    resolution_element = next(iter(descendants(config, "Resolution")), None)
    if resolution_element is None:
        raise RuntimeError("Encoder resolution cannot be changed on this profile.")
    next(item for item in descendants(resolution_element, "Width")).text = str(resolution["width"])
    next(item for item in descendants(resolution_element, "Height")).text = str(resolution["height"])
    rate_control = next(iter(descendants(config, "RateControl")), None)
    frame_rate_element = next(iter(descendants(rate_control, "FrameRateLimit")), None) if rate_control is not None else None
    if frame_rate_element is None:
        raise RuntimeError("Frame rate cannot be changed on this profile.")
    frame_rate_element.text = str(frame_rate)
    quality_element = next(iter(descendants(config, "Quality")), None)
    if quality_element is None:
        raise RuntimeError("Image quality cannot be changed on this profile.")
    quality_element.text = str(quality)
    h264 = next(iter(descendants(config, "H264")), None)
    gop_element = next(iter(descendants(h264, "GovLength")), None) if h264 is not None else None
    if gop_element is None:
        raise RuntimeError("GOP length cannot be changed on this profile.")
    gop_element.text = str(gop)

    configuration_xml = ET.tostring(config, encoding="unicode")
    body = (
        f"{configuration_xml}"
        "<m:ForcePersistence>true</m:ForcePersistence>"
    )
    camera_call(ip, timeout, "/Media", MEDIA_NS, "SetVideoEncoderConfiguration", body)
    verification_root = camera_call(
        ip, timeout, "/Media", MEDIA_NS, "GetVideoEncoderConfigurations"
    )
    applied = next(
        (
            item for item in descendants(verification_root, "Configurations")
            if item.attrib.get("token") == profile["encoderToken"]
        ),
        None,
    )
    if applied is None:
        raise RuntimeError("Camera did not return the updated encoder configuration.")
    applied_resolution = next(iter(descendants(applied, "Resolution")), None)
    applied_rate = next(iter(descendants(applied, "RateControl")), None) if applied is not None else None
    applied_h264 = next(iter(descendants(applied, "H264")), None) if applied is not None else None
    actual = {
        "resolution": {
            "width": int(first_text(applied_resolution, "Width", "0")),
            "height": int(first_text(applied_resolution, "Height", "0")),
        } if applied_resolution is not None else None,
        "frameRate": int(first_text(applied_rate, "FrameRateLimit", "0")),
        "quality": int(first_text(applied, "Quality", "0")),
        "gop": int(first_text(applied_h264, "GovLength", "0")),
    }
    expected = {
        "resolution": resolution,
        "frameRate": frame_rate,
        "quality": quality,
        "gop": gop,
    }
    if actual != expected:
        raise RuntimeError(
            f"Camera did not apply all requested settings; read-back: {actual}."
        )
    return "تنظیمات تصویر ذخیره شد."


def move_speed(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid PTZ speed.")
    if not MIN_PTZ_SPEED <= value <= MAX_PTZ_SPEED:
        raise ValueError(
            f"PTZ speed must be between {MIN_PTZ_SPEED:.2f} and {MAX_PTZ_SPEED:.2f}."
        )
    return float(value)


def camera_status(ip, timeout):
    data = {"ip": ip, "connected": False, "profiles": [], "features": {}}
    errors = []
    try:
        root = camera_call(ip, timeout, "/device_service", DEVICE_NS, "GetDeviceInformation")
        data["device"] = {
            "manufacturer": first_text(root, "Manufacturer"),
            "model": first_text(root, "Model"),
            "firmware": first_text(root, "FirmwareVersion"),
            "serial": first_text(root, "SerialNumber"),
        }
        data["connected"] = True
    except (RuntimeError, OSError) as error:
        errors.append(f"Device information: {error}")

    try:
        root = camera_call(ip, timeout, "/device_service", DEVICE_NS, "GetSystemDateAndTime")
        date = next(iter(descendants(root, "LocalDateTime")), None)
        if date is not None:
            data["cameraTime"] = " ".join(
                (first_text(date, "Year"), first_text(date, "Month"),
                 first_text(date, "Day"), first_text(date, "Hour"),
                 first_text(date, "Minute"), first_text(date, "Second"))
            )
    except (RuntimeError, OSError) as error:
        errors.append(f"Camera clock: {error}")

    try:
        data["profiles"] = get_profiles(ip, timeout)
        for profile in data["profiles"]:
            try:
                body = (
                    "<m:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                    "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                    f"</m:StreamSetup><m:ProfileToken>{escape(profile['token'])}</m:ProfileToken>"
                )
                root = camera_call(ip, timeout, "/Media", MEDIA_NS, "GetStreamUri", body)
                uri = first_text(root, "Uri", "")
                parsed = urlparse(uri)
                if (
                    parsed.scheme == "rtsp"
                    and parsed.hostname == ip
                    and (parsed.port is None or parsed.port == STREAM_PORT)
                    and parsed.username is None
                    and parsed.password is None
                ):
                    profile["uri"] = uri
                else:
                    errors.append(f"Invalid RTSP URI returned for profile {profile['token']}")
            except (RuntimeError, OSError) as error:
                errors.append(f"Stream URI ({profile['token']}): {error}")
    except (RuntimeError, OSError) as error:
        errors.append(f"Media profiles: {error}")

    try:
        root = camera_call(
            ip, timeout, "/device_service", DEVICE_NS, "GetCapabilities",
            "<m:Category>All</m:Category>",
        )
        data["features"] = {
            "ptzAdvertised": any(
                bool(descendants(section, "XAddr"))
                for section in descendants(root, "PTZ")
            ),
            "relayOutputsAdvertised": first_text(root, "RelayOutputs", "0"),
            "audioSourcesAdvertised": first_text(root, "AudioSources", "0"),
        }
    except (RuntimeError, OSError) as error:
        errors.append(f"Capabilities: {error}")

    try:
        root = camera_call(ip, timeout, "/Media", MEDIA_NS, "GetAudioSources")
        data["audioChannels"] = [
            channel.text or "0" for channel in descendants(root, "Channels")
        ]
    except (RuntimeError, OSError) as error:
        data["audioError"] = str(error)

    if errors:
        data["errors"] = errors
    return data


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CameraDashboard/1.0"

    def log_message(self, format_string, *args):
        return

    @property
    def app(self):
        return self.server.app

    def send_bytes(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, payload):
        self.send_bytes(
            status,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self.send_json(200, camera_status(self.app.ip, self.app.timeout))
            return
        if parsed.path == "/api/settings":
            try:
                self.send_json(200, {"profiles": read_encoder_settings(self.app.ip, self.app.timeout)})
            except (RuntimeError, OSError, ValueError) as error:
                self.send_json(502, {"error": str(error)})
            return
        if parsed.path == "/api/recording":
            self.send_json(200, self.app.recording.status())
            return
        if parsed.path == "/api/stream":
            self.stream(parse_qs(parsed.query).get("profile", [""])[0])
            return
        if parsed.path == "/api/snapshot":
            self.snapshot(parse_qs(parsed.query).get("profile", [""])[0])
            return
        if parsed.path == "/":
            target = WEB_DIR / "index.html"
        elif parsed.path in ("/app.css", "/app.js"):
            target = WEB_DIR / parsed.path[1:]
        else:
            self.send_json(404, {"error": "Not found"})
            return
        try:
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
            }[target.suffix]
            self.send_bytes(200, content_type, target.read_bytes())
        except OSError as error:
            self.send_json(500, {"error": str(error)})

    def read_json(self):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if (
            self.headers.get("Host") != expected_host
            or self.headers.get("Origin") != f"http://{expected_host}"
        ):
            raise ValueError("Request origin was rejected.")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 2048:
            raise ValueError("Invalid request body.")
        try:
            data = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Expected a JSON request body.") from error
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def do_POST(self):
        try:
            data = self.read_json()
            if self.path == "/api/ptz":
                result = self.ptz(data)
            elif self.path == "/api/relay":
                result = self.relay(data)
            elif self.path == "/api/settings":
                if self.app.recording.status()["recording"]:
                    raise RuntimeError("برای تغییر تنظیمات دوربین ابتدا ضبط را متوقف کنید.")
                result = update_encoder_settings(self.app.ip, self.app.timeout, data)
            elif self.path == "/api/recording/settings":
                result = self.app.recording.save_settings(
                    data.get("folder"),
                    data.get("segmentMinutes"),
                )
                self.send_json(200, {"ok": True, "settings": result})
                return
            elif self.path == "/api/recording/start":
                profile = data.get("profile", "")
                if not isinstance(profile, str) or not TOKEN_PATTERN.fullmatch(profile):
                    raise ValueError("پروفایل استریم معتبر نیست.")
                uri = self.stream_uri(profile)
                result = self.app.recording.start(uri, profile)
                self.send_json(200, {"ok": True, "status": result})
                return
            elif self.path == "/api/recording/stop":
                result = self.app.recording.stop()
                self.send_json(200, {"ok": True, "status": result})
                return
            elif self.path == "/api/recording/browse":
                result = self.choose_recording_folder()
                self.send_json(200, {"ok": True, "folder": result})
                return
            else:
                self.send_json(404, {"error": "Not found"})
                return
            self.send_json(200, {"ok": True, "message": result})
        except (ValueError, RuntimeError, OSError) as error:
            self.send_json(400, {"ok": False, "error": str(error)})

    def choose_recording_folder(self):
        import tkinter
        from tkinter import filedialog

        with self.app.dialog_lock:
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                return filedialog.askdirectory(
                    parent=root,
                    title="پوشه‌ی ذخیره‌ی ویدئوها",
                    initialdir=self.app.recording.status()["folder"],
                    mustexist=True,
                )
            finally:
                root.destroy()

    def ptz(self, data):
        action = data.get("action")
        profile = data.get("profile", "")
        if not TOKEN_PATTERN.fullmatch(profile):
            raise ValueError("Invalid profile.")
        if action == "stop":
            body = (
                f"<m:ProfileToken>{escape(profile)}</m:ProfileToken>"
                "<m:PanTilt>true</m:PanTilt><m:Zoom>true</m:Zoom>"
            )
            operation = "Stop"
        elif action == "move":
            direction = data.get("direction")
            speed = move_speed(data.get("speed", 0.06))
            vectors = {
                "left": (speed, 0.0),
                "right": (-speed, 0.0),
                "up": (0.0, speed),
                "down": (0.0, -speed),
            }
            if direction not in vectors:
                raise ValueError("Unsupported PTZ direction.")
            x_value, y_value = vectors[direction]
            body = (
                f"<m:ProfileToken>{escape(profile)}</m:ProfileToken><m:Velocity>"
                f'<tt:PanTilt x="{x_value}" y="{y_value}"/>'
                f"</m:Velocity><m:Timeout>{MOVE_TIMEOUT}</m:Timeout>"
            )
            operation = "ContinuousMove"
        else:
            raise ValueError("Unsupported PTZ action.")
        camera_call(self.app.ip, self.app.timeout, "/PTZ", PTZ_NS, operation, body)
        return "فرمان حرکت ارسال شد." if action == "move" else "فرمان توقف ارسال شد."

    def relay(self, data):
        state = data.get("state")
        if state not in ("active", "inactive"):
            raise ValueError("Relay state must be active or inactive.")
        token = data.get("token", "RelayOutput0")
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("Invalid relay token.")
        body = (
            f"<m:RelayOutputToken>{escape(token)}</m:RelayOutputToken>"
            f"<m:LogicalState>{'true' if state == 'active' else 'false'}</m:LogicalState>"
        )
        camera_call(
            self.app.ip, self.app.timeout, "/DeviceIO", DEVICE_IO_NS,
            "SetRelayOutputState", body,
        )
        return "وضعیت رله تغییر کرد."

    def stream_uri(self, token):
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("Invalid stream profile.")
        matching = [profile for profile in get_profiles(self.app.ip, self.app.timeout)
                    if profile["token"] == token]
        if not matching:
            raise ValueError("Stream profile not found.")
        profile = matching[0]
        body = (
            "<m:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            f"</m:StreamSetup><m:ProfileToken>{escape(token)}</m:ProfileToken>"
        )
        root = camera_call(self.app.ip, self.app.timeout, "/Media", MEDIA_NS, "GetStreamUri", body)
        uri = first_text(root, "Uri", "")
        parsed = urlparse(uri)
        if (
            parsed.scheme != "rtsp"
            or parsed.hostname != self.app.ip
            or (parsed.port is not None and parsed.port != STREAM_PORT)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise RuntimeError("Camera returned an invalid stream URL.")
        return uri

    def stream(self, token):
        try:
            uri = self.stream_uri(token)
        except (RuntimeError, ValueError, OSError) as error:
            self.send_json(400, {"error": str(error)})
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.send_json(503, {"error": "FFmpeg was not found on PATH."})
            return
        try:
            process = subprocess.Popen(
                [
                    ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-i", uri, "-an",
                    "-vf", "fps=8,scale='min(960,iw)':-2",
                    "-q:v", "7", "-f", "mjpeg", "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self.send_json(500, {"error": f"Unable to start FFmpeg: {error}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        buffer = bytearray()
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    start = buffer.find(JPEG_BOUNDARY)
                    if start < 0:
                        if len(buffer) > 4:
                            del buffer[:-1]
                        break
                    end = buffer.find(JPEG_END, start + 2)
                    if end < 0:
                        if start:
                            del buffer[:start]
                        break
                    frame_end = end + 2
                    frame = bytes(buffer[start:frame_end])
                    del buffer[:frame_end]
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode("ascii")
                        + b"\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                    self.wfile.flush()
                if len(buffer) > 12_000_000:
                    raise RuntimeError("Video frame exceeded the size limit.")
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout:
                process.stdout.close()

    def snapshot(self, token):
        try:
            uri = self.stream_uri(token)
        except (RuntimeError, ValueError, OSError) as error:
            self.send_json(400, {"error": str(error)})
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.send_json(503, {"error": "FFmpeg was not found on PATH."})
            return
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-i", uri, "-frames:v", "1",
                    "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(10, self.app.timeout * 3),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Snapshot capture timed out.") from error
        if result.returncode or not result.stdout.startswith(JPEG_BOUNDARY):
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or "Unable to capture a frame.")
        self.send_bytes(200, "image/jpeg", result.stdout)


class CameraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, ip, timeout):
        super().__init__(address, handler)
        self.app = argparse.Namespace(
            ip=ip,
            timeout=timeout,
            recording=RecordingManager(app_settings_path()),
            dialog_lock=threading.Lock(),
        )

    def server_close(self):
        self.app.recording.stop()
        super().server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True, help="camera IPv4 address")
    parser.add_argument("--port", type=int, default=8765, help="local dashboard port")
    parser.add_argument("--timeout", type=float, default=5.0, help="ONVIF request timeout")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    server = CameraHTTPServer(("127.0.0.1", args.port), DashboardHandler, args.ip, args.timeout)
    print(f"Camera dashboard: http://127.0.0.1:{args.port}")
    print("Bound to localhost only; press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping camera dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
