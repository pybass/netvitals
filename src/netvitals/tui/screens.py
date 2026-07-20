"""History screens: a DataTable per probe series over the recorded rows, newest first."""

from collections.abc import Iterable
from typing import ClassVar, Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Static

from netvitals.core.core import Core
from netvitals.status import COLD_OK_MS, COLD_SLOW_MS, WARM_OK_MS, WARM_SLOW_MS
from netvitals.tui.utils import latency_style, local_time


class HistoryScreen(Screen[None]):
    """Base for the history screens: a title bar, a row-cursor table, and a hint line."""

    _HISTORY_LIMIT = 200  # Rows fetched per screen; every title quotes the same number
    _TIME_FMT = "%Y-%m-%d %H:%M:%S"  # Full local timestamp for table cells

    CSS = """
    #title { height: 1; padding: 0 1; background: $accent; color: $text; text-style: bold; }
    #hint { dock: bottom; height: 1; padding: 0 1; color: $text-muted; }
    DataTable { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Back"),
        Binding("q", "dismiss", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, core: Core, title: str, columns: tuple[str, ...]) -> None:
        """Initialize with the application core, the title-bar text, and the table columns."""
        super().__init__()
        self._core = core  # All reads go through here
        self._title = title  # Title-bar text
        self._columns = columns  # Table column labels

    def compose(self) -> ComposeResult:
        """Build the screen layout."""
        yield Static(self._title, id="title")
        yield DataTable[Text | str]()
        yield Static("r refresh    esc/q back", id="hint")

    def on_mount(self) -> None:
        """Configure the table and load the rows."""
        table: DataTable[Text | str] = self.query_one(DataTable)
        table.add_columns(*self._columns)
        table.cursor_type = "row"
        self._reload()

    def action_refresh(self) -> None:
        """Refetch rows from the database."""
        self._reload()

    def _reload(self) -> None:
        """Clear the table and refill it from `_rows`."""
        table: DataTable[Text | str] = self.query_one(DataTable)
        table.clear()
        for row in self._rows():
            table.add_row(*row)

    def _rows(self) -> Iterable[tuple[Text | str, ...]]:
        """Yield one cell tuple per table row, newest first."""
        raise NotImplementedError


class LatencyHistoryScreen(HistoryScreen):
    """Recent latency samples of one kind (warm or cold), newest first."""

    def __init__(self, core: Core, kind: Literal["warm", "cold"]) -> None:
        """Initialize for one probe kind."""
        super().__init__(core, f"Latency history ({kind}) — last {self._HISTORY_LIMIT} samples", ("Time", "Latency", "Endpoint"))
        self._kind: Literal["warm", "cold"] = kind  # Which latency series this screen shows

    def _rows(self) -> Iterable[tuple[Text | str, ...]]:
        """One row per sample; the latency cell is colored by the kind's bands."""
        if self._kind == "warm":
            history, ok_ms, slow_ms = self._core.warm_history(self._HISTORY_LIMIT), WARM_OK_MS, WARM_SLOW_MS
        else:
            history, ok_ms, slow_ms = self._core.cold_history(self._HISTORY_LIMIT), COLD_OK_MS, COLD_SLOW_MS
        for row in reversed(history):
            if row.latency_ms is None:
                latency: Text | str = Text("down", style="bold red")
            else:
                latency = Text(f"{row.latency_ms:.0f} ms", style=latency_style(row.latency_ms, ok_ms, slow_ms))
            yield local_time(row.created_at, self._TIME_FMT), latency, row.endpoint or "-"


class DnsHistoryScreen(HistoryScreen):
    """Recent DNS cycles expanded to one row per resolver, newest first.

    The only view that exposes non-primary resolvers over time — the dashboard
    sparkline shows the primary only.
    """

    def __init__(self, core: Core) -> None:
        """Initialize the DNS history screen."""
        title = f"DNS history — last {self._HISTORY_LIMIT} cycles (one row per resolver)"
        super().__init__(core, title, ("Time", "Role", "Resolver", "ms", "Error"))

    def _rows(self) -> Iterable[tuple[Text | str, ...]]:
        """Expand each cycle into per-resolver rows; a cycle with no resolvers becomes one `no config` row."""
        for cycle in reversed(self._core.dns_history(self._HISTORY_LIMIT)):
            when = local_time(cycle.created_at, self._TIME_FMT)
            if not cycle.resolvers:
                yield when, Text("—", style="dim"), Text("no config", style="bold red"), "-", "-"
                continue
            for idx, resolver in enumerate(cycle.resolvers):
                role = Text("primary", style="bold") if idx == 0 else Text(f"#{idx + 1}", style="dim")
                ms = f"{resolver.latency_ms:.0f}" if resolver.latency_ms is not None else "-"
                error = Text(resolver.error, style="bold red") if resolver.error else Text("-", style="dim")
                yield when, role, resolver.address, ms, error


class VpnHistoryScreen(HistoryScreen):
    """Recent VPN state changes, newest first."""

    def __init__(self, core: Core) -> None:
        """Initialize the VPN history screen."""
        title = f"VPN history — last {self._HISTORY_LIMIT} state changes"
        super().__init__(core, title, ("Time", "State", "Mode", "Interface", "Provider", "Last seen"))

    def _rows(self) -> Iterable[tuple[Text | str, ...]]:
        """One row per deduplicated VPN state; `Last seen` is when the state was last confirmed."""
        for row in reversed(self._core.vpn_history(self._HISTORY_LIMIT)):
            state = Text("on", style="green") if row.active else Text("off", style="dim")
            yield (
                local_time(row.created_at, self._TIME_FMT),
                state,
                row.mode or "-",
                row.interface or "-",
                row.provider or "-",
                local_time(row.updated_at, self._TIME_FMT),
            )


class IpHistoryScreen(HistoryScreen):
    """Recent public-IP changes, newest first."""

    def __init__(self, core: Core) -> None:
        """Initialize the IP history screen."""
        super().__init__(core, f"IP history — last {self._HISTORY_LIMIT} address changes", ("Time", "IP", "Country", "Last seen"))

    def _rows(self) -> Iterable[tuple[Text | str, ...]]:
        """One row per deduplicated address; `Last seen` is when it was last confirmed."""
        for row in reversed(self._core.ip_history(self._HISTORY_LIMIT)):
            yield (
                local_time(row.created_at, self._TIME_FMT),
                row.ip or "-",
                row.country or "-",
                local_time(row.updated_at, self._TIME_FMT),
            )
