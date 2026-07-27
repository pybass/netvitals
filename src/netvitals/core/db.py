"""SQLite storage for what the monitor measures: schema, writes, and retention.

Timestamps are UTC Unix seconds (REAL) everywhere, and callers pass them in — the
storage layer never reads the clock, so one cycle's rows all carry the same instant.
"""

import json
import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path

from netvitals.core.models import DnsRow, DnsSample, IpRow, LatencyRow, LatencySample, MonitorState, VpnRow, VpnSample

log = logging.getLogger(__name__)

_MIGRATION_V1 = """
CREATE TABLE latency_warm (created_at REAL NOT NULL, latency_ms REAL, endpoint TEXT) STRICT;
CREATE INDEX idx_latency_warm_created_at ON latency_warm(created_at);

CREATE TABLE latency_cold (created_at REAL NOT NULL, latency_ms REAL, endpoint TEXT) STRICT;
CREATE INDEX idx_latency_cold_created_at ON latency_cold(created_at);

CREATE TABLE dns (
    created_at REAL NOT NULL,
    primary_ms REAL,
    primary_error TEXT,
    primary_address TEXT,
    resolvers_json TEXT NOT NULL
) STRICT;
CREATE INDEX idx_dns_created_at ON dns(created_at);

CREATE TABLE vpn (
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    active INTEGER NOT NULL,
    mode TEXT,
    interface TEXT,
    provider TEXT
) STRICT;
CREATE INDEX idx_vpn_created_at ON vpn(created_at);
CREATE INDEX idx_vpn_updated_at ON vpn(updated_at);

CREATE TABLE ip (created_at REAL NOT NULL, updated_at REAL NOT NULL, ip TEXT, country TEXT) STRICT;
CREATE INDEX idx_ip_created_at ON ip(created_at);
CREATE INDEX idx_ip_updated_at ON ip(updated_at);

CREATE TABLE monitor_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
"""

_MIGRATIONS: tuple[str, ...] = (_MIGRATION_V1,)
"""Ordered schema migrations; the index in this tuple is the `PRAGMA user_version` it produces."""

_PURGE_STATEMENTS: tuple[str, ...] = (
    "DELETE FROM latency_warm WHERE created_at < ?",
    "DELETE FROM latency_cold WHERE created_at < ?",
    "DELETE FROM dns WHERE created_at < ?",
    # The deduplicated state tables purge by updated_at: a state that has been confirmed for
    # months is current data living in an old row, and deleting it would lose the state itself.
    "DELETE FROM vpn WHERE updated_at < ?",
    "DELETE FROM ip WHERE updated_at < ?",
)


class Db:
    """Database access for measurement data. The monitor writes; every other client reads."""

    def __init__(self, path: Path) -> None:
        """Open the database, apply pragmas, and run pending migrations; the directory must exist (Core makes it)."""
        # Owned by the creating thread; the monitor is single-threaded. autocommit=True gives every
        # statement its own transaction, so a write can never be left hanging in an implicit one;
        # the few multi-statement writes take an explicit BEGIN/COMMIT instead.
        self.conn = sqlite3.connect(path, autocommit=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")  # Readers never block the writer, and vice versa
        # A dropped sample after a power cut is acceptable; an fsync per commit at a 2 s cadence is not.
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")  # Milliseconds to wait out a competing writer
        self._migrate()

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()

    def _migrate(self) -> None:
        """Apply every migration newer than `PRAGMA user_version`, each in its own transaction."""
        version: int = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for target, migration in enumerate(_MIGRATIONS, start=1):
            if version >= target:
                continue
            # Explicit BEGIN/COMMIT so the schema change and the version bump land together: a crash
            # halfway must leave the old version, never a half-applied schema. executescript() cannot
            # be used for the same reason — it commits implicitly.
            self.conn.execute("BEGIN")
            for statement in migration.split(";"):
                if statement.strip():
                    self.conn.execute(statement)
            self.conn.execute(f"PRAGMA user_version = {target}")  # PRAGMA takes no query parameters
            self.conn.execute("COMMIT")
            log.info("db: applied migration v%d", target)

    # -- Latency ---------------------------------------------------------------

    def insert_warm_latency(self, created_at: float, sample: LatencySample) -> None:
        """Append one warm-latency sample."""
        self.conn.execute(
            "INSERT INTO latency_warm (created_at, latency_ms, endpoint) VALUES (?, ?, ?)",
            (created_at, sample.latency_ms, sample.endpoint),
        )

    def insert_cold_latency(self, created_at: float, sample: LatencySample) -> None:
        """Append one cold-latency sample."""
        self.conn.execute(
            "INSERT INTO latency_cold (created_at, latency_ms, endpoint) VALUES (?, ?, ?)",
            (created_at, sample.latency_ms, sample.endpoint),
        )

    # -- DNS -------------------------------------------------------------------

    def insert_dns(self, created_at: float, sample: DnsSample) -> None:
        """Append one DNS cycle.

        The `primary_*` columns mirror `resolvers[0]` so status queries need no JSON parsing;
        the full per-resolver list stays in `resolvers_json`. All three are NULL together when
        the system reported no resolvers at all.
        """
        primary = sample.resolvers[0] if sample.resolvers else None
        self.conn.execute(
            "INSERT INTO dns (created_at, primary_ms, primary_error, primary_address, resolvers_json) VALUES (?, ?, ?, ?, ?)",
            (
                created_at,
                primary.latency_ms if primary else None,
                primary.error if primary else None,
                primary.address if primary else None,
                json.dumps([asdict(resolver) for resolver in sample.resolvers]),
            ),
        )

    # -- VPN and public IP (deduplicated state) --------------------------------

    def upsert_vpn(self, now: float, sample: VpnSample) -> None:
        """Record VPN state: bump `updated_at` when unchanged, insert a new row on a real change.

        VPN state changes on a scale of hours, so storing every 10-second check would be ~260k
        near-identical rows a month; deduplicated, it is one row per change.
        """
        current = (sample.active, sample.mode, sample.interface, sample.provider)  # The fields that define a change
        latest = self.conn.execute(
            "SELECT rowid, active, mode, interface, provider FROM vpn ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if latest is not None and (bool(latest["active"]), latest["mode"], latest["interface"], latest["provider"]) == current:
            self.conn.execute("UPDATE vpn SET updated_at = ? WHERE rowid = ?", (now, latest["rowid"]))
        else:
            self.conn.execute(
                "INSERT INTO vpn (created_at, updated_at, active, mode, interface, provider) VALUES (?, ?, ?, ?, ?, ?)",
                (now, now, int(sample.active), sample.mode, sample.interface, sample.provider),
            )

    def upsert_ip(self, now: float, ip: str | None, country: str | None) -> None:
        """Record the public-IP state, deduplicated the same way as `upsert_vpn`."""
        latest = self.conn.execute("SELECT rowid, ip, country FROM ip ORDER BY updated_at DESC LIMIT 1").fetchone()
        if latest is not None and (latest["ip"], latest["country"]) == (ip, country):
            self.conn.execute("UPDATE ip SET updated_at = ? WHERE rowid = ?", (now, latest["rowid"]))
        else:
            self.conn.execute("INSERT INTO ip (created_at, updated_at, ip, country) VALUES (?, ?, ?, ?)", (now, now, ip, country))

    def fetch_country_for_ip(self, ip: str) -> str | None:
        """Return the country last recorded for *ip*; None when this address has none.

        Lets the monitor skip a quota-limited country lookup for an address it has seen before —
        an IP's country does not change within our retention horizon.
        """
        row = self.conn.execute(
            "SELECT country FROM ip WHERE ip = ? AND country IS NOT NULL ORDER BY updated_at DESC LIMIT 1", (ip,)
        ).fetchone()
        return row["country"] if row else None

    # -- History reads: the newest rows, returned oldest-first -------------------
    #
    # Chronological order is what every consumer renders (sparklines, event lists), so the
    # DESC LIMIT window is reversed here once instead of by each caller.

    def fetch_recent_warm(self, limit: int) -> list[LatencyRow]:
        """Return the newest *limit* warm-latency samples, oldest first."""
        rows = self.conn.execute(
            "SELECT created_at, latency_ms, endpoint FROM latency_warm ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [LatencyRow.from_row(row) for row in reversed(rows)]

    def fetch_recent_cold(self, limit: int) -> list[LatencyRow]:
        """Return the newest *limit* cold-latency samples, oldest first."""
        rows = self.conn.execute(
            "SELECT created_at, latency_ms, endpoint FROM latency_cold ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [LatencyRow.from_row(row) for row in reversed(rows)]

    def fetch_recent_dns(self, limit: int) -> list[DnsRow]:
        """Return the newest *limit* DNS cycles, oldest first."""
        rows = self.conn.execute(
            "SELECT created_at, primary_ms, primary_error, primary_address, resolvers_json FROM dns "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [DnsRow.from_row(row) for row in reversed(rows)]

    def fetch_recent_vpn(self, limit: int) -> list[VpnRow]:
        """Return the newest *limit* VPN state changes, oldest first; the last one is the current state."""
        rows = self.conn.execute(
            "SELECT created_at, updated_at, active, mode, interface, provider FROM vpn ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [VpnRow.from_row(row) for row in reversed(rows)]

    def fetch_recent_ip(self, limit: int) -> list[IpRow]:
        """Return the newest *limit* public-IP state changes, oldest first; the last one is the current state."""
        rows = self.conn.execute(
            "SELECT created_at, updated_at, ip, country FROM ip ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [IpRow.from_row(row) for row in reversed(rows)]

    # -- Monitor liveness and retention ----------------------------------------

    def update_heartbeat(self, pid: int, started_at: float, now: float) -> None:
        """Record that the monitor is alive, so readers can tell "monitor is down" from "network is down"."""
        self.conn.execute(
            "INSERT INTO monitor_state (id, pid, started_at, updated_at) VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE "
            "SET pid = excluded.pid, started_at = excluded.started_at, updated_at = excluded.updated_at",
            (pid, started_at, now),
        )

    def fetch_monitor_state(self) -> MonitorState | None:
        """Return the monitor's last heartbeat; None when no monitor has ever run here."""
        row = self.conn.execute("SELECT pid, started_at, updated_at FROM monitor_state WHERE id = 1").fetchone()
        return MonitorState.from_row(row) if row else None

    def purge(self, cutoff: float) -> int:
        """Delete data older than *cutoff* (UTC Unix seconds) from every table. Return rows deleted."""
        deleted = 0
        self.conn.execute("BEGIN")  # One transaction for all tables: retention is a single logical operation
        for statement in _PURGE_STATEMENTS:
            deleted += self.conn.execute(statement, (cutoff,)).rowcount
        self.conn.execute("COMMIT")
        return deleted
