"""Shared CLI helpers: the injected-Core annotation and output console."""

from typing import Annotated

from cyclopts import Parameter
from rich.console import Console

from netvitals.core.core import Core

type InjectedCore = Annotated[Core, Parameter(parse=False)]  # a Core injected by the launcher, invisible to CLI parsing

# The one console every command prints through (rich's documented module-singleton pattern).
# highlight=False stops rich from auto-coloring numbers; soft_wrap=True stops it from
# hard-wrapping long lines in pipes — with both off, console.print behaves exactly like
# print() until a style is asked for explicitly.
console = Console(highlight=False, soft_wrap=True)
