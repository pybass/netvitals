"""VPN detection: tunnel interfaces, kernel routing lookup, provider name."""

import asyncio
import logging
import re
import socket

import psutil

from netvitals.core.models import TunnelMode, VpnSample
from netvitals.core.utils import SCUTIL, run_command

log = logging.getLogger(__name__)

_CMD_TIMEOUT = 5.0
"""Timeout for route/scutil subprocesses in seconds."""


def tunnel_interfaces() -> list[str]:
    """Names of tun/utun interfaces holding an IPv4 address (the address is the liveness filter)."""
    return [
        name
        for name, addrs in psutil.net_if_addrs().items()
        if name.startswith(("tun", "utun")) and any(a.family == socket.AF_INET and a.address for a in addrs)
    ]


async def _egress_interface() -> str | None:
    """Interface the kernel would route a public address through — a pure routing-table lookup.

    Asking the kernel via `route -n get` follows the actual longest-prefix match, so it is
    immune to VPN routing conventions (plain default route, 0/1 + 128.0/1 halves, etc.).
    """
    # 1.1.1.1: any well-known public anycast address works — the lookup sends no packets to it.
    output = await run_command("/sbin/route", "-n", "get", "1.1.1.1", timeout=_CMD_TIMEOUT)
    if output is None:
        return None
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "interface":
            return value.strip() or None
    return None


async def _connected_provider() -> str | None:
    """Name of the connected VPN service from `scutil --nc list`, best effort.

    Only NetworkExtension-based clients appear here; for others the provider stays None.
    """
    output = await run_command(SCUTIL, "--nc", "list", timeout=_CMD_TIMEOUT)
    if output is None:
        return None
    for line in output.splitlines():
        if "(Connected)" in line:
            match = re.search(r'"([^"]+)"', line)  # The service name is the quoted string on the line
            if match:
                return match.group(1)
    return None


async def detect_vpn() -> VpnSample:
    """Detect VPN state: tunnel presence, full/split mode, carrying interface, provider."""
    tunnels = tunnel_interfaces()
    if not tunnels:
        return VpnSample(active=False, mode=None, interface=None, provider=None)
    egress, provider = await asyncio.gather(_egress_interface(), _connected_provider())
    mode: TunnelMode | None
    if egress is None:
        mode, interface = None, tunnels[0]
    elif egress in tunnels:
        mode, interface = TunnelMode.FULL, egress
    else:
        mode, interface = TunnelMode.SPLIT, tunnels[0]
    return VpnSample(active=True, mode=mode, interface=interface, provider=provider)
