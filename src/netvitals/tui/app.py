"""The live dashboard: a read-only Textual app over what the monitor records.

Layout: a one-line status banner, sparklines for warm/cold latency and DNS, a merged
VPN/IP events list, and a footer with monitor liveness. On wide terminals the events
list moves beside the sparklines. Hotkeys open a history screen per probe series.
"""

import time
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.widgets import Static

from netvitals import monitor
from netvitals.core.core import Core
from netvitals.status import COLD_OK_MS, COLD_SLOW_MS, WARM_OK_MS, WARM_SLOW_MS
from netvitals.tui.screens import DnsHistoryScreen, IpHistoryScreen, LatencyHistoryScreen, VpnHistoryScreen
from netvitals.tui.widgets import BannerWidget, DnsWidget, EventsWidget, LatencyWidget


class TuiApp(App[None]):
    """The dashboard app: polls the database on a timer and pushes data into the widgets."""

    TITLE = "netvitals"
    CSS = """
    Screen {
        layout: vertical;
        overflow: hidden;
    }
    #main {
        layout: vertical;
        height: 1fr;
    }
    #sparks {
        layout: vertical;
        height: auto;
    }
    #main.wide {
        layout: horizontal;
    }
    #main.wide #sparks {
        width: 2fr;
        height: 1fr;
    }
    #main.wide EventsWidget {
        width: 1fr;
    }
    #footer-bar {
        height: 1;
        dock: bottom;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("w", "warm_history", "Warm"),
        Binding("c", "cold_history", "Cold"),
        Binding("d", "dns_history", "DNS"),
        Binding("v", "vpn_history", "VPN"),
        Binding("i", "ip_history", "IP"),
        Binding("q", "quit", "Quit"),
    ]

    _HISTORY_LIMIT = 300  # Deepest sparkline window we ever draw; widgets trim to their actual width at render time
    _EVENTS_LIMIT = 10  # VPN and IP change rows shown in the events panel (each)

    def __init__(self, core: Core) -> None:
        """Initialize with the application core."""
        super().__init__()
        self._core = core  # All reads go through here

    def compose(self) -> ComposeResult:
        """Build the widget tree."""
        yield BannerWidget()
        with Container(id="main"):
            with Vertical(id="sparks"):
                yield LatencyWidget("warm", WARM_OK_MS, WARM_SLOW_MS)
                yield LatencyWidget("cold", COLD_OK_MS, COLD_SLOW_MS)
                yield DnsWidget()
            yield EventsWidget()
        yield Static(id="footer-bar")

    def on_mount(self) -> None:
        """Apply the initial layout, draw once, and start the poll timer."""
        self._apply_layout(self.size.width)
        self._refresh_data()
        # 0.5 s: snappier than the 2 s warm grid so new samples appear promptly; the reads are trivial
        self.set_interval(0.5, self._refresh_data)

    def on_resize(self, event: events.Resize) -> None:
        """Re-evaluate the stacked vs two-column layout on every resize."""
        self._apply_layout(event.size.width)

    def _apply_layout(self, width: int) -> None:
        """Toggle the `wide` class on #main: two columns when the terminal is wide enough."""
        # 120 columns: enough for a useful sparkline and the events pane side by side
        self.query_one("#main").set_class(width >= 120, "wide")

    def _refresh_data(self) -> None:
        """Poll the database and update the banner, sparklines, events, and footer."""
        warm = self._core.warm_history(self._HISTORY_LIMIT)
        cold = self._core.cold_history(self._HISTORY_LIMIT)
        dns = self._core.dns_history(self._HISTORY_LIMIT)
        vpn = self._core.vpn_history(self._EVENTS_LIMIT)
        ip = self._core.ip_history(self._EVENTS_LIMIT)

        self.query_one(BannerWidget).update_data(warm, cold, dns, vpn, ip, now=time.time())
        self.query_one("#latency-warm", LatencyWidget).update_data(warm)
        self.query_one("#latency-cold", LatencyWidget).update_data(cold)
        self.query_one("#dns", DnsWidget).update_data(dns)
        self.query_one(EventsWidget).update_data(vpn, ip)

        status = monitor.monitor_status(self._core)
        if status.pid is not None:
            footer = Text(f"monitor: running · pid {status.pid}", style="dim green")
        else:
            footer = Text("monitor: not running", style="dim red")
        footer.append("    w warm  c cold  d dns  v vpn  i ip  q quit", style="dim")
        self.query_one("#footer-bar", Static).update(footer)

    def action_warm_history(self) -> None:
        """Open the warm-latency history screen."""
        self.push_screen(LatencyHistoryScreen(self._core, "warm"))

    def action_cold_history(self) -> None:
        """Open the cold-latency history screen."""
        self.push_screen(LatencyHistoryScreen(self._core, "cold"))

    def action_dns_history(self) -> None:
        """Open the DNS history screen."""
        self.push_screen(DnsHistoryScreen(self._core))

    def action_vpn_history(self) -> None:
        """Open the VPN history screen."""
        self.push_screen(VpnHistoryScreen(self._core))

    def action_ip_history(self) -> None:
        """Open the IP history screen."""
        self.push_screen(IpHistoryScreen(self._core))
