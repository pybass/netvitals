"""The snapshot command: one-shot run of every probe."""

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from netvitals.cli import utils

if TYPE_CHECKING:
    from netvitals.core.models import LatencySample, ResolverSample, VpnSample


def run(*, core: utils.InjectedCore) -> None:
    """Run every probe once and print the combined result."""
    snap = asyncio.run(core.take_snapshot())
    utils.console.print(_latency_line("Latency warm", snap.latency_warm))
    utils.console.print(_latency_line("Latency cold", snap.latency_cold))
    utils.console.print(_vpn_line(snap.vpn))
    if snap.ip is None:
        utils.console.print("IP: [red]unknown[/]")
    else:
        utils.console.print(f"IP: {snap.ip} ({snap.country})" if snap.country else f"IP: {snap.ip}")
    if not snap.dns.resolvers:
        utils.console.print("DNS: [red]no resolvers[/]")
    for resolver in snap.dns.resolvers:
        utils.console.print(_resolver_line(resolver))


def _latency_line(label: str, sample: LatencySample) -> str:
    """Format one latency row: milliseconds and the answering host, or down."""
    if sample.latency_ms is None:
        return f"{label}: [red]down[/]"
    host = (urlsplit(sample.endpoint).hostname or "?") if sample.endpoint else "?"
    return f"{label}: {sample.latency_ms:.0f} ms ({host})"


def _vpn_line(vpn: VpnSample) -> str:
    """Format the VPN row: active with known details in parentheses, or inactive."""
    if not vpn.active:
        return "VPN: inactive"
    details = [detail for detail in (vpn.mode and f"{vpn.mode} tunnel", vpn.interface, vpn.provider) if detail]
    return f"VPN: active ({', '.join(details)})" if details else "VPN: active"


def _resolver_line(resolver: ResolverSample) -> str:
    """Format one DNS resolver row: latency, an error with latency, or a bare error."""
    if resolver.error is None:
        return f"DNS: {resolver.address}: {resolver.latency_ms:.0f} ms"
    if resolver.latency_ms is not None:
        return f"DNS: {resolver.address}: [red]{resolver.error}[/] in {resolver.latency_ms:.0f} ms"
    return f"DNS: {resolver.address}: [red]{resolver.error}[/]"
