"""Small domain-free helpers shared across the application layer."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable

log = logging.getLogger(__name__)

SCUTIL = "/usr/sbin/scutil"
"""Absolute path to macOS scutil (System Configuration utility)."""


async def race[T](coros: Iterable[Coroutine[Any, Any, T | None]]) -> T | None:
    """Run *coros* concurrently, return the first non-None result, cancel the rest.

    Returns None when every coroutine produced None (or *coros* is empty).
    """
    pending: set[asyncio.Task[T | None]] = {asyncio.create_task(coro) for coro in coros}
    if not pending:
        return None
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                if result is not None:
                    return result
        return None
    finally:
        for task in pending:
            task.cancel()
        # Await the cancellations so no request outlives the race — the caller may close
        # the HTTP session immediately after this returns.
        await asyncio.gather(*pending, return_exceptions=True)


async def run_command(*argv: str, timeout: float) -> str | None:
    """Run a fixed command and return its stdout text; None on start failure, timeout, or non-zero exit."""
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError as exc:
        log.warning("proc: cannot start %s: %s", argv[0], exc)
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("proc: %s timed out after %.1f s", argv[0], timeout)
        return None
    if proc.returncode != 0:
        log.warning("proc: %s exited with code %s", argv[0], proc.returncode)
        return None
    return stdout.decode()
