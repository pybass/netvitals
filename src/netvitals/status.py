"""Derived status: the one verdict about the connection, plus the state behind it.

The stored rows say what was measured; the monitor's lock says whether anything is measuring right
now. Neither answers "is the connection ok" on its own — a green sample from an hour ago is not an
answer, and a missing sample means nothing when nothing is running. Combining them in one place is
what keeps a menu bar glyph, the dashboard, and a status command from disagreeing.

This lives outside `core/` because Core does not know that the monitor process exists (see
architecture.md), and the lock is exactly what separates a stopped monitor from a stuck one.
"""

import time
from dataclasses import dataclass
from enum import StrEnum

from netvitals import monitor
from netvitals.core.core import Core
from netvitals.core.models import DnsRow, IpRow, LatencyRow, VpnRow

# Latency classification bands, shared by every client that judges a number: the icon and the
# dashboard must call the same measurement healthy. Cold sits higher because every cold sample pays
# the TCP+TLS handshake on top of the round-trip.
WARM_OK_MS = 300  # Warm latency below this is healthy
WARM_SLOW_MS = 800  # Warm latency below this is slow; at or above is bad
COLD_OK_MS = 600  # Cold latency below this is healthy
COLD_SLOW_MS = 1500  # Cold latency below this is slow; at or above is bad

_STALE_AFTER = 5 * monitor.WARM_INTERVAL + Core.WARM_MEASURE_MAX
"""Seconds without a warm sample before the stored state stops counting as current.

Five cycles — a single missed cycle is ordinary jitter, five in a row means nothing is arriving —
plus the worst-case measurement: rows are stamped at cycle start, so during an outage every down
sample lands already up to WARM_MEASURE_MAX seconds old and would otherwise read as stale.
"""


class Health(StrEnum):
    """The verdict a client shows when it has room for exactly one."""

    OK = "ok"  # Fresh sample inside the healthy band
    SLOW = "slow"  # Fresh sample above the ok band
    BAD = "bad"  # Fresh sample above the slow band
    DOWN = "down"  # Fresh sample, and every endpoint failed: no connectivity
    STALE = "stale"  # The monitor holds its lock, but its samples stopped arriving
    MONITOR_OFF = "monitor_off"  # Nothing is measuring; whatever is stored is from the last run
    NO_DATA = "no_data"  # The monitor runs but has not recorded a warm sample yet


@dataclass(frozen=True, slots=True)
class Status:
    """The newest state of every series, and the single verdict derived from it."""

    health: Health  # The one-glyph answer; the rows below explain it
    warm: LatencyRow | None  # Newest warm-latency sample; None when none was ever recorded
    cold: LatencyRow | None  # Newest cold-latency sample
    dns: DnsRow | None  # Newest DNS cycle
    vpn: VpnRow | None  # Current VPN state
    ip: IpRow | None  # Current public IP


def current_status(core: Core) -> Status:
    """Read the newest row of every series and derive the verdict from it."""
    warm = _newest(core.warm_history(1))
    return Status(
        health=_health(warm, monitor.monitor_status(core).pid),
        warm=warm,
        cold=_newest(core.cold_history(1)),
        dns=_newest(core.dns_history(1)),
        vpn=_newest(core.vpn_history(1)),
        ip=_newest(core.ip_history(1)),
    )


def _newest[RowT](rows: list[RowT]) -> RowT | None:
    """Unwrap a history asked for one row: its newest element doubles as the current state."""
    return rows[0] if rows else None


def _health(warm: LatencyRow | None, monitor_pid: int | None) -> Health:
    """Answer who is measuring first, then how fresh the sample is, and only then how fast.

    Warm-driven on purpose. DNS and VPN failures stay visible in the detail rows, but folding them
    into the single verdict would make it flicker on one 10 s DNS timeout — and the failure that
    actually matters, a tunnel dropping, shows up in the warm series anyway.
    """
    if monitor_pid is None:
        return Health.MONITOR_OFF
    if warm is None:
        return Health.NO_DATA
    if time.time() - warm.created_at > _STALE_AFTER:
        return Health.STALE
    if warm.latency_ms is None:
        return Health.DOWN
    if warm.latency_ms < WARM_OK_MS:
        return Health.OK
    if warm.latency_ms < WARM_SLOW_MS:
        return Health.SLOW
    return Health.BAD
