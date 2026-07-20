"""CLI client: run and control netvitals from the shell."""

import os
import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from netvitals.cli.commands import dashboard, monitor, snapshot, tray
from netvitals.core.core import Core
from netvitals.core.errors import AppError

app = App(name="netvitals", help="Internet connection monitor.")
app["--help"].show = False  # the flags keep working; they just have no business in the command list
app["--version"].show = False
app.default(dashboard.run)
app.command(snapshot.run, name="snapshot", alias="s", sort_key=0)  # the one command typed all day, hence the alias
app.command(monitor.app)  # its sort_key lives on the sub-app: cyclopts rejects extra kwargs when registering an App
app.command(tray.app)


@app.meta.default
def launcher(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    data_dir: Path | None = None,
    debug: bool = False,
) -> None:
    """Run the selected command, opening (and closing) the Core only when it asks for one.

    Parameters
    ----------
    tokens
        Raw CLI tokens, forwarded to the selected command.
    data_dir
        Data directory (default: ~/.local/share/netvitals).
    debug
        Log at DEBUG level instead of INFO.

    """
    command, bound, ignored = app.parse_args(tokens)
    if "core" not in ignored:  # --help and --version run without a Core
        command(*bound.args, **bound.kwargs)
        return
    if data_dir is None:  # default: the XDG data dir (deliberate cross-tool convention; usually ~/.local/share)
        data_dir = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "netvitals"
    core = Core(data_dir, debug=debug)
    try:
        command(*bound.args, **bound.kwargs, core=core)
    finally:
        core.close()


def main() -> None:
    """Console-script entry point: map AppError to a clean one-line exit."""
    # Nothing here is portable: the probes shell out to scutil/route, the tray is AppKit.
    if sys.platform != "darwin":
        sys.exit("error: netvitals runs on macOS only")
    try:
        app.meta()
    except AppError as e:
        sys.exit(f"error: {e}")
