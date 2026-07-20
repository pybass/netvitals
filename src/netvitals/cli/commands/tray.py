"""The tray command group: the menu bar icon and its process control."""

from cyclopts import App

from netvitals.cli import utils
from netvitals.tray import controller

app = App(name="tray", help="Menu bar icon (macOS).", sort_key=2)  # after monitor


@app.command(name="start", sort_key=0)
def start(*, core: utils.InjectedCore) -> None:
    """Start the menu bar icon in the background."""
    utils.console.print(f"tray: started (pid {controller.start_tray(core)})")


@app.command(name="stop", sort_key=1)
def stop(*, core: utils.InjectedCore) -> None:
    """Stop the menu bar icon."""
    pid = controller.stop_tray(core)
    if pid is None:
        utils.console.print("tray: not running")
    else:
        utils.console.print(f"tray: stopped (pid {pid})")


@app.command(name="run", sort_key=2)
def run(*, core: utils.InjectedCore) -> None:
    """Show the menu bar icon in the foreground until interrupted."""
    controller.run_tray(core)
