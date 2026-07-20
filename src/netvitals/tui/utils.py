"""Formatting helpers shared by the TUI modules; the latency bands themselves live in `status`."""

from datetime import UTC, datetime


def latency_style(ms: float, ok_ms: int, slow_ms: int) -> str:
    """Map a latency value onto the green/yellow/red bands."""
    if ms < ok_ms:
        return "green"
    if ms < slow_ms:
        return "yellow"
    return "red"


def local_time(ts: float, fmt: str) -> str:
    """Format a UTC Unix timestamp as local wall-clock time."""
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime(fmt)
