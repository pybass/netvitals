"""Public IP address and country detection via plain-text services."""

import ipaddress
import logging
import random

import aiohttp

from netvitals.core.utils import race

log = logging.getLogger(__name__)

IP_SERVICES: tuple[str, ...] = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ipinfo.io/ip",
    "https://v4.ident.me",
)
"""Plain-text IPv4 detection services."""

COUNTRY_SERVICES: tuple[str, ...] = (
    "https://ipinfo.io/{ip}/country",
    "https://ipapi.co/{ip}/country/",
)
"""Country resolution URL templates; {ip} is substituted at call time."""

TIMEOUT = 5.0
"""Total HTTP timeout per request in seconds."""

_rng = random.SystemRandom()  # OS randomness: the choice is load spreading, but this also satisfies security lint (S311)


async def _get_text(session: aiohttp.ClientSession, url: str) -> str | None:
    """GET *url* and return the stripped non-empty body text; None on any failure."""
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            text = (await resp.text()).strip()
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.debug("ip: %s failed: %s", url, exc)
        return None
    log.debug("ip: %s returned %r", url, text)
    return text or None


async def detect_public_ip() -> str | None:
    """Detect the public IPv4 address by racing randomly chosen services; None when undetectable.

    A throwaway session per call: at minute-scale intervals connection reuse is pointless.
    """
    urls = _rng.sample(IP_SERVICES, 2)  # Two per detection — enough redundancy, and the load spreads across providers
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        text = await race(_get_text(session, url) for url in urls)
    if text is None:
        log.warning("ip: all %d services failed", len(urls))
        return None
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        log.warning("ip: invalid IPv4 response: %r", text)
        return None
    return text


async def resolve_country(ip: str) -> str | None:
    """Resolve the 2-letter ISO country code for *ip*; None when unresolvable.

    Callers are expected to cache per-IP results — country services are quota-limited,
    and an IP's country never changes within our retention horizon.
    """
    urls = [template.format(ip=ip) for template in COUNTRY_SERVICES]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        text = await race(_get_text(session, url) for url in urls)
    if text is None:
        log.warning("country: all %d services failed", len(COUNTRY_SERVICES))
        return None
    if len(text) == 2 and text.isascii() and text.isalpha() and text.isupper():
        return text
    log.warning("country: invalid response: %r", text)
    return None
