# The monitor

The monitor is the process that measures. It runs every probe on its own cadence and records what
it measures; the dashboard, tray, and CLI only read what it wrote. One writer means every client
shows the same numbers, taken once — not four clients probing the network on their own schedules.

It is deliberately not called a daemon: "daemon" describes how a process is started, not what it
does, and `monitor run` runs in the foreground. See `src/netvitals/monitor.py`.

## Running it

| command | what it does |
|---|---|
| `netvitals monitor run` | run in the foreground; logs to the terminal as well as the log file. Ctrl-C stops it. This is what a supervisor should run. |
| `netvitals monitor start` | spawn a background monitor (`python -m netvitals … monitor run`) detached from the terminal, with its output to `<data-dir>/crash.log`. Waits until the new monitor holds the lock, so a start that silently dies is reported as an error, not as success. |
| `netvitals monitor stop` | SIGTERM the running monitor and wait for it to exit. |
| `netvitals monitor status` | whether a monitor is running, and how old its last heartbeat is. |

The monitor never daemonizes itself: it is a plain foreground process that stops on SIGTERM, so
launchd (or anything else) can supervise it without fighting it.

**Single instance.** The monitor holds an exclusive `flock` on `<data-dir>/monitor.lock` for its
whole life and writes its pid into that file. Two monitors writing the same database is the one
thing that must never happen, and a lock — unlike a pid file — cannot go stale: the kernel drops it
when the holder dies, however it dies. So `monitor status` after `kill -9` reports "not running" with
nothing to clean up, and `monitor stop` waits for the *lock* to be released rather than for the pid to
disappear, which no recycled pid can fake.

## Cadence

| loop | interval | why |
|---|---|---|
| warm latency | 2 s | the "is it up right now" signal, and the one worth a dense series |
| cold latency | 10 s | a full connection setup is too expensive to pay every cycle |
| DNS | 10 s | one cycle queries every system resolver in parallel |
| VPN | 10 s | a local routing lookup; the cost is negligible |
| public IP | 60 s | the services are quota-limited and the address rarely changes |
| purge | 1 h | retention housekeeping, also run once at startup |

Every loop also runs immediately at startup, so a fresh monitor produces data at once instead of
after its first interval. The heartbeat rides the warm loop — the fastest one — so readers get a
liveness signal every 2 s without a loop of its own.

A VPN state change triggers an immediate public-IP check from within the VPN loop: the exit
address almost certainly moved, and waiting up to a minute to notice would be visible to the
user. The IP loop's own grid may repeat the check seconds later — one redundant lookup per
VPN change, deduplicated in storage.

## Scheduling

Wake times come from a fixed grid (`next_at += interval`), not from sleeping *after* the work is
done. Sleeping after the work stretches the real period by however long the measurement took, so
the series ends up unevenly spaced and no longer comparable sample to sample.

Missed cycles are dropped, never fired back to back. A burst of catch-up samples would all be
timestamped now, which would misreport when they were taken — a lie about history is worse than a
hole in it. Consumers see the hole in the row spacing.

A gap of more than 60 s (a machine sleep, or a stalled loop) is logged once and re-bases the grid;
the warm loop additionally drops its keep-alive session, since the pooled connection cannot have
survived and a fresh election gives a true steady-state number instead of one inflated by setup.
Smaller overruns — work that simply takes longer than its cadence, as during an outage when every
request runs to its timeout — pass silently: the wider row spacing already says it, and the monitor
could not do better anyway.

## Failure policy

Probes report a failed measurement as a sample (`latency_ms = None`, a DNS error category), never
as an exception. So an exception reaching a loop means something else broke — storage, or us.

Such a loop retries twice; on the third consecutive failure it stops the whole monitor with the
traceback logged as CRITICAL. A process that stays alive while recording nothing is worse than one
that dies loudly: the log has an explanation, `status` shows a stale heartbeat, and a supervisor can
restart it. The counter resets after any successful cycle, so isolated hiccups are tolerated.

The first SIGTERM/SIGINT asks the loops to finish, which is not instant — a probe can be mid-request
for its whole timeout. A second signal restores the default handler and re-raises, killing the
process immediately.

## Not here yet

No configuration file: intervals are constants in `monitor.py` and the retention horizon lives in
`Core.purge` (constructor keywords would be the first step when they need to become configurable).
No launchd integration, no derived incidents ("when was the internet down"), and no sample-level
status API for the tray beyond the liveness view in `monitor_status()` — the monitor is the right
place to derive those, and they come next.
