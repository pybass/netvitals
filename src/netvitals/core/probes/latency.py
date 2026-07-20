"""HTTP latency probes: warm (pinned keep-alive endpoint) and cold (fresh connection setup)."""

import logging
import time

import aiohttp

from netvitals.core.models import LatencySample
from netvitals.core.utils import race

log = logging.getLogger(__name__)

LATENCY_ENDPOINTS: tuple[str, ...] = (
    "https://connectivitycheck.gstatic.com/generate_204",
    "https://www.apple.com/library/test/success.html",
    "http://detectportal.firefox.com/success.txt",
    "http://www.msftconnecttest.com/connecttest.txt",
)
"""Captive-portal detection endpoints: purpose-built for connectivity checks, tiny payloads, global CDNs."""

TIMEOUT = 5.0
"""Total HTTP timeout per request in seconds."""


async def _timed_get(session: aiohttp.ClientSession, url: str) -> tuple[float, str] | None:
    """GET *url* and return (latency_ms, url), or None on any failure."""
    start = time.monotonic()
    try:
        async with session.get(url) as resp:
            await resp.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.debug("latency: %s failed: %s", url, exc)
        return None
    latency_ms = round((time.monotonic() - start) * 1000, 3)
    log.debug("latency: %s responded in %.0f ms", url, latency_ms)
    return latency_ms, url


class WarmLatencyProbe:
    """Steady-state HTTP latency over one pinned endpoint and a live keep-alive connection.

    The first cycle races every endpoint; the winner is pinned and its connection stays
    in the session pool, so steady state sends exactly one request per cycle. When the
    pinned endpoint fails, the full race runs again in the same cycle and re-pins the
    winner. When every endpoint fails, the sample is down and the session is dropped so
    the next cycle starts from a fresh election.
    """

    def __init__(self) -> None:
        """Initialize; the HTTP session is created on first measure."""
        self._session: aiohttp.ClientSession | None = None  # Keep-alive session; None until first measure / after down
        self._pinned: str | None = None  # Pinned endpoint; None before election and after down

    async def measure(self) -> LatencySample:
        """Take one warm-latency sample, electing or re-electing the pinned endpoint as needed."""
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        if self._pinned is None:
            # Election. The winning request itself includes connection setup, so its time
            # is discarded — it only warms the pool; the request below produces the sample.
            elected = await race(_timed_get(self._session, url) for url in LATENCY_ENDPOINTS)
            if elected is None:
                return await self._down()
            self._pinned = elected[1]
            log.debug("latency[warm]: pinned %s", self._pinned)
        result = await _timed_get(self._session, self._pinned)
        if result is None:
            # Failover: re-elect within the same cycle. The winner's time includes setup —
            # slightly inflated, but better than a gap, and the endpoint change marks the
            # incident in the series.
            log.warning("latency[warm]: pinned %s failed, racing for failover", self._pinned)
            result = await race(_timed_get(self._session, url) for url in LATENCY_ENDPOINTS)
        if result is None:
            return await self._down()
        latency_ms, endpoint = result
        self._pinned = endpoint
        return LatencySample(latency_ms=latency_ms, endpoint=endpoint)

    async def _down(self) -> LatencySample:
        """Record total failure: unpin, drop the session (stale pooled connections die with it)."""
        log.warning("latency[warm]: all %d endpoints failed", len(LATENCY_ENDPOINTS))
        self._pinned = None
        await self.aclose()
        return LatencySample(latency_ms=None, endpoint=None)

    async def aclose(self) -> None:
        """Release the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None


async def measure_cold_latency() -> LatencySample:
    """Measure connection-setup latency: a fresh session, full DNS + TCP + TLS + HTTP race.

    Complements the warm probe: catches setup-path failures (DNS, handshakes) that a
    reused keep-alive connection hides. The first endpoint to respond wins.
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        result = await race(_timed_get(session, url) for url in LATENCY_ENDPOINTS)
    if result is None:
        log.warning("latency[cold]: all %d endpoints failed", len(LATENCY_ENDPOINTS))
        return LatencySample(latency_ms=None, endpoint=None)
    return LatencySample(latency_ms=result[0], endpoint=result[1])
