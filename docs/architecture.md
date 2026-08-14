# Architecture

One rule shapes everything: **`core/` is the entire API of the application, and it knows
nothing about its consumers.** Core can run probes and read/write the database; how the
monitor, the CLI, or any future UI works is not its business.

## Layout

```
src/netvitals/
  core/            the application core -- the only API surface
    core.py        Core: the facade every consumer talks to
    models.py      shared types (public)
    errors.py      AppError (public)
    db.py  logs.py  utils.py  probes/    internals, private to core/
  monitor.py       the background monitor: scheduler + process control
  process.py       lock, detached spawn, stop handshake -- shared by monitor and tray
  status.py        the derived verdict: latency bands + Health + Status
  cli/             command-line adapter
  tui/             terminal dashboard adapter
  tray/            macOS menu bar adapter
  __main__.py      `python -m netvitals` -> cli
```

## Dependency rules

- `core/` imports nothing outside itself. It does not know that monitor, cli, tui, or
  tray exist.
- `process.py` imports only `Core` and `core.errors`; it knows nothing about what it
  starts beyond the argv it is handed.
- `monitor.py` imports only `Core`, `core.models`, `core.errors`, `process`.
- `status.py` imports only `Core`, `core.models`, `monitor` — the lock is what
  separates a stopped monitor from a stuck one, and Core cannot see it.
- Adapters (`cli/`, `tui/`, `tray/`) import only `Core`, `core.models`, `core.errors`,
  `monitor`, `process`, and `status`. One adapter-to-adapter edge exists: the CLI's
  `dashboard` command launches the TUI app (`cli -> tui`), and the same for `tray`.
- Nothing outside `core/` may import `core.db`, `core.logs`, `core.utils`, or
  `core.probes.*` — they are implementation details behind Core.

`start_monitor` launches the background process with `subprocess`
(`python -m netvitals ... monitor`), not with an import — so monitor→core is the only
edge between the two and no cycle exists.

## Core

One Core per process: the adapter entry point creates it (`data_dir`, `debug`) and closes
it. The API, grouped:

| Group | Members | Notes |
|---|---|---|
| Snapshot | `take_snapshot()` | live one-shot measurement of everything; records nothing |
| Sampling | `sample_warm/cold/dns/vpn/ip(now)`, `reset_warm()` | measure via probes **and** record to the database; the monitor's workhorses |
| History | `warm/cold/dns/vpn/ip_history(limit)` | the newest recorded rows, oldest first; the newest element doubles as the current state |
| Monitor data | `record_heartbeat(started_at, now)`, `monitor_state()` | Core stores and serves the heartbeat row without knowing what process writes it |
| Maintenance | `purge(now)` | delete rows past retention |
| Infra | `data_dir`, `DEFAULT_DATA_DIR`, `WARM_MEASURE_MAX`, `crash_log`, `monitor_lock`, `tray_lock`, `debug`, `log_to_terminal()`, `close()` | the directory is created, and logging configured, in `Core.__init__` |

Core owns the data directory: it creates it, it names everything in it, and it owns where the
directory sits without `--data-dir` (`DEFAULT_DATA_DIR`, a fixed `~/.local/share/netvitals` — see
[non-goals](non-goals.md)). A spawned client is given `--data-dir` only when it differs from that
default: the common case stays readable in `ps`, and the child resolves the same fixed default
itself.

Naming the two lock files is not a dependency on the clients that hold them — the same distinction
`record_heartbeat`/`monitor_state` already draw: Core owns the file, and what the holder does with
it is none of its business.

## The crash log

A detached client has no terminal, so `process.start_detached` points its stdout and stderr at
`<data-dir>/crash.log` (opened `"ab"`, so concurrent writers are safe). Everything it has to say
routinely goes through the logger into `netvitals.log`; what lands in the crash log is only what
never reached the logger — a traceback from before logging was wired, or from outside it — stamped
with a `netvitals crashed:` header by `cli.main`. The file stays empty in normal operation, so it
needs no rotation. That is also why `log_to_terminal()` mirrors the log to stderr only when stderr
is a terminal: in a detached client stderr *is* the crash log, and a stream of INFO lines would
bury the tracebacks it exists to catch.

Domain state lives in Core, scheduling state does not: the pinned warm-probe session
(dropped by `reset_warm`) and the public-IP/country cache (keyed by the address itself,
so it never needs invalidating) are Core's; grids, intervals, gap detection, and failure
counters are the monitor's.

## Monitor

`monitor.py` owns everything about the background process. It decides *when*; Core does
*what*.

- `Monitor` — the scheduler: a fixed-grid loop per probe, gap re-basing, failure policy,
  signal handling (behavior spec: [monitor.md](monitor.md)).
- `run_monitor(core)` — foreground run: acquire the single-instance lock, mirror the log
  to stderr, run the scheduler.
- `start_monitor(core)`, `stop_monitor(core)`, `monitor_status(core)` — process control
  for adapters, built on `process.py`; the lock is `<data-dir>/monitor.lock`, and
  `MonitorStatus` (lock pid + heartbeat) is the derived liveness view.

## Status

`status.py` answers the one question every client with limited room has to answer, and
owns the latency bands that decide it. `current_status(core)` returns the newest row of
each series plus a single `Health`: who is measuring (the lock), then how fresh the warm
sample is, then how fast. It sits between the monitor and the adapters because Core is
not allowed to know that a monitor process exists, and no adapter may import another.
The icon's rendering of `Health` is in [tray.md](tray.md).

## Data flow

```
monitor          ── single writer ──>  Core  ──>  sqlite
cli / tui / tray ── readers ────────>  Core  ──>  sqlite
snapshot         ── live, unrecorded ─>  Core  ──>  probes
```

Component contracts: [probes.md](probes.md) (measurement), [monitor.md](monitor.md)
(process behavior), [storage.md](storage.md) (schema).
