# Probes

Probes are the lightweight network measurements netvitals runs continuously in the
background. This document specifies their behavior; keep it in sync with
`src/netvitals/core/probes/`.

Shared contract:

- **One round-trip per target per cycle.** No retries, no per-measurement fallbacks —
  retries would mask exactly the degradation a probe exists to expose.
- **Probes are pure measurement.** Each returns a frozen `*Sample` model; timestamps,
  storage, thresholds, and caching are the caller's job. Probe code never touches the
  database or configuration files.
- **Naming:** `measure_*` produces numbers, `detect_*` produces state.

## Latency

Two HTTP probes, **warm** and **cold**, measure internet latency against the same pool
of captive-portal detection endpoints. HTTP is used instead of ICMP ping because many
VPN tunnels do not route ICMP; HTTP works over any TCP-capable path.

Endpoint pool (`LATENCY_ENDPOINTS`):

- `https://connectivitycheck.gstatic.com/generate_204` — Google, 204 No Content
- `https://www.apple.com/library/test/success.html` — Apple, tiny HTML
- `http://detectportal.firefox.com/success.txt` — Mozilla, plain text
- `http://www.msftconnecttest.com/connecttest.txt` — Microsoft, plain text

These URLs are purpose-built for automated connectivity checks: tiny payloads, global
CDNs, extreme uptime, no rate limiting, and ISPs never block them (doing so would break
captive-portal detection on every phone and laptop). Four independent operators cover
each other's outages.

Any HTTP response counts as connectivity; status/content validation (true
captive-portal detection) is a possible future refinement.

### Warm latency — `WarmLatencyProbe`

Measures steady-state responsiveness over an **established keep-alive connection** —
the HTTP analogue of ping. No DNS, TCP, or TLS setup appears in the number, so
steady-state network degradation stands out from a low baseline.

The probe pins a single endpoint and keeps one live connection to it:

1. **Election (first cycle).** All endpoints race on the shared session; the first
   responder wins and is pinned. The election time is discarded — it includes
   connection setup and only warms the pool. The steady-state request that follows
   produces the first sample.
2. **Steady state.** Exactly one request per cycle to the pinned endpoint over the
   pooled connection. This is the sample.
3. **Failover.** If the pinned endpoint fails, the full race runs again within the same
   cycle; the winner's time is recorded as the sample (slightly inflated by setup —
   better than a gap) and the winner becomes the new pinned endpoint. The change in the
   sample's `endpoint` field marks the incident in the series.
4. **Down.** If the race also fails, the sample is down (`latency_ms = None`) and the
   session is dropped, so the next cycle starts from a fresh election with no stale
   pooled connections.

The pin is **sticky until failure** — there is no periodic "fastest endpoint"
re-election. The probe tracks degradation of the *network*, not a ranking of
endpoints: hopping to whoever is currently fastest would turn the series back into a
min-over-CDNs and mask slow deterioration. A one-endpoint series stays comparable
sample to sample.

### Cold latency — `measure_cold_latency`

Measures the full cost of establishing a new connection: DNS resolution, TCP handshake,
TLS handshake, and the first HTTP round-trip. Every cycle builds a fresh session, races
all endpoints, takes the first response, and tears the session down.

This is the complement to the warm probe. Browsers open new connections constantly; when
the setup path is broken (DNS slow, handshakes stalled, firewall state exhausted) a warm
probe keeps reporting "all good" over its pre-existing connection. Cold catches exactly
that class of incident. Its baseline sits above warm by roughly the handshake cost
(~100–300 ms on a healthy link), so the two probes use separate thresholds.

## DNS — `measure_dns`

Measures latency and reachability of the **system's own resolvers** — the ones macOS is
actually using right now — not public resolvers like `1.1.1.1`, which measure a
different path. When a VPN swaps the resolver set, the probe picks that up on the next
cycle.

**Resolver discovery** (`system_resolvers`): parse `scutil --dns` — the authoritative
source of macOS DNS configuration (`/etc/resolv.conf` is a legacy shim):

1. Take every `nameserver[N]` of `resolver #1` in the main `DNS configuration` section.
   Resolvers `#2+` there are per-domain scopes (`.local`, `ip6.arpa`, …) and are ignored.
2. Fallback: if the default resolver has no nameservers, collect nameservers from
   **interface-scoped** blocks (those without a `domain :` line) in the
   `(for scoped queries)` section. Some VPN clients publish their DNS only this way.
   Domain-scoped blocks are always ignored.
3. An empty result is itself a signal: the system currently has no DNS configuration.

**Probing:** every discovered resolver is queried **in parallel** with a single UDP
`A cloudflare.com` query (2 s timeout). Per-resolver outcome: `latency_ms`
on a completed exchange, and an error category otherwise — `timeout`, `network`
(socket error), `malformed`, or the reply's non-success rcode (`servfail`, `nxdomain`,
`refused`, `other`). On an error rcode the exchange did complete, so `latency_ms` is
still recorded alongside the error. Multi-resolver parallelism captures the
"primary dead, fallback alive" case a primary-only probe would miss.

**The canary** `cloudflare.com`: short name (small packet), DNS-native operator that
essentially cannot fail to resolve, 300 s TTL, always an A record. Because the TTL far
exceeds the probe cadence, most answers come from the resolver's cache — the metric is
**resolver-path reachability and cached-answer speed**, not full recursion health.
Cache-busting via random subdomains is deliberately avoided: hammering a domain we do
not control is abusive and produces misleading NXDOMAIN noise.

Deliberately not measured: answer correctness (no hijack detection, no DNSSEC), AAAA
records, public resolvers, domain-scoped resolvers.

## VPN — `detect_vpn`

Detects VPN state, reporting only what a user acts on: active/inactive, full vs split
tunnel, the carrying interface, and a best-effort provider name.

1. **Tunnel presence.** List `tun*`/`utun*` interfaces holding an IPv4 address (the
   address filters out the idle system `utun`s). No such interface → inactive.
2. **Tunnel mode.** Ask the kernel where traffic to a well-known public address
   (`1.1.1.1`) would leave: `route -n get` performs a pure routing-table lookup — **no
   packets are sent** — and follows the actual longest-prefix match, so it is immune to
   VPN routing conventions (plain default route, OpenVPN-style `0/1` + `128.0/1`
   halves, etc.). Egress via a tunnel → `full` (that tunnel is the reported
   `interface`); egress elsewhere while a tunnel exists → `split`; lookup failure →
   mode unknown (`None`).
3. **Provider.** Parse `scutil --nc list` for a service in `(Connected)` state. Only
   NetworkExtension-based clients appear there; otherwise the provider stays `None`.

## Public IP — `detect_public_ip` / `resolve_country`

Detects the public IPv4 address and its country — which exit point traffic uses,
especially after toggling a VPN.

**IP detection:** race 2 randomly chosen services from the pool (random choice spreads
load across providers), first valid response wins, and the winner must parse as an
IPv4 address:

- `https://api.ipify.org`
- `https://ipv4.icanhazip.com`
- `https://checkip.amazonaws.com`
- `https://ipinfo.io/ip`
- `https://v4.ident.me`

**Country resolution:** race the country services (`ipinfo.io`, `ipapi.co`), and the
answer must be exactly two uppercase ASCII letters. Both functions are stateless;
callers must cache country per IP (the services are quota-limited, and an IP's country
never changes within our retention horizon), so a lookup happens only for a genuinely
new IP.
