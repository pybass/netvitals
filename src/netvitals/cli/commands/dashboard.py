"""The default command: live TUI dashboard."""

from netvitals.cli import utils
from netvitals.tui.app import TuiApp


def run(*, core: utils.InjectedCore) -> None:
    """Open the live dashboard."""
    TuiApp(core).run()
