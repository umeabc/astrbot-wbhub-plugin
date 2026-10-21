"""Regression tests for tools/probe_hub.py — the CLI probe's error paths.

Covers two distinct failure modes:

1. Hub unreachable (connection refused): the probe used to reference
   ``HubError`` without importing it, so this — the single most common
   first-run failure — crashed with ``NameError`` instead of printing the
   clean ``TRANSPORT FAILURE`` line and exiting 2. Pinned here via a real
   subprocess against the documented placeholder ``http://127.0.0.1:1`` (a
   closed loopback port; never expected to be reachable on any machine).

2. Hub reachable but ``/accounts`` broken: /accounts is non-fatal, so the
   probe must still exit 0, render the report, and emit the
   ``WARNING: /accounts unavailable`` line exactly once. Driven in-process
   through the probe's own ``main()`` against the shared StubHub with
   ``accounts_status = 500``.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pathlib
import subprocess
import sys

import pytest

from astrbot_plugin_workbuddy_usage import reporting  # noqa: F401  (probe imports it)

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
PROBE = PLUGIN_DIR / "tools" / "probe_hub.py"


def _run_probe_unreachable() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WB_HUB_PASSWORD"] = "x"  # any non-empty password; auth never completes
    # Documented placeholder endpoint: port 1 on loopback is a closed port, so
    # the connection is refused immediately (fast, and hermetic on any machine).
    return subprocess.run(
        [sys.executable, str(PROBE), "--base-url", "http://127.0.0.1:1"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=env,
        cwd=str(PLUGIN_DIR),
    )


class TestProbeUnreachableHub:
    def test_unreachable_hub_exits_2_without_traceback(self):
        """A connection-refused Hub must yield a clean TRANSPORT FAILURE and
        exit 2 — never a NameError from the (previously unimported) HubError.
        Because the analytics fetch is fatal, the probe aborts BEFORE the
        non-fatal /accounts section: it must not claim an /accounts problem."""
        proc = _run_probe_unreachable()
        assert proc.returncode == 2, (
            f"expected exit 2, got {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
        assert "TRANSPORT FAILURE" in proc.stderr
        assert "/accounts unavailable" not in proc.stderr
        assert "Traceback" not in proc.stderr
        assert "NameError" not in proc.stderr


class TestProbeAccountsFailureNonFatal:
    async def test_accounts_failure_still_renders_report(self, stub_hub, session, monkeypatch, capsys):
        """Hub up, /accounts returning 500: probe must exit 0, render the
        report, and warn about /accounts exactly once."""
        stub_hub.accounts_status = 500

        import importlib.util
        spec = importlib.util.spec_from_file_location("probe_hub_module", PROBE)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)  # sets probe.main / EXIT_* constants

        monkeypatch.setattr(
            sys, "argv",
            ["probe_hub.py", "--base-url", stub_hub.base_url],
        )
        exit_code = await probe.main()
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.err.count("WARNING: /accounts unavailable") == 1
        # The report itself still renders.
        assert "WorkBuddy 当日用量" in captured.out
        assert "/accounts: unavailable (no status info)" in captured.out
