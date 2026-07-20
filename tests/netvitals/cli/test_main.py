"""CLI smoke tests."""

import pytest

from netvitals.cli.main import app


class TestCli:
    """Top-level CLI behavior through the meta launcher."""

    def test_version_flag(self, capsys):
        """--version prints the installed package version and exits 0."""
        with pytest.raises(SystemExit) as excinfo:
            app.meta(["--version"])
        assert excinfo.value.code == 0
        assert capsys.readouterr().out.strip()

    def test_command_gets_core(self, capsys, tmp_path):
        """A registered command gets a Core injected, prints, and exits 0.

        `monitor status` is the command to smoke-test this with: it is the only one that
        touches nothing outside the data directory it was given.
        """
        with pytest.raises(SystemExit) as excinfo:
            app.meta(["--data-dir", str(tmp_path), "monitor", "status"])
        assert excinfo.value.code == 0
        assert "not running" in capsys.readouterr().out
