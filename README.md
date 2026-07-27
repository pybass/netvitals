# netvitals

Internet connection monitor for macOS: warm/cold latency, DNS, VPN state, public IP.

> **Status:** Early development.

## Install

```sh
uv tool install netvitals
```

macOS only. Requires Python 3.14+.

## Usage

```sh
netvitals monitor start   # measure continuously in the background
netvitals                 # dashboard
netvitals tray start      # menu bar icon
netvitals snapshot        # measure once and print the result
```

The monitor does not come back after a reboot yet — start it again.

## Design

- [architecture](https://github.com/pybass/netvitals/blob/main/docs/architecture.md) — layers, dependency rules, the Core API, data flow.
- [probes](https://github.com/pybass/netvitals/blob/main/docs/probes.md) — what is measured and how.
- [monitor](https://github.com/pybass/netvitals/blob/main/docs/monitor.md) — cadence, scheduling, failure policy.
- [tray](https://github.com/pybass/netvitals/blob/main/docs/tray.md) — what each glyph means and why.
- [storage](https://github.com/pybass/netvitals/blob/main/docs/storage.md) — schema, deduplication, retention.
- [non-goals](https://github.com/pybass/netvitals/blob/main/docs/non-goals.md) — what netvitals deliberately will not do.

## License

[MIT](https://github.com/pybass/netvitals/blob/main/LICENSE)
