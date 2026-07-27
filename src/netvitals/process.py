"""Background clients: the single-instance lock, the detached spawn, and the stop handshake.

Two netvitals processes run in the background — the monitor and the tray — and both are found,
started, and stopped identically. Liveness is a lock, not a pid file: an advisory lock cannot go
stale, because the kernel drops it when the holder dies, however it dies. So there is no "is this
pid file leftover?" guesswork, and two instances of the same client can never both believe they
are the live one.
"""

import fcntl
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from netvitals.core.core import Core
from netvitals.core.errors import AppError

_START_TIMEOUT = 5.0
"""Seconds to wait for a spawned client to take its lock — interpreter startup is most of it."""


def acquire_lock(path: Path) -> int | None:
    """Take the exclusive lock on *path* and write our pid into it; None when another process holds it.

    The returned file descriptor is what holds the lock: closing it — or dying — releases it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.truncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def lock_holder(path: Path) -> int | None:
    """Return the pid holding the lock on *path*, or None when nobody holds it.

    The pid is only read when the lock is actually held, so the file's contents can never be
    mistaken for a live process after a crash.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            held = True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            held = False
    finally:
        os.close(fd)
    if not held:
        return None
    pid = path.read_text().strip()
    return int(pid) if pid.isdigit() else None


def start_detached(core: Core, args: Sequence[str], lock_path: Path, *, what: str) -> int:
    """Spawn `netvitals *args*` detached, wait until it holds *lock_path*, and return its pid.

    Raises AppError when one is already running, or when the spawned one dies before taking the
    lock (its story is in the log file).
    """
    if (running := lock_holder(lock_path)) is not None:
        raise AppError(f"{what}: already running (pid {running})")
    # `-m netvitals` rather than the console script: this interpreter certainly has us installed,
    # while whether the child's PATH would find the script is unknowable.
    argv = [sys.executable, "-m", "netvitals"]
    # The default is passed by omission, so the common case stays readable in `ps`; the child
    # resolves the same fixed default itself.
    if core.data_dir != Core.DEFAULT_DATA_DIR:
        argv += ["--data-dir", str(core.data_dir)]
    if core.debug:
        argv.append("--debug")
    argv += args
    # A new session detaches the child from the terminal's process group, so it survives the shell
    # that started it; the launching CLI exits immediately after, and the child is reparented to the
    # init process. It must not write to a terminal it no longer owns, so stdin is /dev/null and its
    # output goes to the crash log: everything it has to say routinely goes to the log file, and
    # what lands here is what never reached the logger — including a crash before it was wired.
    # S603: argv is ours — this interpreter, our module name, the resolved data dir. Nothing
    # external can reach it, and shell=False is exactly the behavior we want.
    with core.crash_log.open("ab") as crash_log:
        subprocess.Popen(  # noqa: S603
            argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=crash_log, stderr=crash_log
        )
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if (pid := lock_holder(lock_path)) is not None:
            return pid
        time.sleep(0.1)
    raise AppError(f"{what}: did not come up within {_START_TIMEOUT:.0f} s")


def stop_detached(lock_path: Path, *, what: str, timeout: float) -> int | None:
    """Stop the client holding *lock_path* and return its pid; None when none was running.

    Waiting for the lock rather than for the pid to vanish is what makes this correct: the kernel
    releases the lock exactly when the process dies, and an unrelated process that happened to
    reuse the pid cannot fool us into reporting success.

    Raises AppError when the lock is still held after *timeout* — deliberately without SIGKILL: a
    client that ignores SIGTERM for that long is a bug worth seeing, and killing it would hide the
    evidence.
    """
    pid = lock_holder(lock_path)
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:  # died between the lock probe and the signal
        return pid
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock_holder(lock_path) is None:
            return pid
        time.sleep(0.1)
    raise AppError(f"{what}: still running after {timeout:.0f} s (pid {pid})")
