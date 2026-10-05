"""The tray command group: the menu bar icon and its process control."""

from typing import Annotated

from cyclopts import App, Parameter

from netvitals import process
from netvitals.cli import utils
from netvitals.tray import controller

app = App(name="tray", help="Menu bar icon (macOS); starts it when given no command.", sort_key=2)  # after monitor


@app.default
@app.command(name="start", sort_key=0)
def start(
    *,
    foreground: Annotated[bool, Parameter(help="Show the icon from this terminal until interrupted.")] = False,
    core: utils.InjectedCore,
) -> None:
    """Start the menu bar icon in the background."""
    if foreground:
        controller.run_tray(core)
    else:
        utils.console.print(f"tray: started (pid {controller.start_tray(core)})")


@app.command(name="stop", sort_key=1)
def stop(*, core: utils.InjectedCore) -> None:
    """Stop the menu bar icon."""
    pid = controller.stop_tray(core)
    if pid is None:
        utils.console.print("tray: not running")
    else:
        utils.console.print(f"tray: stopped (pid {pid})")


@app.command(name="status", sort_key=2)
def status(*, core: utils.InjectedCore) -> None:
    """Report whether the menu bar icon is running."""
    pid = process.lock_holder(core.tray_lock)
    if pid is None:
        utils.console.print("tray: [red]not running[/]")
    else:
        utils.console.print(f"tray: running (pid {pid})")
