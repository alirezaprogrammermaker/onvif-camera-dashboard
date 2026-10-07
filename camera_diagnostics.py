#!/usr/bin/env python3
"""Read-only ONVIF and RTSP checks for an IP camera."""

import argparse
import http.client
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlparse


SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
DEVICE_IO_NS = "http://www.onvif.org/ver10/deviceIO/wsdl"


def local_name(node):
    return node.tag.rsplit("}", 1)[-1]


def descendants(node, name):
    return [item for item in node.iter() if local_name(item) == name]


def first_text(node, name, default="-"):
    found = descendants(node, name)
    return found[0].text if found and found[0].text else default


class Camera:
    def __init__(self, ip, timeout):
        self.ip = ip
        self.timeout = timeout
        self.base = f"http://{ip}:8899/onvif"
        self.results = []
        self.profiles = []

    def report(self, status, label, detail=""):
        line = f"[{status}] {label}"
        if detail:
            line += f": {detail}"
        print(line)
        self.results.append(status)

    def soap(self, endpoint, namespace, operation, body=""):
        envelope = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:m="{namespace}" '
            f'xmlns:tt="{SCHEMA_NS}"><s:Body>'
            f"<m:{operation}>{body}</m:{operation}>"
            f"</s:Body></s:Envelope>"
        ).encode("utf-8")
        connection = http.client.HTTPConnection(
            self.ip, 8899, timeout=self.timeout
        )
        try:
            connection.request(
                "POST",
                endpoint,
                body=envelope,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{namespace}/{operation}"',
                },
            )
            response = connection.getresponse()
            payload = response.read()
            try:
                root = ET.fromstring(payload)
            except ET.ParseError:
                return None, f"HTTP {response.status}; invalid XML response"
            fault = descendants(root, "faultstring")
            if fault:
                return None, f"HTTP {response.status}; {fault[0].text or 'SOAP fault'}"
            if response.status < 200 or response.status >= 300:
                return None, f"HTTP {response.status}"
            return root, None
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            return None, str(error)
        finally:
            connection.close()

    def check(self, endpoint, namespace, operation, body="", unsupported=False):
        root, error = self.soap(endpoint, namespace, operation, body)
        if error:
            status = "UNSUPPORTED" if unsupported and "not implemented" in error.lower() else "FAIL"
            self.report(status, operation, error)
            return None
        self.report("PASS", operation)
        return root

    def run_onvif(self):
        device = "/device_service"
        media = "/Media"
        ptz = "/PTZ"
        device_io = "/DeviceIO"

        info = self.check(device, DEVICE_NS, "GetDeviceInformation")
        if info is not None:
            values = {
                name: first_text(info, name)
                for name in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")
            }
            self.report("INFO", "Device", ", ".join(f"{k}={v}" for k, v in values.items()))

        capabilities = self.check(
            device, DEVICE_NS, "GetCapabilities", "<m:Category>All</m:Category>"
        )
        if capabilities is not None:
            for service in ("Device", "Media", "PTZ", "DeviceIO"):
                section = next(
                    (item for item in descendants(capabilities, service)
                     if descendants(item, "XAddr")),
                    None,
                )
                if section is not None:
                    self.report(
                        "INFO",
                        f"ONVIF {service} service",
                        first_text(section, "XAddr"),
                    )
            self.report(
                "INFO",
                "Advertised media I/O",
                f"video sources={first_text(capabilities, 'VideoSources')}, "
                f"audio sources={first_text(capabilities, 'AudioSources')}, "
                f"audio outputs={first_text(capabilities, 'AudioOutputs')}, "
                f"relay outputs={first_text(capabilities, 'RelayOutputs')}",
            )

        services = self.check(
            device, DEVICE_NS, "GetServices", "<m:IncludeCapability>false</m:IncludeCapability>"
        )
        if services is not None:
            for service in descendants(services, "Service"):
                namespace = first_text(service, "Namespace")
                version = next(
                    (v for v in descendants(service, "Version")), None
                )
                if namespace != "-" and version is not None:
                    parts = namespace.rstrip("/").split("/")
                    service_name = parts[-2] if len(parts) > 1 else namespace
                    self.report(
                        "INFO",
                        "Service version",
                        f"{service_name} "
                        f"{first_text(version, 'Major')}.{first_text(version, 'Minor')}",
                    )

        clock = self.check(device, DEVICE_NS, "GetSystemDateAndTime")
        if clock is not None:
            self.report("INFO", "Camera clock", " ".join(
                (first_text(clock, "Year"), first_text(clock, "Month"),
                 first_text(clock, "Day"), first_text(clock, "Hour"),
                 first_text(clock, "Minute"), first_text(clock, "Second"))
            ))

        profiles = self.check(media, MEDIA_NS, "GetProfiles")
        ptz_node_tokens = []
        if profiles is not None:
            for profile in descendants(profiles, "Profiles"):
                token = profile.attrib.get("token")
                if not token:
                    continue
                width = first_text(profile, "Width")
                height = first_text(profile, "Height")
                fps = first_text(profile, "FrameRateLimit")
                encoding = first_text(profile, "Encoding")
                self.profiles.append((token, profile))
                self.report(
                    "INFO",
                    f"Profile {token}",
                    f"{encoding} {width}x{height} at {fps} fps",
                )

        sources = self.check(media, MEDIA_NS, "GetVideoSources")
        if sources is not None:
            self.report("INFO", "Video sources", str(len(descendants(sources, "VideoSources"))))
        audio = self.check(media, MEDIA_NS, "GetAudioSources")
        if audio is not None:
            self.report("INFO", "Audio source channels", ", ".join(
                item.text or "0" for item in descendants(audio, "Channels")
            ) or "none")

        stream_uris = []
        for token, profile in self.profiles:
            setup = (
                "<m:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                f"</m:StreamSetup><m:ProfileToken>{token}</m:ProfileToken>"
            )
            uri_result = self.check(media, MEDIA_NS, "GetStreamUri", setup)
            if uri_result is not None:
                uri = first_text(uri_result, "Uri", "")
                if uri:
                    stream_uris.append((token, uri))
                    self.report("INFO", f"RTSP URI {token}", uri)

            for config in descendants(profile, "PTZConfiguration"):
                config_token = config.attrib.get("token")
                if config_token:
                    node_token = first_text(config, "NodeToken", "")
                    if node_token and node_token not in ptz_node_tokens:
                        ptz_node_tokens.append(node_token)
                    config_result = self.check(
                        ptz, PTZ_NS, "GetConfiguration",
                        f"<m:PTZConfigurationToken>{config_token}</m:PTZConfigurationToken>",
                    )
                    if config_result is not None:
                        options = self.check(
                            ptz, PTZ_NS, "GetConfigurationOptions",
                            f"<m:ConfigurationToken>{config_token}</m:ConfigurationToken>",
                        )
                        if options is not None:
                            pan_tilt = next(
                                (item for item in descendants(options, "ContinuousPanTiltVelocitySpace")
                                 if descendants(item, "XRange")),
                                None,
                            )
                            zoom = next(
                                (item for item in descendants(options, "ContinuousZoomVelocitySpace")
                                 if descendants(item, "XRange")),
                                None,
                            )
                            timeout = next(
                                (item for item in descendants(options, "PTZTimeout")),
                                None,
                            )
                            range_text = lambda node: (
                                f"{first_text(node, 'Min')}..{first_text(node, 'Max')}"
                                if node is not None else "not reported"
                            )
                            self.report(
                                "INFO",
                                f"PTZ {token} options",
                                f"pan/tilt X={range_text(pan_tilt)}; "
                                f"zoom={range_text(zoom)}; "
                                f"timeout={range_text(timeout)}",
                            )
                    break

        # These are safe queries: they never move the camera or change settings.
        self.check(ptz, PTZ_NS, "GetNodes", unsupported=True)
        for node_token in ptz_node_tokens:
            self.check(
                ptz, PTZ_NS, "GetNode",
                f"<m:NodeToken>{node_token}</m:NodeToken>", unsupported=True,
            )
        for token, _ in self.profiles:
            self.check(
                ptz, PTZ_NS, "GetPresets",
                f"<m:ProfileToken>{token}</m:ProfileToken>", unsupported=True,
            )
            self.check(
                ptz, PTZ_NS, "GetStatus",
                f"<m:ProfileToken>{token}</m:ProfileToken>", unsupported=True,
            )

        self.check(media, MEDIA_NS, "GetSnapshotUri",
                   f"<m:ProfileToken>{self.profiles[0][0]}</m:ProfileToken>" if self.profiles else "",
                   unsupported=True)
        for operation in ("GetVideoSources", "GetAudioSources", "GetRelayOutputs", "GetDigitalInputs"):
            self.check(device_io, DEVICE_IO_NS, operation, unsupported=True)
        self.check(
            device_io, DEVICE_IO_NS, "GetRelayOutputOptions",
            "<tt:RelayOutputToken>RelayOutput0</tt:RelayOutputToken>",
            unsupported=True,
        )
        self.check(media, MEDIA_NS, "GetServiceCapabilities", unsupported=True)

        return stream_uris

    def run_rtsp(self, uri):
        parsed = urlparse(uri)
        if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
            self.report("FAIL", "RTSP URI", f"invalid URI: {uri}")
            return
        port = parsed.port or 554
        path = parsed.path or "/"
        try:
            with socket.create_connection((parsed.hostname, port), self.timeout) as sock:
                sock.settimeout(self.timeout)
                request = (
                    f"OPTIONS {uri} RTSP/1.0\r\nCSeq: 1\r\n\r\n"
                ).encode("ascii")
                sock.sendall(request)
                reply = sock.recv(4096).decode("latin-1", errors="replace")
            status = reply.splitlines()[0] if reply else "empty response"
            if "200" in status:
                public = next((line.split(":", 1)[1].strip()
                               for line in reply.splitlines()
                               if line.lower().startswith("public:")), "not listed")
                self.report("PASS", f"RTSP OPTIONS {path}", f"{status}; methods={public}")
            else:
                self.report("FAIL", f"RTSP OPTIONS {path}", status)
        except (OSError, TimeoutError) as error:
            self.report("FAIL", f"RTSP OPTIONS {path}", str(error))
            return

        try:
            with socket.create_connection((parsed.hostname, port), self.timeout) as sock:
                sock.settimeout(self.timeout)
                request = (
                    f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: 2\r\n"
                    "Accept: application/sdp\r\n\r\n"
                ).encode("ascii")
                sock.sendall(request)
                response = b""
                while len(response) < 16384:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    if b"\r\n\r\n" in response:
                        headers, body = response.split(b"\r\n\r\n", 1)
                        content_length = 0
                        for line in headers.decode("latin-1").splitlines():
                            if line.lower().startswith("content-length:"):
                                content_length = int(line.split(":", 1)[1].strip())
                        if len(body) >= content_length:
                            break
            text = response.decode("latin-1", errors="replace")
            first_line = text.splitlines()[0] if text else "empty response"
            codec = next((line.split(" ", 1)[1] for line in text.splitlines()
                          if line.startswith("a=rtpmap:")), "not listed")
            if "200" in first_line:
                self.report("PASS", f"RTSP DESCRIBE {path}", f"{first_line}; codec={codec}")
            else:
                self.report("FAIL", f"RTSP DESCRIBE {path}", first_line)
        except (OSError, TimeoutError, ValueError) as error:
            self.report("FAIL", f"RTSP DESCRIBE {path}", str(error))
            return

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.report("SKIP", f"Decode frame {path}", "ffmpeg not found on PATH")
            return
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-i", uri,
                    "-frames:v", "1", "-f", "null", "-",
                ],
                capture_output=True,
                text=True,
                timeout=max(self.timeout * 3, 12),
                check=False,
            )
            if result.returncode == 0:
                self.report("PASS", f"Decode frame {path}", "one frame decoded")
            else:
                detail = result.stderr.strip().splitlines()
                self.report("FAIL", f"Decode frame {path}",
                            detail[-1] if detail else f"ffmpeg exit {result.returncode}")
        except subprocess.TimeoutExpired:
            self.report("FAIL", f"Decode frame {path}", "ffmpeg timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True, help="camera IPv4 address")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    args = parser.parse_args()

    camera = Camera(args.ip, args.timeout)
    uris = camera.run_onvif()
    for _, uri in uris:
        camera.run_rtsp(uri)

    print("\nPTZ movement and relay activation were not tested; this diagnostic is read-only.")
    print(
        f"Summary: {camera.results.count('PASS')} passed, "
        f"{camera.results.count('UNSUPPORTED')} unsupported, "
        f"{camera.results.count('FAIL')} failed, "
        f"{camera.results.count('SKIP')} skipped."
    )
    return 1 if "FAIL" in camera.results else 0


if __name__ == "__main__":
    sys.exit(main())
