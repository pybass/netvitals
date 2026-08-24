"""The background monitor: the single process that measures continuously and records.

One process owns all measurement. Every other client (dashboard, tray, CLI) only reads what the
monitor wrote, so they all show the same numbers, taken once. The monitor decides *when* — grids,
gaps, failure policy, signals live here; Core does *what* — measuring and recording. Also here:
the process controls adapters call (`run_monitor`, `start_monitor`, `stop_monitor`,
`monitor_status`), built on the generic lock-and-spawn machinery in `process.py`.
"""

# create_task() hands the Task to the TaskGroup; `async with` awaits them all on exit.
# mypy: disable-error-code="unused-awaitable"

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from netvitals import process
from netvitals.core.core import Core
from netvitals.core.errors import AppError

log = logging.getLogger(__name__)

WARM_INTERVAL = 2.0
"""Seconds between warm-latency samples — the fastest signal, and what "is it up right now" means."""

COLD_INTERVAL = 10.0
"""Seconds between cold-latency samples: a full connection setup is too expensive to pay every cycle."""

DNS_INTERVAL = 10.0
"""Seconds between DNS cycles (one cycle queries every system resolver in parallel)."""

VPN_INTERVAL = 10.0
"""Seconds between VPN checks — a local routing lookup, so the cost is negligible."""

IP_INTERVAL = 60.0
"""Seconds between public-IP lookups: the services are quota-limited and the address rarely changes."""

PURGE_INTERVAL = 3600.0
"""Seconds between retention purges."""

_MAX_CONSECUTIVE_FAILURES = 3
"""Failures in a row before a loop gives up and takes the whole monitor down with it.

Probes report a failed measurement as a sample, never as an exception, so an exception here means
storage broke or we have a bug. Catching it forever would keep the process alive while it records
nothing; a loud death that a supervisor can restart is the honest outcome.
"""

_STOP_TIMEOUT = 10.0
"""Seconds to wait for a graceful exit — a probe can be mid-request for its full timeout first."""


@dataclass(frozen=True, slots=True)
class MonitorStatus:
    """Derived monitor liveness: the lock says what runs now, the heartbeat when one last ran."""

    pid: int | None  # Pid holding the monitor lock; None when no monitor is running
    started_at: float | None  # UTC Unix seconds the running (or last) monitor started; None when none ever ran
    heartbeat_at: float | None  # UTC Unix seconds of the last heartbeat; None when none ever ran


class Monitor:
    """Schedules the probes: every sampling method of Core runs on its own fixed grid."""

    def __init__(self, core: Core) -> None:
        """Initialize with the Core that measures and records."""
        self._core = core  # Does the actual work: probes and storage
        self._vpn_active: bool | None = None  # Previous VPN state, to notice a change; None before the first check
        self._started_at = 0.0  # UTC Unix seconds when run() began; reported in the heartbeat
        self._shutdown = asyncio.Event()  # Set by SIGTERM/SIGINT; every loop exits at its next wake

    async def run(self) -> None:
        """Run every loop until a signal arrives, then shut down and release the probes' resources."""
        self._started_at = time.time()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown, sig)
        log.info("monitor: started (pid %d)", os.getpid())
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._loop("warm", WARM_INTERVAL, self._measure_warm, on_gap=self._core.reset_warm))
                tasks.create_task(self._loop("cold", COLD_INTERVAL, self._core.sample_cold))
                tasks.create_task(self._loop("dns", DNS_INTERVAL, self._core.sample_dns))
                tasks.create_task(self._loop("vpn", VPN_INTERVAL, self._detect_vpn))
                tasks.create_task(self._loop("ip", IP_INTERVAL, self._core.sample_ip))
                tasks.create_task(self._loop("purge", PURGE_INTERVAL, self._purge))
        except* Exception as failures:
            # Nothing above us will report this: when spawned in the background there is no
            # terminal for a traceback, so the log file is the only place it can land.
            for failure in failures.exceptions:
                log.critical("monitor: fatal error", exc_info=failure)
            raise
        finally:
            await self._core.reset_warm()
            log.info("monitor: stopped")

    def _request_shutdown(self, sig: signal.Signals) -> None:
        """Ask the loops to finish on the first signal; die immediately on a repeat.

        A probe can be mid-request for its whole timeout, so a graceful stop is not instant. The
        second signal is the user saying they know and want out now — restoring the default
        handler and re-raising kills us exactly as an unhandled signal would.
        """
        if self._shutdown.is_set():
            log.warning("monitor: second %s, exiting now", sig.name)
            signal.signal(sig, signal.SIG_DFL)
            signal.raise_signal(sig)
        log.info("monitor: %s received, shutting down", sig.name)
        self._shutdown.set()

    async def _loop(
        self,
        name: str,
        interval: float,
        work: Callable[[float], Awaitable[object]],
        *,
        on_gap: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Run *work* every *interval* seconds until shutdown, keeping to a fixed grid.

        Wake times come from a grid (`next_at += interval`), not from sleeping *after* the work:
        the latter stretches the real period by however long the measurement took, which makes an
        evenly spaced series impossible. Missed cycles are dropped, never fired back to back — a
        burst of catch-up samples all timestamped now would misreport when they were taken.

        *on_gap* is invoked after a gap, for state that cannot have survived it.
        """
        clock = asyncio.get_running_loop().time  # The clock asyncio's own timers run on
        next_at = clock()
        failures = 0
        while not self._shutdown.is_set():
            delay = next_at - clock()
            if delay > 0:
                before = time.time()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
                if self._shutdown.is_set():
                    return
                # Wall-clock time the monotonic clock never saw: the machine was asleep.
                gap = time.time() - before - delay
            else:
                gap = -delay  # Behind schedule: the work outlasts its cadence, or the loop stalled
            # 60+ s of missing schedule is a real gap — a machine sleep or a stalled loop. Anything
            # smaller passes silently: work a little slower than its cadence already shows up as
            # wider row spacing, and the monitor could not do better anyway.
            if gap >= 60.0:
                log.warning("monitor[%s]: %.0f s gap in the series", name, gap)
                next_at = clock()
                if on_gap is not None:
                    await on_gap()
            next_at += interval
            try:
                await work(time.time())
            except Exception:
                failures += 1
                log.exception("monitor[%s]: failure %d of %d", name, failures, _MAX_CONSECUTIVE_FAILURES)
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise
            else:
                failures = 0

    # -- The measurements ------------------------------------------------------

    async def _measure_warm(self, now: float) -> None:
        """Take one warm-latency sample, and refresh the heartbeat on the same tick.

        The heartbeat rides the fastest loop so readers can trust it as "the monitor was alive a
        moment ago" without a loop of its own.
        """
        await self._core.sample_warm(now)
        self._core.record_heartbeat(self._started_at, now)

    async def _detect_vpn(self, now: float) -> None:
        """Record VPN state; on a change, check the public IP right away instead of within a minute."""
        sample = await self._core.sample_vpn(now)
        if self._vpn_active is not None and sample.active != self._vpn_active:
            log.info("monitor[vpn]: active %s -> %s, checking the public IP now", self._vpn_active, sample.active)
            # The exit point almost certainly moved. The IP loop's own grid may repeat this check
            # seconds later — one redundant lookup per VPN change, deduplicated in storage.
            await self._core.sample_ip(now)
        self._vpn_active = sample.active

    async def _purge(self, now: float) -> None:
        """Delete measurements older than the retention horizon."""
        deleted = self._core.purge(now)
        if deleted:
            log.info("monitor[purge]: deleted %d old row(s)", deleted)


# -- Process control: what adapters call ------------------------------------------


async def run_monitor(core: Core) -> None:
    """Run the monitor in this process until a signal stops it.

    Raises AppError when another monitor already holds the lock. The log is mirrored to the
    terminal when there is one: a foreground run belongs there too.
    """
    lock_fd = process.acquire_lock(core.monitor_lock)
    if lock_fd is None:
        raise AppError(f"monitor: already running (pid {process.lock_holder(core.monitor_lock)})")
    core.log_to_terminal()
    try:
        await Monitor(core).run()
    finally:
        os.close(lock_fd)  # Releases the lock; the file stays — unlinking it would race another acquirer


def start_monitor(core: Core) -> int:
    """Spawn a background monitor, wait until it holds the lock, and return its pid.

    Raises AppError when a monitor is already running, or when the spawned one dies before taking
    the lock (its story is in the log file).
    """
    return process.start_detached(core, ["monitor", "run"], core.monitor_lock, what="monitor")


def stop_monitor(core: Core) -> int | None:
    """Stop the background monitor and return its pid; None when none was running.

    Raises AppError when it still holds the lock after the grace period — deliberately without
    SIGKILL: a monitor that ignores SIGTERM for that long is a bug worth seeing.
    """
    return process.stop_detached(core.monitor_lock, what="monitor", timeout=_STOP_TIMEOUT)


def monitor_status(core: Core) -> MonitorStatus:
    """Return the combined liveness view: the lock answers "running now", the heartbeat "last alive"."""
    state = core.monitor_state()
    return MonitorStatus(
        pid=process.lock_holder(core.monitor_lock),
        started_at=state.started_at if state is not None else None,
        heartbeat_at=state.updated_at if state is not None else None,
    )
