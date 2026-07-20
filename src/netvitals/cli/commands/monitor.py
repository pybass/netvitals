"""The monitor command group: run the measuring process, control it, inspect it."""

import asyncio
import time
from datetime import UTC, datetime

from cyclopts import App

from netvitals import monitor
from netvitals.cli import utils

app = App(name="monitor", help="The process that measures continuously.", sort_key=1)  # after snapshot


@app.command(name="run", sort_key=3)
def run(*, core: utils.InjectedCore) -> None:
    """Measure continuously in the foreground until interrupted, recording everything."""
    asyncio.run(monitor.run_monitor(core))


@app.command(name="start", sort_key=0)
def start(*, core: utils.InjectedCore) -> None:
    """Start the monitor in the background."""
    utils.console.print(f"monitor: started (pid {monitor.start_monitor(core)})")


@app.command(name="stop", sort_key=1)
def stop(*, core: utils.InjectedCore) -> None:
    """Stop the background monitor."""
    pid = monitor.stop_monitor(core)
    if pid is None:
        utils.console.print("monitor: not running")
    else:
        utils.console.print(f"monitor: stopped (pid {pid})")


@app.command(name="status", sort_key=2)
def status(*, core: utils.InjectedCore) -> None:
    """Report whether the monitor is running and when it last recorded anything."""
    state = monitor.monitor_status(core)
    if state.pid is None:
        utils.console.print("monitor: [red]not running[/]")
    elif state.started_at is None:
        utils.console.print(f"monitor: running (pid {state.pid})")
    else:
        started = datetime.fromtimestamp(state.started_at, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        utils.console.print(f"monitor: running (pid {state.pid}), started {started}")
    if state.heartbeat_at is None:
        utils.console.print("heartbeat: never")
    else:
        utils.console.print(f"heartbeat: {time.time() - state.heartbeat_at:.1f} s ago")
    utils.console.print(f"data dir: {core.data_dir}")
