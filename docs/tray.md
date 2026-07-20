# Tray

The menu bar client. It measures nothing: it reads what the monitor recorded and answers one
question — *is the connection ok right now* — in a single character. The numbers behind that
answer live in the dropdown.

## The icon

Two characters of country code, then one health icon. The label keeps its two characters even when
the country is unknown, so the item holds one width and the icons to the left of us never shift.

| State | Icon | SF Symbol | Meaning |
|---|---|---|---|
| ok | ● filled | `circle.fill` | fresh sample below the ok band |
| slow | ◐ half | `circle.lefthalf.filled` | fresh sample above the ok band |
| bad | ○ hollow | `circle` | fresh sample above the slow band |
| down | ✕ | `xmark` | measured, and every endpoint failed |
| stale | ◌ dotted | `circle.dotted` | the monitor holds its lock, but its samples stopped arriving |
| monitor off | ⊘ slashed | `circle.slash` | nothing is measuring; the stored values are from the last run |
| no data | dot in a ring | `smallcircle.filled.circle` | the monitor runs, but has not recorded a warm sample yet |

**Shape carries everything; there is no color.** The menu bar sits on whatever wallpaper the user
has, where a tint is either invisible or shouting. The healthy ladder empties out as things get
worse — filled, half, hollow — so the three order themselves at a glance without a legend.
Anything that is *not* a measurement leaves that ladder: a cross for a network that answered
nothing, a slashed circle for a ladder switched off, a dotted one for a monitor that is up but
silent. The last two are worth separate icons precisely because one shared "stale" symbol would
hide the difference between a connection that is down and a monitor that is.

**SF Symbols, not text glyphs.** The neighbours in the menu bar are drawn icons; a character set at
font size sits visibly smaller than every one of them. Symbols are template images at exactly the
bar's icon metrics, so ours is the same size as everyone else's, follows light and dark, and
inverts under the highlight when the menu opens.

The country code shows only while a fresh sample backs it (`ok`/`slow`/`bad`), and is blank
otherwise. An exit country nobody is currently confirming is a guess, and a guess sitting next to a
health glyph reads as a fact.

The glyph is driven by the **warm latency series alone**. DNS and VPN failures stay visible in the
dropdown, but folding them into the single verdict would make it flicker on one 10 s DNS timeout —
and the failure that actually matters, a tunnel dropping, shows up in the warm series anyway.

Where the verdict itself is computed: [`status.py`](../src/netvitals/status.py), shared so that the
icon, the dashboard, and a future `status` command cannot disagree about what "slow" means.

## The menu

```
Monitor is not running - the values below are from its last run   <- only when ⊘ or ◌
Latency warm: 238 ms
Latency cold: 402 ms
DNS: 21 ms (192.168.1.1)
VPN: full tunnel (Happ Plus)
IP: 212.6.44.17 (LV)
--------
Start monitor            <- exactly one of the two is visible
Stop monitor
--------
Quit tray
```

The warning row appears only when the icon is not reporting a measurement. Without it, last-known
values look current, and the icon reads as broken rather than honest.

Monitor control is explicit, which is also what makes `Quit tray` unambiguous: it closes the icon
and nothing else. Both control clicks wait for the monitor to take or release its lock, so the menu
freezes for that moment and then reopens showing what actually happened.

The refresh runs every 2 s — the warm cadence, the fastest series there is — and keeps running
while the menu is open, so an open dropdown stays live instead of freezing exactly while it is
being read.

## Process

```
netvitals tray start    # detached; prints the pid
netvitals tray stop     # SIGTERM, confirmed by the lock being released
netvitals tray run      # foreground, for debugging
```

One icon per data dir, enforced by an `flock` on `<data-dir>/tray.lock` — the same machinery the
monitor uses ([`process.py`](../src/netvitals/process.py)). Starting the tray does not start the
monitor, and quitting the tray does not stop it: they are independent processes that happen to
share a database.
