# Storage

SQLite database at `<data-dir>/netvitals.db`, written only by the monitor and read by every other
client. This document explains the schema and the rules behind it; `src/netvitals/core/db.py` is
where they are actually enforced — when the two disagree, the code is what runs.

All timestamps are UTC Unix seconds stored as `REAL`. The storage layer never reads the clock:
callers pass the time in, so every row a single measurement cycle writes carries the same instant.

## Connection settings

- **WAL** journaling, so readers (dashboard, tray, `status`) never block the writer and vice versa.
- **`synchronous = NORMAL`** — no fsync per commit. At a 2-second cadence that cost is real, and
  losing the last few samples to a power cut costs nothing that matters.
- **`busy_timeout = 5000`** ms, so a reader that arrives mid-commit waits instead of failing.
- Tables are `STRICT`: a wrong type is a write-time error, not a surprise months later.

## Tables

`latency_warm`, `latency_cold` — one row per cycle, append-only:

| column | type | notes |
|---|---|---|
| `created_at` | REAL | when the sample was taken |
| `latency_ms` | REAL | NULL when every endpoint failed — that is what "down" looks like |
| `endpoint` | TEXT | the URL that answered; NULL when down. A change here marks a failover |

`dns` — one row per cycle, append-only:

| column | type | notes |
|---|---|---|
| `created_at` | REAL | |
| `primary_ms` | REAL | mirrors `resolvers[0].latency_ms` |
| `primary_error` | TEXT | mirrors `resolvers[0].error` |
| `primary_address` | TEXT | mirrors `resolvers[0].address` |
| `resolvers_json` | TEXT | every probed resolver as `[{address, latency_ms, error}, …]` |

The `primary_*` columns exist so status queries and sparklines need no JSON parsing, while the full
resolver list stays available for a breakdown view. All three are NULL together when the system
reported no resolvers at all — that is its own diagnostic, not a missing measurement.

`vpn`, `ip` — one row per *state change*, deduplicated (see below):

| `vpn` column | type | notes |
|---|---|---|
| `created_at` | REAL | when this state first appeared |
| `updated_at` | REAL | when it was last confirmed |
| `active` | INTEGER | 1 when a live tunnel exists |
| `mode` | TEXT | `full` / `split`; NULL when inactive or the routing lookup failed |
| `interface` | TEXT | the tunnel carrying egress when full, else the first tunnel found |
| `provider` | TEXT | service name from `scutil --nc`; NULL when not identified |

`ip` holds `created_at`, `updated_at`, `ip`, `country` under the same rules.

`monitor_state` — a single row (`id = 1`) with `pid`, `started_at`, `updated_at`, rewritten on every
heartbeat. It lets a reader tell "the monitor is down" from "the network is down", which sample age
alone cannot distinguish.

## Deduplication of state tables

VPN state changes on a scale of hours and a public IP even less often, so storing every check would
mean roughly 260k identical `vpn` rows a month. Instead each check compares against the newest row:

- **Unchanged** → bump `updated_at`. No new row.
- **Changed** → insert a new row with `created_at = updated_at = now`.

Compared fields are `(active, mode, interface, provider)` and `(ip, country)`. The consequence for
queries: `created_at` is the timeline of genuine changes (what an events view wants), while
`updated_at` is "still true as of".

## Retention

Purged hourly and at startup; the horizon is 30 days (`Core.purge`).

Sample tables purge on `created_at`. The deduplicated tables purge on `updated_at` — a state that
has been confirmed for months is current data living in an old row, and deleting it would delete the
state itself.

## Migrations

Ordered SQL strings in `_MIGRATIONS`; the index of a migration is the `PRAGMA user_version` it
produces. Each runs inside an explicit transaction together with its version bump, so a crash
halfway leaves the old version rather than a half-applied schema. Adding a migration means appending
to that tuple — never editing an existing one, which would leave already-migrated databases behind.
