"""Public IP address and country detection via public lookup services."""

import ipaddress
import json
import logging
import random
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp

from netvitals.core.models import LookupSample
from netvitals.core.utils import race

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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
    "https://api.db-ip.com/v2/free/{ip}/countryCode",
    "https://api.ip2location.io/?ip={ip}",
)
"""Country resolution URL templates; {ip} is substituted at call time.

Each operator maintains its own geolocation database. Without a key db-ip allows 500 requests a
day and ip2location 1000, counted per client address. ip2location answers JSON, the others the bare code.
"""

TIMEOUT = 5.0
"""Total HTTP timeout per request in seconds."""

_rng = random.SystemRandom()  # OS randomness: the choice is load spreading, but this also satisfies security lint (S311)


def _parse_ipv4(text: str) -> str | None:
    """Return *text* when it is an IPv4 address; None otherwise."""
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return None
    return text


def _parse_country(text: str) -> str | None:
    """Return the 2-letter ISO country code in *text*: the bare code, or `country_code` of a JSON object."""
    if text.startswith("{"):
        try:
            code = json.loads(text).get("country_code")
        except ValueError:
            return None
        if not isinstance(code, str):
            return None
        text = code
    # "ZZ" is db-ip's answer for an address it cannot place; cached, it would pass for a country.
    return text if len(text) == 2 and text.isascii() and text.isalpha() and text.isupper() and text != "ZZ" else None


async def _ask(
    session: aiohttp.ClientSession, url: str, parse: Callable[[str], str | None], failures: dict[str, str]
) -> tuple[str, str] | None:
    """GET *url* and return (value, host) when *parse* accepts the body; otherwise note the reason in *failures*."""
    host = urlsplit(url).netloc
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            # errors="replace": a body that is not text must fail validation below, not raise from here.
            text = (await resp.text(errors="replace")).strip()
    except aiohttp.ClientResponseError as exc:
        reason = f"HTTP {exc.status}"  # The exception's own text repeats the URL, and its repr every header
    except (aiohttp.ClientError, TimeoutError) as exc:
        # The class name always: a timeout has no message of its own.
        reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    else:
        value = parse(text)
        if value is not None:
            log.debug("ip: %s returned %r", url, value)
            return value, host
        reason = f"invalid response {text[:40]!r}"
    log.debug("ip: %s failed: %s", url, reason)
    failures[host] = reason
    return None


async def _lookup(urls: Iterable[str], parse: Callable[[str], str | None]) -> LookupSample:
    """Race *urls*; the first body *parse* accepts wins, so a fast invalid answer cannot beat a slower valid one.

    A throwaway session per call: at minute-scale intervals connection reuse is pointless.
    """
    failures: dict[str, str] = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        won = await race(_ask(session, url, parse, failures) for url in urls)
    if won is None:
        return LookupSample(value=None, source=None, error="; ".join(f"{host}: {reason}" for host, reason in failures.items()))
    return LookupSample(value=won[0], source=won[1], error=None)


async def detect_public_ip(*, every_service: bool = False) -> LookupSample:
    """Detect the public IPv4 address by racing two randomly chosen services.

    Two per detection is enough redundancy, and the load spreads across providers. *every_service*
    races all of them instead: for a retry after a failed detection, where a quick answer matters
    more than the load.
    """
    return await _lookup(IP_SERVICES if every_service else _rng.sample(IP_SERVICES, 2), _parse_ipv4)


async def resolve_country(ip: str) -> LookupSample:
    """Resolve the 2-letter ISO country code for *ip* by racing every country service.

    Callers are expected to cache per-IP results — country services are quota-limited,
    and an IP's country never changes within our retention horizon.
    """
    return await _lookup((template.format(ip=ip) for template in COUNTRY_SERVICES), _parse_country)
