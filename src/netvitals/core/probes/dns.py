"""DNS probe — latency and reachability of the system's own resolvers."""

import asyncio
import logging
import time

import dns.asyncquery
import dns.exception
import dns.message
import dns.rcode
import dns.rdatatype

from netvitals.core.models import DnsError, DnsSample, ResolverSample
from netvitals.core.utils import SCUTIL, run_command

log = logging.getLogger(__name__)

_RCODE_ERRORS = {
    dns.rcode.SERVFAIL: DnsError.SERVFAIL,
    dns.rcode.NXDOMAIN: DnsError.NXDOMAIN,
    dns.rcode.REFUSED: DnsError.REFUSED,
}
# Common non-success rcodes mapped to typed categories; anything else becomes OTHER.


def _parse_scutil_dns(text: str) -> list[str]:
    """Extract the system's effective default nameservers from `scutil --dns` output.

    Preference order:
    1. `resolver #1` of the main `DNS configuration` section — the normal case.
    2. Fallback: interface-scoped resolvers (blocks without a `domain :` line) from the
       `(for scoped queries)` section. Some VPN clients publish their DNS only via
       per-interface scoping, leaving the main default resolver empty. Domain-scoped
       blocks serve only lookups matching their domain and are always ignored.
    """
    lines = [line.strip() for line in text.splitlines()]
    primary = _main_default_nameservers(lines)
    if primary:
        return primary
    fallback = _scoped_interface_nameservers(lines)
    if fallback:
        log.debug("dns: main default resolver empty, using %d scoped nameserver(s)", len(fallback))
    return fallback


def _main_default_nameservers(lines: list[str]) -> list[str]:
    """Nameservers of `resolver #1` in the main `DNS configuration` section."""
    nameservers: list[str] = []
    in_main = False
    in_default = False
    for line in lines:
        if line.startswith("DNS configuration (for scoped"):
            break  # The scoped-queries section follows the main one.
        if line == "DNS configuration":
            in_main = True
        elif in_main and line.startswith("resolver #"):
            # Only resolver #1 is the default; #2+ in the main section are per-domain scopes.
            in_default = line == "resolver #1"
        elif in_main and in_default and line.startswith("nameserver["):
            addr = line.partition(":")[2].strip()  # Split on the first ':' only — safe for IPv6 addresses
            if addr:
                nameservers.append(addr)
    return nameservers


def _scoped_interface_nameservers(lines: list[str]) -> list[str]:
    """Nameservers of interface-scoped resolver blocks in the `(for scoped queries)` section."""
    blocks: list[tuple[list[str], bool]] = []  # Per resolver block: (nameservers, is domain-scoped)
    in_section = False
    for line in lines:
        if line.startswith("DNS configuration (for scoped"):
            in_section = True
        elif in_section and line.startswith("resolver #"):
            blocks.append(([], False))
        elif in_section and blocks and line.startswith("nameserver["):
            addr = line.partition(":")[2].strip()
            if addr:
                blocks[-1][0].append(addr)
        elif in_section and blocks and line.startswith("domain "):
            # `domain : example.com` marks a per-domain scope (`search domain[N]` starts with "search").
            blocks[-1] = (blocks[-1][0], True)
    return [addr for nameservers, domain_scoped in blocks if not domain_scoped for addr in nameservers]


async def system_resolvers() -> list[str]:
    """Addresses of the system's effective default DNS resolvers; [] when none can be discovered.

    `scutil --dns` is the authoritative source of macOS DNS configuration —
    `/etc/resolv.conf` is a legacy shim that does not reflect VPN resolver swaps.
    """
    output = await run_command(SCUTIL, "--dns", timeout=3.0)
    if output is None:
        return []
    return _parse_scutil_dns(output)


async def _query_resolver(nameserver: str) -> ResolverSample:
    """Send one A-record UDP query for the canary to *nameserver* and time the round-trip."""
    # cloudflare.com as the canary: short name, DNS-native operator, 300 s TTL, no dual-stack complications.
    query = dns.message.make_query("cloudflare.com", dns.rdatatype.A)
    start = time.monotonic()
    try:
        response = await dns.asyncquery.udp(query, nameserver, timeout=2.0)  # Seconds; no retries — a miss is the signal
    except dns.exception.Timeout:
        log.debug("dns: %s timed out", nameserver)
        return ResolverSample(address=nameserver, latency_ms=None, error=DnsError.TIMEOUT)
    except OSError as exc:
        log.debug("dns: %s network error: %s", nameserver, exc)
        return ResolverSample(address=nameserver, latency_ms=None, error=DnsError.NETWORK)
    except dns.exception.DNSException as exc:
        log.debug("dns: %s malformed response: %s", nameserver, exc)
        return ResolverSample(address=nameserver, latency_ms=None, error=DnsError.MALFORMED)
    latency_ms = round((time.monotonic() - start) * 1000, 3)
    rcode = response.rcode()
    if rcode != dns.rcode.NOERROR:
        # The exchange completed, so the latency is real and recorded alongside the error.
        log.debug("dns: %s rcode=%s in %.0f ms", nameserver, dns.rcode.to_text(rcode), latency_ms)
        return ResolverSample(address=nameserver, latency_ms=latency_ms, error=_RCODE_ERRORS.get(rcode, DnsError.OTHER))
    log.debug("dns: %s responded in %.0f ms", nameserver, latency_ms)
    return ResolverSample(address=nameserver, latency_ms=latency_ms, error=None)


async def measure_dns() -> DnsSample:
    """Query every system resolver in parallel: one UDP round-trip each, no retries."""
    resolvers = await system_resolvers()
    if not resolvers:
        log.warning("dns: no system resolvers found")
        return DnsSample(resolvers=[])
    samples = await asyncio.gather(*(_query_resolver(addr) for addr in resolvers))
    return DnsSample(resolvers=list(samples))
