"""Dashboard widgets: the status banner, the sparkline panels, and the events list."""

from collections.abc import Callable
from importlib.metadata import version
from typing import Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from netvitals.core.models import DnsRow, IpRow, LatencyRow, VpnRow
from netvitals.status import COLD_OK_MS, COLD_SLOW_MS, WARM_OK_MS, WARM_SLOW_MS
from netvitals.tui.utils import latency_style, local_time


class BannerWidget(Static):
    """One-line status banner built from the newest element of each history."""

    DEFAULT_CSS = """
    BannerWidget {
        height: 1;
        padding: 0 1;
    }
    """

    _VERSION = version("netvitals")  # Read once: metadata lookup hits the filesystem, the banner redraws twice a second

    def update_data(
        self, warm: list[LatencyRow], cold: list[LatencyRow], dns: list[DnsRow], vpn: list[VpnRow], ip: list[IpRow], *, now: float
    ) -> None:
        """Rebuild the banner from the given histories and re-render."""
        # Stale windows are 3-5x the monitor cadence (2 s warm, 10 s cold/dns): one missed
        # cycle passes silently, a stopped monitor dims the numbers within seconds.
        warm_stale = bool(warm) and now - warm[-1].created_at > 10.0
        cold_stale = bool(cold) and now - cold[-1].created_at > 30.0
        dns_stale = bool(dns) and now - dns[-1].created_at > 30.0

        banner = Text("netvitals", style="bold")
        banner.append(f" v{self._VERSION}", style="dim")
        banner.append("    ")
        banner.append_text(self._latency_status("warm", warm[-1] if warm else None, WARM_OK_MS, WARM_SLOW_MS, stale=warm_stale))
        banner.append("    ")
        banner.append_text(self._latency_status("cold", cold[-1] if cold else None, COLD_OK_MS, COLD_SLOW_MS, stale=cold_stale))
        banner.append("    ")
        banner.append_text(self._dns_status(dns[-1] if dns else None, stale=dns_stale))
        banner.append("    ")
        banner.append_text(self._vpn_status(vpn[-1] if vpn else None))
        banner.append("    ")
        banner.append_text(self._ip_status(ip[-1] if ip else None))
        self.update(banner)

    @staticmethod
    def _latency_status(label: str, row: LatencyRow | None, ok_ms: int, slow_ms: int, *, stale: bool) -> Text:
        """Format one latency series: colored dot for health, dimmed when untrustworthy."""
        if stale:
            return Text(f"● {label} stale", style="dim")
        if row is None:
            return Text(f"● {label} ?", style="dim")
        if row.latency_ms is None:
            return BannerWidget._dot("✕ ", "bold red", f"{label} down")
        return BannerWidget._dot("● ", latency_style(row.latency_ms, ok_ms, slow_ms), f"{label} {row.latency_ms:.0f}ms")

    @staticmethod
    def _dns_status(row: DnsRow | None, *, stale: bool) -> Text:
        """Format DNS: the primary resolver only."""
        if stale:
            return Text("● DNS stale", style="dim")
        if row is None:
            return Text("● DNS ?", style="dim")
        if row.primary_address is None:
            return BannerWidget._dot("✕ ", "bold red", "DNS no config")
        if row.primary_error is not None:
            return BannerWidget._dot("✕ ", "bold red", f"DNS {row.primary_error}")
        if row.primary_ms is None:
            return Text("● DNS ?", style="dim")
        return BannerWidget._dot("● ", "green", f"DNS {row.primary_ms:.0f}ms")

    @staticmethod
    def _vpn_status(row: VpnRow | None) -> Text:
        """Format the VPN state."""
        if row is None:
            return Text("● VPN ?", style="dim")
        if not row.active:
            return Text("● VPN off", style="dim")
        body = f"VPN {row.mode or 'on'}"
        if row.provider:
            body += f" · {row.provider}"
        return BannerWidget._dot("● ", "green", body)

    @staticmethod
    def _ip_status(row: IpRow | None) -> Text:
        """Format the public IP."""
        if row is None or row.ip is None:
            return Text("● IP ?", style="dim")
        body = f"{row.ip} ({row.country})" if row.country else row.ip
        return BannerWidget._dot("● ", "green", body)

    @staticmethod
    def _dot(glyph: str, glyph_style: str, body: str) -> Text:
        """Return *body* prefixed with a styled *glyph*; only the glyph carries color."""
        text = Text()
        text.append(glyph, style=glyph_style)
        text.append(body)
        return text


class SparkPanel[RowT](Widget):
    """Base for the sparkline panels: a bordered box, right-aligned graph, stats line below."""

    DEFAULT_CSS = """
    SparkPanel {
        height: auto;
        max-height: 6;
        border: round $accent;
        border-title-color: $text;
        padding: 0 1;
    }
    """

    _SPARK_CHARS = "▁▂▃▄▅▆▇█"  # Bar glyphs, lowest to highest; every sparkline scales into this ramp

    def __init__(self, dom_id: str, title: str) -> None:
        """Initialize with a DOM id and a border title."""
        super().__init__(id=dom_id)
        self.border_title = title
        self._history: list[RowT] = []  # Latest samples, oldest first

    def update_data(self, history: list[RowT]) -> None:
        """Set new samples and trigger a re-render."""
        self._history = history
        self.refresh()

    def render(self) -> Text:
        """Render the sparkline right-aligned (newest at the right edge) with stats below."""
        width = self.scrollable_content_region.width
        visible = self._history[-width:] if width > 0 else self._history
        text = self._spark(visible)
        if width > 0 and len(text) < width:
            text.pad_left(width - len(text))
        text.append("\n")
        text.append_text(self._stats(visible))
        return text

    def _spark(self, visible: list[RowT]) -> Text:
        """Build the sparkline for the visible window."""
        raise NotImplementedError

    def _stats(self, visible: list[RowT]) -> Text:
        """Build the stats line under the sparkline."""
        raise NotImplementedError

    def _sparkline(self, values: list[float | None], style_of: Callable[[float], str]) -> Text:
        """Scale values into the bar ramp; None (a failed sample) becomes an ✕ mark."""
        if not values:
            return Text("no data", style="dim")
        max_value = max((v for v in values if v is not None), default=1.0)
        text = Text()
        for v in values:
            if v is None:
                text.append("✕", style="dim red")
            else:
                idx = min(int(v / max_value * (len(self._SPARK_CHARS) - 1)), len(self._SPARK_CHARS) - 1)
                text.append(self._SPARK_CHARS[idx], style=style_of(v))
        return text

    @staticmethod
    def _summary(values: list[float]) -> str:
        """Build the min/avg/p95/max block shared by every stats line; *values* must be non-empty."""
        ordered = sorted(values)
        avg = sum(ordered) / len(ordered)
        p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
        return f"min {ordered[0]:.0f}    avg {avg:.0f}    p95 {p95:.0f}    max {ordered[-1]:.0f}"


class LatencyWidget(SparkPanel[LatencyRow]):
    """Latency sparkline panel, parametrized by probe kind (warm or cold)."""

    def __init__(self, kind: Literal["warm", "cold"], ok_ms: int, slow_ms: int) -> None:
        """Initialize for one probe kind; *kind* drives the DOM id (`latency-warm` / `latency-cold`) and title."""
        super().__init__(f"latency-{kind}", f"Latency ({kind})")
        self._ok_ms = ok_ms  # Healthy threshold (ms) for coloring
        self._slow_ms = slow_ms  # Slow threshold (ms) for coloring

    def _spark(self, visible: list[LatencyRow]) -> Text:
        """Bars colored by the latency bands; down samples become ✕ marks."""
        return self._sparkline([row.latency_ms for row in visible], lambda ms: latency_style(ms, self._ok_ms, self._slow_ms))

    def _stats(self, visible: list[LatencyRow]) -> Text:
        """min/avg/p95/max plus a down count when there is one."""
        values = [row.latency_ms for row in visible if row.latency_ms is not None]
        down = sum(1 for row in visible if row.latency_ms is None)
        if not values:
            return Text(f"down {down}", style="dim")
        text = Text(self._summary(values), style="dim")
        if down:
            text.append(f"    down {down}", style="dim red")
        return text


class DnsWidget(SparkPanel[DnsRow]):
    """DNS sparkline panel; shows the primary resolver only."""

    def __init__(self) -> None:
        """Initialize the DNS widget."""
        super().__init__("dns", "DNS")

    def _spark(self, visible: list[DnsRow]) -> Text:
        """Bars for primary-resolver latency; any error becomes an ✕ mark."""
        # An error rcode can still carry a latency; it is a failure to the user either way.
        values = [None if row.primary_error is not None else row.primary_ms for row in visible]
        return self._sparkline(values, lambda _: "cyan")

    def _stats(self, visible: list[DnsRow]) -> Text:
        """Build the stats line: resolver address, min/avg/p95/max, errors, extra-resolver hint."""
        if not visible:
            return Text("", style="dim")
        latest = visible[-1]
        values = [row.primary_ms for row in visible if row.primary_error is None and row.primary_ms is not None]
        errors = sum(1 for row in visible if row.primary_error is not None)
        text = Text()
        if latest.primary_address is None:
            text.append("no config", style="dim red")
        else:
            text.append(latest.primary_address, style="dim")
        if values:
            text.append(f"    {self._summary(values)}", style="dim")
        if errors:
            text.append(f"    errors {errors}", style="dim red")
        if (extra := len(latest.resolvers) - 1) > 0:
            text.append(f"    +{extra}", style="dim")
        return text


class EventsWidget(VerticalScroll):
    """Merged VPN/IP change list, newest first; scrolls internally when it overflows."""

    DEFAULT_CSS = """
    EventsWidget {
        border: round $accent;
        border-title-color: $text;
        padding: 0 1;
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        """Initialize the events panel."""
        super().__init__()
        self.border_title = "Events"

    def compose(self) -> ComposeResult:
        """Create the inner static that holds the rendered list."""
        yield Static(id="events-body")

    def update_data(self, vpn_rows: list[VpnRow], ip_rows: list[IpRow]) -> None:
        """Merge both change lists newest-first and update the inner static."""
        entries: list[tuple[float, str]] = []
        for vpn in vpn_rows:
            if not vpn.active:
                label = "off"
            else:
                label = f"{vpn.mode or 'on'}"
                if vpn.provider:
                    label += f" {vpn.provider}"
            entries.append((vpn.created_at, f"{local_time(vpn.created_at, '%H:%M:%S')}  VPN  {label}"))
        for ip in ip_rows:
            body = f"{ip.ip} ({ip.country})" if ip.ip and ip.country else ip.ip or "?"
            entries.append((ip.created_at, f"{local_time(ip.created_at, '%H:%M:%S')}  IP   {body}"))
        entries.sort(key=lambda e: e[0], reverse=True)
        text = Text("\n".join(line for _, line in entries)) if entries else Text("no events", style="dim")
        self.query_one("#events-body", Static).update(text)
