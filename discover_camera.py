#!/usr/bin/env python3
"""Discover ONVIF cameras on the local IPv4 network using WS-Discovery."""

import argparse
import socket
import time
import uuid
import xml.etree.ElementTree as ET


MULTICAST_ADDRESS = ("239.255.255.250", 3702)
WSA_NS = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WSD_NS = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
NETWORK_NS = "http://www.onvif.org/ver10/network/wsdl"


def make_probe():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        f'xmlns:a="{WSA_NS}" xmlns:d="{WSD_NS}" xmlns:dn="{NETWORK_NS}">'
        "<s:Header><a:MessageID>urn:uuid:"
        f"{uuid.uuid4()}</a:MessageID>"
        '<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
        '<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
        "</s:Header><s:Body><d:Probe><d:Types>"
        "dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>"
    ).encode("utf-8")


def discover(timeout=4.0):
    found = set()
    probe = make_probe()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as client:
        client.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        client.settimeout(0.25)
        client.sendto(probe, MULTICAST_ADDRESS)
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            try:
                payload, address = client.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ET.fromstring(payload)
            except ET.ParseError:
                continue
            types = [
                item.text or ""
                for item in root.iter()
                if item.tag.rsplit("}", 1)[-1] == "Types"
            ]
            xaddrs = [
                item.text or ""
                for item in root.iter()
                if item.tag.rsplit("}", 1)[-1] == "XAddrs"
            ]
            if not any("NetworkVideoTransmitter" in value or "Device" in value for value in types):
                continue
            if xaddrs:
                found.add(address[0])
    return sorted(found)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=4.0, help="discovery duration in seconds")
    args = parser.parse_args()
    if not 0.5 <= args.timeout <= 30:
        parser.error("--timeout must be between 0.5 and 30 seconds")
    for address in discover(args.timeout):
        print(address)


if __name__ == "__main__":
    main()
