# Non-goals

Deliberate boundaries, not missing features. Each is a decision already made; the document that
argues it in full is linked where one exists. Things that are simply *not built yet* — a config
file, launchd integration, derived incidents — live at the end of [monitor.md](monitor.md) instead.

- **Environment-variable configuration.** `--data-dir` is the only override, and the default
  `~/.local/share/netvitals` is a fixed path, not `$XDG_DATA_HOME`. An env var is invisible state: a
  variable exported for some other tool would silently move the database, and a client started by
  launchd would resolve it in a different environment than the shell that installed us — which
  matters precisely because a spawned client is *given* no `--data-dir` when it is the default one.

- **Color in the menu bar icon.** Shape carries the whole verdict. The bar sits on whatever
  wallpaper the user has, where a tint is either invisible or shouting ([tray.md](tray.md)).

- **A verdict that folds in DNS and VPN.** The single health glyph is driven by the warm latency
  series alone. Folding the rest in would make it flicker on one 10 s DNS timeout, and the failure
  that actually matters — a tunnel dropping — shows up in the warm series anyway. DNS and VPN stay
  visible in their own rows ([tray.md](tray.md)).

- **Retries inside a probe.** One round-trip per target per cycle, no per-measurement fallbacks: a
  retry would mask exactly the degradation the probe exists to expose. A failed measurement is a
  recorded sample, never an exception ([probes.md](probes.md)).

- **Re-electing the warm endpoint for speed.** The pinned endpoint is sticky until it fails. Hopping
  to whoever is currently fastest would turn the series into a min-over-CDNs and hide slow
  deterioration; a one-endpoint series stays comparable sample to sample ([probes.md](probes.md)).

- **ICMP ping.** Latency is measured over HTTP, because many VPN tunnels do not route ICMP while
  HTTP works over any TCP-capable path ([probes.md](probes.md)).

- **DNS answer correctness.** No hijack detection, no DNSSEC, no AAAA, no public resolvers, no
  domain-scoped resolvers — netvitals measures the reachability and speed of the resolvers the
  system is actually using. The canary is also never cache-busted: hammering a domain we do not
  control is abusive and produces misleading NXDOMAIN noise ([probes.md](probes.md)).

- **Catch-up samples after a gap.** Missed cycles are dropped, never fired back to back. A burst of
  samples all timestamped *now* would misreport when they were taken, and a lie about history is
  worse than a hole in it — consumers see the hole in the row spacing ([monitor.md](monitor.md)).

- **SIGKILL as a fallback when stopping.** `monitor stop` and `tray stop` send SIGTERM and wait; if
  the lock is still held after the grace period they report an error and leave the process alone. A
  client that ignores SIGTERM for that long is a bug worth seeing, and killing it would destroy the
  evidence.

- **A second writer.** Only the monitor records. Every other client reads what it wrote, so they all
  show the same numbers, taken once, instead of four clients probing the network on their own
  schedules. `snapshot` is the one exception that proves the rule: it measures live and records
  nothing ([monitor.md](monitor.md)).

- **Self-daemonizing.** `monitor run` is a plain foreground process that stops on SIGTERM;
  `monitor start` is a convenience spawn on top of it. Anything that wants to supervise the monitor
  can do so without fighting it ([monitor.md](monitor.md)).

- **Platforms other than macOS.** The probes shell out to `scutil` and `route`, and the tray is
  AppKit. The CLI refuses to start elsewhere rather than half-working.

- **Older Python versions.** netvitals targets the latest stable Python. Users can select that
  interpreter when installing with `uv tool install`.
