"""Module entry point, so `python -m netvitals` works without the console script on PATH.

This is how a background monitor is started: the parent knows its own interpreter, but not
whether the shell that will host the child has our script directory in PATH.
"""

from netvitals.cli.main import main

if __name__ == "__main__":
    main()
