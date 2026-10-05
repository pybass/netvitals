"""The Snapshot window: every probe run once on demand, each row filled in as its probe finishes."""

import asyncio
import logging
import threading
import time
from urllib.parse import urlsplit

from netvitals.core.core import Core
from netvitals.core.models import LatencySample, SnapshotProgress
from netvitals.tray.appkit import Row, Window, call_on_main

log = logging.getLogger(__name__)


class SnapshotWindow:
    """Owns the Snapshot window and the one check that may be running in it.

    A check runs on a background thread: a probe can take seconds, and the main thread is the menu
    bar icon. Every result is handed back to the main thread before it touches the window.
    """

    def __init__(self, core: Core) -> None:
        """Build the window; nothing is measured until it is opened."""
        self._core = core
        self._window = Window("netvitals snapshot", button="Check again", on_button=self._check)
        self._running = False  # One check at a time: the rows on screen belong to exactly one run

    def open(self) -> None:
        """Bring the window to the front, and start a check unless one is already running."""
        if not self._running:
            self._check()
        self._window.show()

    def _check(self) -> None:
        """Reset every row to a spinner, then measure everything again in the background."""
        # The monitor's current address saves a country lookup. It is read here because the database
        # belongs to the main thread, and first because a failed read must leave the window as it was.
        countries = {row.ip: row.country for row in self._core.ip_history(1) if row.ip is not None and row.country is not None}
        self._running = True
        self._window.set_button_enabled(False)
        self._window.set_status("Checking...")
        self._window.set_rows(_rows(SnapshotProgress()))
        threading.Thread(target=self._measure, args=(countries,), name="snapshot", daemon=True).start()

    def _measure(self, countries: dict[str, str]) -> None:
        """Run the check. This is the background thread: it may reach the window only through call_on_main."""
        try:
            asyncio.run(
                self._core.take_snapshot(
                    known_countries=countries,
                    on_progress=lambda progress: call_on_main(self._window.set_rows, _rows(progress)),
                )
            )
        except Exception:
            # Probes report a failed measurement as a sample, so this is a bug, not the network. It is
            # caught because the window would otherwise show "Checking..." forever with its button off.
            log.exception("tray: snapshot failed")
            call_on_main(self._finish, failed=True)
        else:
            call_on_main(self._finish, failed=False)

    def _finish(self, *, failed: bool) -> None:
        """Close a check on the main thread."""
        self._running = False
        self._window.set_button_enabled(True)
        if failed:
            self._window.set_rows(())  # Half-filled rows with spinners that will never stop would be a lie
            self._window.set_status("Check failed - see netvitals.log")
        else:
            self._window.set_status(f"Checked {time.strftime('%H:%M:%S')}")


def _rows(progress: SnapshotProgress) -> list[Row]:
    """Build the rows for *progress*: a probe that has not finished is a spinner in its place.

    The order is fixed, so nothing jumps as results arrive. DNS is last because it is the one row
    that grows — into a row per resolver.
    """
    rows = [_latency_row("Latency warm", progress.latency_warm), _latency_row("Latency cold", progress.latency_cold)]

    vpn = progress.vpn
    if vpn is None:
        rows.append(Row("VPN", busy=True))
    elif not vpn.active:
        rows.append(Row("VPN", "inactive"))
    else:
        details = [detail for detail in (vpn.mode and f"{vpn.mode} tunnel", vpn.interface, vpn.provider) if detail]
        rows.append(Row("VPN", f"active ({', '.join(details)})" if details else "active"))

    ip = progress.ip
    if ip is None:
        rows.append(Row("IP", busy=True))
    elif ip.ip is None:
        rows.append(Row("IP", "unknown"))
    else:
        rows.append(Row("IP", f"{ip.ip} ({ip.country})" if ip.country else ip.ip, busy=ip.country_pending))

    dns = progress.dns
    if dns is None:
        rows.append(Row("DNS", busy=True))
    elif not dns.resolvers:
        rows.append(Row("DNS", "no resolvers"))
    else:
        for resolver in dns.resolvers:
            if resolver.error is None:
                result = f"{resolver.latency_ms:.0f} ms"
            elif resolver.latency_ms is not None:
                result = f"{resolver.error} in {resolver.latency_ms:.0f} ms"
            else:
                result = str(resolver.error)
            rows.append(Row("DNS", f"{result} ({resolver.address})"))
    return rows


def _latency_row(label: str, sample: LatencySample | None) -> Row:
    """Format one latency row: milliseconds and the answering host, or down."""
    if sample is None:
        return Row(label, busy=True)
    if sample.latency_ms is None:
        return Row(label, "down")
    host = (urlsplit(sample.endpoint).hostname or "?") if sample.endpoint else "?"
    return Row(label, f"{sample.latency_ms:.0f} ms ({host})")
