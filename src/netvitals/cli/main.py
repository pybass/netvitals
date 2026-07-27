"""CLI client: run and control netvitals from the shell."""

import sys
import time
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
    core = Core(data_dir if data_dir is not None else Core.DEFAULT_DATA_DIR, debug=debug)
    try:
        command(*bound.args, **bound.kwargs, core=core)
    finally:
        core.close()


def main() -> None:
    """Console-script entry point: map AppError to a clean one-line exit, stamp anything else as a crash."""
    # Nothing here is portable: the probes shell out to scutil/route, the tray is AppKit.
    if sys.platform != "darwin":
        sys.exit("error: netvitals runs on macOS only")
    try:
        app.meta()
    except AppError as e:
        sys.exit(f"error: {e}")
    except Exception:
        # This entry point is also the detached clients', and their stderr is the crash log: without
        # a timestamp a traceback in there cannot be dated, and one that crashed before logging was
        # wired has nowhere else to land at all. On a terminal it is one extra line above the same
        # traceback the user would have seen anyway.
        sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] netvitals crashed:\n")
        raise
