"""The menu bar client: one icon in the bar, the numbers behind it, and the tray's process control."""

import logging
import os
from collections.abc import Callable

from netvitals import monitor, process
from netvitals.core.core import Core
from netvitals.core.errors import AppError
from netvitals.core.models import DnsRow, IpRow, LatencyRow, VpnRow
from netvitals.status import Health, Status, current_status
from netvitals.tray.appkit import MenuItem, MenuSeparator, TrayApp

log = logging.getLogger(__name__)

_POLL_SEC = 2.0
"""Seconds between refreshes: the warm cadence, which is the fastest series there is.

Polling faster would only re-read the same row.
"""

_LOCK_FILENAME = "tray.lock"
"""Lock file name under the data dir: one icon per data dir, and the SIGTERM target for `tray stop`."""

_STOP_TIMEOUT = 5.0
"""Seconds to wait for the icon to go away — it has nothing in flight, so this is already generous."""

_SYMBOLS = {
    Health.OK: "circle.fill",  # ● filled
    Health.SLOW: "circle.lefthalf.filled",  # ◐ half
    Health.BAD: "circle",  # ○ hollow
    Health.DOWN: "xmark",  # ✕
    Health.STALE: "circle.dotted",  # ◌ dotted outline
    Health.MONITOR_OFF: "circle.slash",  # ⊘ slashed
    Health.NO_DATA: "smallcircle.filled.circle",  # a dot waiting inside a ring
}
"""One SF Symbol per health state — this mapping is the whole icon.

Shape carries everything and color is deliberately absent: the menu bar sits on whatever wallpaper
the user has, where a tint is either invisible or shouting. The healthy ladder empties out as
things get worse — filled, half, hollow — so the three order themselves at a glance without a
legend. Anything that is not a measurement leaves that ladder: a cross means the network answered
nothing, a slashed circle means nobody is measuring (the ladder switched off), and a dotted one
means the monitor is up but its samples stopped arriving. Those last two are worth separate
symbols precisely because one shared "stale" icon would hide the difference between a connection
that is down and a monitor that is.

Symbols rather than text glyphs: the neighbours in the menu bar are drawn icons, and a character
set at font size sits visibly smaller than all of them. SF Symbols are template images at exactly
the bar's icon metrics, so ours is the same size as everyone else's.
"""


class TrayController:
    """Owns the menu bar item: builds the menu once, then keeps it in sync with the stored state."""

    def __init__(self, core: Core) -> None:
        """Build the static menu; the first refresh fills in every title."""
        self._core = core  # Read side only: the tray never measures, it shows what the monitor recorded
        self._app = TrayApp("  ")  # The refresh in run() sets the real label and icon before anyone sees it
        # A warning row above the numbers, shown only when the icon is not reporting a measurement:
        # without it, last-known values look current and the icon looks broken rather than honest.
        self._warning_item = MenuItem("", hidden=True)
        self._warm_item = MenuItem("Latency warm: ...")
        self._cold_item = MenuItem("Latency cold: ...")
        self._dns_item = MenuItem("DNS: ...")
        self._vpn_item = MenuItem("VPN: ...")
        self._ip_item = MenuItem("IP: ...")
        # Monitor control is explicit, and exactly one of the two rows is visible at any time. That
        # also makes Quit unambiguous: it closes the icon, and nothing else.
        self._start_item = MenuItem("Start monitor", callback=lambda: self._invoke(monitor.start_monitor), hidden=True)
        self._stop_item = MenuItem("Stop monitor", callback=lambda: self._invoke(monitor.stop_monitor), hidden=True)
        self._app.set_menu(
            [
                self._warning_item,
                self._warm_item,
                self._cold_item,
                self._dns_item,
                self._vpn_item,
                self._ip_item,
                MenuSeparator(),
                self._start_item,
                self._stop_item,
                MenuSeparator(),
                MenuItem("Quit tray", callback=self._app.quit),
            ]
        )

    def run(self) -> None:
        """Show the current state, then keep it fresh until the icon quits."""
        self._refresh()  # The timer's first tick is a whole interval away; the icon must be right before that
        self._app.start_timer(_POLL_SEC, self._refresh)
        self._app.run()

    def _refresh(self) -> None:
        """Re-read the stored state and push it into the label, the icon, and every menu row.

        Best-effort by design: this runs as a timer callback in a process whose stderr is
        /dev/null, so an exception escaping into AppKit would freeze the icon on its last value —
        a healthy circle nobody is updating — and leave no trace anywhere. The log file is the only
        place it can land, and the next tick tries again.
        """
        try:
            status = current_status(self._core)
            self._app.set_title(self._label(status))
            self._app.set_icon(_SYMBOLS[status.health])
            if status.health is Health.MONITOR_OFF:
                self._warning_item.set_title("Monitor is not running - the values below are from its last run")
            elif status.health is Health.STALE:
                self._warning_item.set_title("No fresh samples - the monitor is running but stuck")
            self._warning_item.set_hidden(status.health not in (Health.MONITOR_OFF, Health.STALE))
            self._warm_item.set_title(f"Latency warm: {self._latency(status.warm)}")
            self._cold_item.set_title(f"Latency cold: {self._latency(status.cold)}")
            self._dns_item.set_title(self._dns(status.dns))
            self._vpn_item.set_title(self._vpn(status.vpn))
            self._ip_item.set_title(self._ip(status.ip))
            running = status.health is not Health.MONITOR_OFF
            self._start_item.set_hidden(running)
            self._stop_item.set_hidden(not running)
        except Exception:
            log.exception("tray: refresh failed")

    @staticmethod
    def _label(status: Status) -> str:
        """Build the text beside the icon: the country code, or two blanks holding its width.

        Blanks rather than an empty string, so the item keeps one width and the icons to the left of
        us never shift. The country shows only when a fresh sample backs it: an exit country nobody
        is currently confirming is a guess, and a guess sitting next to a health icon reads as a fact.
        """
        measuring = status.health in (Health.OK, Health.SLOW, Health.BAD)
        return status.ip.country if measuring and status.ip is not None and status.ip.country else "  "

    @staticmethod
    def _latency(row: LatencyRow | None) -> str:
        """Format one latency series for its menu row."""
        if row is None:
            return "no data"
        if row.latency_ms is None:
            return "down"
        return f"{row.latency_ms:.0f} ms"

    @staticmethod
    def _dns(row: DnsRow | None) -> str:
        """Format the primary resolver; the rest of them live in the dashboard."""
        if row is None:
            return "DNS: no data"
        if row.primary_address is None:
            # An empty resolver list is its own diagnostic: no DNS configured, or a tunnel tearing down.
            return "DNS: no resolvers configured"
        if row.primary_error is not None:
            return f"DNS: {row.primary_error} ({row.primary_address})"
        if row.primary_ms is None:
            return f"DNS: ? ({row.primary_address})"
        return f"DNS: {row.primary_ms:.0f} ms ({row.primary_address})"

    @staticmethod
    def _vpn(row: VpnRow | None) -> str:
        """Format the VPN state, naming the tunnel mode and the provider when they are known."""
        if row is None:
            return "VPN: no data"
        if not row.active:
            return "VPN: off"
        body = f"{row.mode} tunnel" if row.mode is not None else "active"
        return f"VPN: {body} ({row.provider})" if row.provider else f"VPN: {body}"

    @staticmethod
    def _ip(row: IpRow | None) -> str:
        """Format the public IP with its country."""
        if row is None or row.ip is None:
            return "IP: unknown"
        return f"IP: {row.ip} ({row.country})" if row.country else f"IP: {row.ip}"

    def _invoke(self, control: Callable[[Core], object]) -> None:
        """Run a monitor control from the menu, then refresh.

        Both controls wait for the other process to take or release its lock, which freezes the menu
        for that long. That is the honest behavior for a deliberate click on a rare action: the menu
        comes back showing what actually happened rather than what was asked for. An AppError only
        means the state changed under us — the refresh right after shows whichever side won.
        """
        try:
            control(self._core)
        except AppError as e:
            log.warning("tray: %s", e)
        self._refresh()


# -- Process control: what adapters call ------------------------------------------


def run_tray(core: Core) -> None:
    """Show the icon in this process until it quits or is stopped.

    Raises AppError when another icon already holds the lock. The log is mirrored to stderr for the
    same reason as the monitor's: a foreground run belongs on the terminal, and a background run has
    its stderr on /dev/null anyway.
    """
    lock_path = core.data_dir / _LOCK_FILENAME
    lock_fd = process.acquire_lock(lock_path)
    if lock_fd is None:
        raise AppError(f"tray: already running (pid {process.lock_holder(lock_path)})")
    core.log_to_stderr()
    try:
        TrayController(core).run()
    finally:
        os.close(lock_fd)  # Releases the lock; the file stays — unlinking it would race another acquirer


def start_tray(core: Core) -> int:
    """Spawn the icon in the background and return its pid.

    Raises AppError when one is already showing, or when the spawned one dies before taking the
    lock (its story is in the log file).
    """
    return process.start_detached(core, ["tray", "run"], core.data_dir / _LOCK_FILENAME, what="tray")


def stop_tray(core: Core) -> int | None:
    """Stop the background icon and return its pid; None when none was running.

    Raises AppError when it still holds the lock after the grace period.
    """
    return process.stop_detached(core.data_dir / _LOCK_FILENAME, what="tray", timeout=_STOP_TIMEOUT)
