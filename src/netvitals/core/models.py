"""Domain models: probe samples, the combined snapshot, and stored rows."""

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Self


class TunnelMode(StrEnum):
    """How a VPN tunnel captures traffic."""

    FULL = "full"  # Default egress goes through the tunnel
    SPLIT = "split"  # A tunnel exists but default egress bypasses it


class DnsError(StrEnum):
    """Failure category of a single resolver query."""

    TIMEOUT = "timeout"  # No response within the timeout
    NETWORK = "network"  # Socket-level error (e.g. unreachable through a down tunnel)
    MALFORMED = "malformed"  # Response could not be parsed
    SERVFAIL = "servfail"  # Resolver replied SERVFAIL
    NXDOMAIN = "nxdomain"  # Resolver replied NXDOMAIN for the canary
    REFUSED = "refused"  # Resolver refused the query
    OTHER = "other"  # Any other non-success rcode


@dataclass(frozen=True, slots=True)
class LatencySample:
    """Outcome of one latency measurement."""

    latency_ms: float | None  # HTTP round-trip in ms; None when every endpoint failed
    endpoint: str | None  # URL that answered; None when down


@dataclass(frozen=True, slots=True)
class ResolverSample:
    """Outcome of querying a single system resolver."""

    address: str  # Resolver IP as reported by scutil (IPv4 or IPv6)
    latency_ms: float | None  # UDP round-trip in ms; set whenever the exchange completed, even on an error rcode
    error: DnsError | None  # Failure category; None on clean success


@dataclass(frozen=True, slots=True)
class DnsSample:
    """Outcome of one DNS probe cycle across all system resolvers."""

    resolvers: list[ResolverSample]  # System order; [0] is the primary; empty = no DNS configuration


@dataclass(frozen=True, slots=True)
class VpnSample:
    """Outcome of one VPN detection cycle."""

    active: bool  # Whether a live tunnel interface exists
    mode: TunnelMode | None  # full/split; None when inactive or the routing lookup failed
    interface: str | None  # The tunnel carrying default egress when full, else the first detected tunnel
    provider: str | None  # VPN service name from scutil --nc; None when not identified


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Combined outcome of running every probe once."""

    latency_warm: LatencySample  # Steady-state latency over a warmed keep-alive connection
    latency_cold: LatencySample  # Connection-setup latency over a fresh session
    vpn: VpnSample  # VPN state
    ip: str | None  # Public IPv4 address; None when undetectable
    country: str | None  # 2-letter ISO country code; None when unresolved
    dns: DnsSample  # System resolver measurements


@dataclass(frozen=True, slots=True)
class LatencyRow:
    """One stored latency sample; warm and cold tables share this shape."""

    created_at: float  # UTC Unix seconds the sample was taken
    latency_ms: float | None  # HTTP round-trip in ms; None when every endpoint failed
    endpoint: str | None  # URL that answered; None when down

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build from a `latency_warm` / `latency_cold` row."""
        return cls(created_at=row["created_at"], latency_ms=row["latency_ms"], endpoint=row["endpoint"])


@dataclass(frozen=True, slots=True)
class DnsRow:
    """One stored DNS cycle: the primary resolver denormalized, the full list parsed from JSON."""

    created_at: float  # UTC Unix seconds the cycle ran
    primary_ms: float | None  # Primary resolver round-trip in ms; mirrors resolvers[0]
    primary_error: DnsError | None  # Primary resolver failure category; None on clean success
    primary_address: str | None  # Primary resolver IP; None when the system reported no resolvers
    resolvers: list[ResolverSample]  # Every resolver's outcome, system order

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build from a `dns` row."""
        resolvers = [
            ResolverSample(address=r["address"], latency_ms=r["latency_ms"], error=DnsError(r["error"]) if r["error"] else None)
            for r in json.loads(row["resolvers_json"])
        ]
        return cls(
            created_at=row["created_at"],
            primary_ms=row["primary_ms"],
            primary_error=DnsError(row["primary_error"]) if row["primary_error"] else None,
            primary_address=row["primary_address"],
            resolvers=resolvers,
        )


@dataclass(frozen=True, slots=True)
class VpnRow:
    """One stored VPN state; deduplicated, so a row spans `created_at`..`updated_at`."""

    created_at: float  # UTC Unix seconds this state was first seen
    updated_at: float  # UTC Unix seconds this state was last confirmed
    active: bool  # Whether a live tunnel interface existed
    mode: TunnelMode | None  # full/split; None when inactive or undetermined
    interface: str | None  # Tunnel interface name; None when inactive
    provider: str | None  # VPN service name; None when not identified

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build from a `vpn` row."""
        return cls(
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            active=bool(row["active"]),
            mode=TunnelMode(row["mode"]) if row["mode"] else None,
            interface=row["interface"],
            provider=row["provider"],
        )


@dataclass(frozen=True, slots=True)
class IpRow:
    """One stored public-IP state; deduplicated, so a row spans `created_at`..`updated_at`."""

    created_at: float  # UTC Unix seconds this state was first seen
    updated_at: float  # UTC Unix seconds this state was last confirmed
    ip: str | None  # Public IPv4 address; None when undetectable
    country: str | None  # 2-letter ISO country code; None when unresolved

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build from an `ip` row."""
        return cls(created_at=row["created_at"], updated_at=row["updated_at"], ip=row["ip"], country=row["country"])


@dataclass(frozen=True, slots=True)
class MonitorState:
    """The monitor's own liveness record — one row, rewritten on every heartbeat."""

    pid: int  # Process id of the monitor that wrote this
    started_at: float  # UTC Unix seconds when that monitor started
    updated_at: float  # UTC Unix seconds of the last heartbeat; its age is how fresh the data is

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Build from the `monitor_state` row."""
        return cls(pid=row["pid"], started_at=row["started_at"], updated_at=row["updated_at"])
