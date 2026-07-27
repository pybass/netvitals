"""Composition root — the single object every client works through."""

import asyncio
import os
import sys
from pathlib import Path

from netvitals.core import logs
from netvitals.core.db import Db
from netvitals.core.models import DnsRow, DnsSample, IpRow, LatencyRow, LatencySample, MonitorState, Snapshot, VpnRow, VpnSample
from netvitals.core.probes.dns import measure_dns
from netvitals.core.probes.ip import detect_public_ip, resolve_country
from netvitals.core.probes.latency import WarmLatencyProbe, measure_cold_latency
from netvitals.core.probes.vpn import detect_vpn


class Core:
    """The application API: owns the probes and the database, and knows nothing about its consumers.

    Every client (the monitor, CLI, TUI, tray) receives a Core and goes through it for
    everything: they never import the database or the probes directly. What a client needs
    appears here as a method; what only Core needs stays private.
    """

    DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "netvitals"
    """Where the data lives without `--data-dir`.

    A class attribute because every entry point needs it before a Core exists, and a spawned client
    resolves it again for itself. A fixed path, deliberately not `$XDG_DATA_HOME`
    (docs/non-goals.md): a variable exported for some other tool must not move our database.
    """

    def __init__(self, data_dir: Path, *, debug: bool = False) -> None:
        """Create the data directory, set up logging, and open the database."""
        # Core owns the directory, so Core creates it: otherwise every writer below has to, and
        # which of them happens to run first becomes load-bearing.
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir  # Where the database, config, and log files live
        self.debug = debug  # DEBUG-level logging; public so a client spawning another process can forward it
        # Where a detached client's stdout/stderr go. It holds only what escaped the logger — a
        # traceback from before logging was wired, or from outside it — so it stays empty in normal
        # operation and needs no rotation. Every writer opens it "ab": O_APPEND keeps them safe.
        self.crash_log = data_dir / "crash.log"
        # The single-instance locks, one per background client: each is flock'ed by its holder for
        # that process's whole life, and the pid inside is the SIGTERM target for a stop. Core names
        # them because it owns what lives in the data directory; what the holders do is not its business.
        self.monitor_lock = data_dir / "monitor.lock"
        self.tray_lock = data_dir / "tray.lock"
        self._warm_probe = WarmLatencyProbe()  # Pinned warm session for sampling; allocates nothing until first measure
        self._ip: str | None = None  # Last known public IP, to skip a redundant country lookup
        self._country: str | None = None  # Country of `_ip`
        # Logging is wired here, not by each client: every client wants the same file log,
        # and a client that forgot to set it up would lose its records silently.
        logs.setup_logging(data_dir / "netvitals.log", debug=debug)
        # After logging is wired: opening the database logs applied migrations.
        self._db = Db(data_dir / "netvitals.db")  # Private on purpose: clients go through Core methods, never Db

    # -- One-shot measurement ---------------------------------------------------

    async def take_snapshot(self) -> Snapshot:
        """Run every probe concurrently and combine the results; records nothing.

        The warm probe elects an endpoint and warms its connection internally, so the
        returned warm sample is a true steady-state measurement even in a one-shot run.
        """
        warm_probe = WarmLatencyProbe()
        try:
            warm, cold, vpn, dns, (ip, country) = await asyncio.gather(
                warm_probe.measure(), measure_cold_latency(), detect_vpn(), measure_dns(), _ip_and_country()
            )
        finally:
            await warm_probe.aclose()
        return Snapshot(latency_warm=warm, latency_cold=cold, vpn=vpn, ip=ip, country=country, dns=dns)

    # -- Sampling: measure via probes and record to the database ----------------

    async def sample_warm(self, now: float) -> LatencySample:
        """Take one warm-latency sample over the pinned keep-alive session and record it."""
        sample = await self._warm_probe.measure()
        self._db.insert_warm_latency(now, sample)
        return sample

    async def reset_warm(self) -> None:
        """Drop the warm probe's keep-alive session; the next sample starts from a fresh connection."""
        await self._warm_probe.aclose()

    async def sample_cold(self, now: float) -> LatencySample:
        """Take one cold-latency (connection setup) sample and record it."""
        sample = await measure_cold_latency()
        self._db.insert_cold_latency(now, sample)
        return sample

    async def sample_dns(self, now: float) -> DnsSample:
        """Query every system resolver once and record the cycle."""
        sample = await measure_dns()
        self._db.insert_dns(now, sample)
        return sample

    async def sample_vpn(self, now: float) -> VpnSample:
        """Detect VPN state and record it."""
        sample = await detect_vpn()
        self._db.upsert_vpn(now, sample)
        return sample

    async def sample_ip(self, now: float) -> None:
        """Record the public IP, resolving its country only when the address is new to us."""
        ip = await detect_public_ip()
        if ip is None:
            self._country = None
        elif ip != self._ip:
            # A country already stored for this address is authoritative: an IP's country does not
            # change within our retention horizon, and the country services are quota-limited.
            self._country = self._db.fetch_country_for_ip(ip) or await resolve_country(ip)
        self._ip = ip
        self._db.upsert_ip(now, ip, self._country)

    # -- History: recorded samples served to readers, oldest first ---------------
    #
    # The newest element doubles as the current state, so readers need no separate
    # "latest" calls.

    def warm_history(self, limit: int) -> list[LatencyRow]:
        """Return the newest *limit* warm-latency samples, oldest first."""
        return self._db.fetch_recent_warm(limit)

    def cold_history(self, limit: int) -> list[LatencyRow]:
        """Return the newest *limit* cold-latency samples, oldest first."""
        return self._db.fetch_recent_cold(limit)

    def dns_history(self, limit: int) -> list[DnsRow]:
        """Return the newest *limit* DNS cycles, oldest first."""
        return self._db.fetch_recent_dns(limit)

    def vpn_history(self, limit: int) -> list[VpnRow]:
        """Return the newest *limit* VPN state changes, oldest first."""
        return self._db.fetch_recent_vpn(limit)

    def ip_history(self, limit: int) -> list[IpRow]:
        """Return the newest *limit* public-IP state changes, oldest first."""
        return self._db.fetch_recent_ip(limit)

    # -- Monitor data: stored and served without knowing what process writes it -

    def record_heartbeat(self, started_at: float, now: float) -> None:
        """Record that this process is alive, so readers can tell "monitor down" from "network down"."""
        self._db.update_heartbeat(os.getpid(), started_at, now)

    def monitor_state(self) -> MonitorState | None:
        """Return the monitor's last heartbeat row; None when no monitor has ever run here."""
        return self._db.fetch_monitor_state()

    # -- Maintenance ------------------------------------------------------------

    def purge(self, now: float) -> int:
        """Delete measurements past retention. Return rows deleted."""
        # 30 days: the documented retention horizon (docs/storage.md)
        return self._db.purge(now - 30 * 86400)

    # -- Infra ------------------------------------------------------------------

    def log_to_terminal(self) -> None:
        """Mirror the log to stderr as well; a no-op when stderr is not a terminal.

        A detached client's stderr is the crash log: mirroring every INFO line into it would bury
        the tracebacks it exists to catch, in a file that is deliberately never rotated.
        """
        if sys.stderr.isatty():
            logs.log_to_stderr()

    def close(self) -> None:
        """Close the database."""
        self._db.close()


async def _ip_and_country() -> tuple[str | None, str | None]:
    """Detect the public IP, then resolve its country (skipped when the IP is unknown)."""
    ip = await detect_public_ip()
    if ip is None:
        return None, None
    return ip, await resolve_country(ip)
