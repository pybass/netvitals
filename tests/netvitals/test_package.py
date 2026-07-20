"""Package-level smoke test."""

import netvitals


class TestPackage:
    """Package-level sanity checks."""

    def test_import(self) -> None:
        """The package imports cleanly."""
        assert netvitals.__doc__
