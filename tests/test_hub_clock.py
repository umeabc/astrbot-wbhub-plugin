"""Hub-clock authority tests: Date-header parsing, skew warning, and the
day-window regression that pins the v1.0.2 under-count bug.

The operator's AstrBot container ran with local time = UTC while the reporting
timezone was Asia/Shanghai: a naive datetime.now() stamped with the reporting
timezone truncated `until` by exactly 8 hours, shrinking the queried window
and under-counting tokens ~650x. These tests pin the fix.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_workbuddy_usage.client import (
    WorkBuddyClient,
    parse_http_date_header,
)
from astrbot_plugin_workbuddy_usage.main import CLOCK_SKEW_WARN_SECONDS
from astrbot_plugin_workbuddy_usage.reporting import day_bounds, day_bounds_from_hub

from conftest import make_analytics, make_stat

TZ = "Asia/Shanghai"
# NOTE: module contains sync tests; asyncio_mode=auto handles both.


# ---------------------------------------------------------------------------
# HTTP Date header parsing
# ---------------------------------------------------------------------------


class TestParseHttpDateHeader:
    def test_parses_to_aware_utc(self):
        parsed = parse_http_date_header("Wed, 15 Oct 2026 06:13:17 GMT")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
        assert parsed == datetime(2026, 10, 15, 6, 13, 17, tzinfo=dt_timezone.utc)

    def test_missing_header_returns_none(self):
        assert parse_http_date_header(None) is None
        assert parse_http_date_header("") is None
        assert parse_http_date_header("   ") is None

    def test_unparseable_header_returns_none(self):
        assert parse_http_date_header("not a date") is None
        assert parse_http_date_header("12345") is None


class TestHubNowCapture:
    async def test_login_response_captures_hub_date(self, stub_hub, session):
        client = WorkBuddyClient(
            base_url=stub_hub.base_url,
            panel_password=stub_hub.password,
            panel_token="",
            timeout_seconds=5.0,
            max_response_bytes=5 * 1024 * 1024,
            session=session,
        )
        assert client.hub_now() is None
        await client.login()
        # aiohttp's test server sends a Date header; it must be captured.
        hub_now = client.hub_now()
        assert hub_now is not None
        assert hub_now.tzinfo is dt_timezone.utc

    async def test_analytics_response_captures_hub_date(self, stub_hub, session):
        client = WorkBuddyClient(
            base_url=stub_hub.base_url,
            panel_password=stub_hub.password,
            panel_token="",
            timeout_seconds=5.0,
            max_response_bytes=5 * 1024 * 1024,
            session=session,
        )
        await client.fetch_analytics(1.0, 2.0)
        assert client.hub_now() is not None


# ---------------------------------------------------------------------------
# day_bounds_from_hub: the calendar-day window derived from the Hub instant
# ---------------------------------------------------------------------------


class TestDayBoundsFromHub:
    def test_hub_time_produces_exact_calendar_day_bounds(self):
        # Hub says 2026-10-15T06:13:17Z, which is 14:13:17+08:00 in Shanghai.
        hub_now = datetime(2026, 10, 15, 6, 13, 17, tzinfo=dt_timezone.utc)
        since, until = day_bounds_from_hub(hub_now, TZ)
        assert until == pytest.approx(hub_now.timestamp())
        midnight = datetime(2026, 10, 15, 0, 0, 0, tzinfo=ZoneInfo(TZ))
        assert since == midnight.timestamp() == 1791993600
        assert until == 1792044797

    def test_naive_hub_time_rejected(self):
        with pytest.raises(ValueError):
            day_bounds_from_hub(datetime(2026, 10, 15, 6, 13, 17), TZ)

    def test_day_bounds_with_aware_now_used_verbatim(self):
        # Pure helper behaviour with an aware value: used as-is.
        aware = datetime(2024, 3, 15, 10, 30, tzinfo=ZoneInfo(TZ))
        since, until = day_bounds(aware, TZ)
        assert until == aware.timestamp()
        assert since == datetime(2024, 3, 15, tzinfo=ZoneInfo(TZ)).timestamp()

    def test_day_bounds_with_naive_now_stamps_timezone(self):
        # Documented pure-helper behaviour: a naive value is stamped with the
        # target timezone. (The bug was CALLERS passing a naive value derived
        # from a differently-based process clock; callers no longer do.)
        naive = datetime(2024, 3, 15, 10, 30)
        since, _ = day_bounds(naive, TZ)
        assert since == datetime(2024, 3, 15, tzinfo=ZoneInfo(TZ)).timestamp()


class TestUtcContainerRegression:
    """The regression that pins the v1.0.2 bug.

    A container whose local time is UTC makes naive datetime.now() return
    06:13 when the true instant is 14:13+08:00. Stamping the reporting
    timezone onto that naive value truncated `until` by exactly 8 hours.
    """

    def test_window_from_hub_instant_is_not_8h_short(self):
        true_utc = datetime(2026, 10, 15, 6, 13, 17, tzinfo=dt_timezone.utc)
        # What datetime.now() returned inside the UTC container (naive):
        naive_container_now = true_utc.replace(tzinfo=None)

        # The buggy caller path (naive stamped with reporting tz) — for
        # documentation only, computed via the pure helper:
        _, buggy_until = day_bounds(naive_container_now, TZ)
        assert buggy_until == 1792015997  # 8 h short of the true instant

        # The fixed path: the window comes from the Hub's aware instant.
        since, until = day_bounds_from_hub(true_utc, TZ)
        assert until == true_utc.timestamp() == 1792044797
        assert since == 1791993600
        assert until != 1792015997  # explicitly: NOT 8 h short

    def test_hub_clock_disagreeing_with_local_by_8h_still_correct(self):
        # Local (buggy) clock believes it is 2026-10-15 06:13 UTC, but the Hub
        # says 2026-10-14T22:13:17Z — the same wall-clock mismatch in reverse:
        # whichever way they disagree, the window must end at the Hub instant.
        hub_utc = datetime(2026, 10, 14, 22, 13, 17, tzinfo=dt_timezone.utc)
        since, until = day_bounds_from_hub(hub_utc, TZ)
        local_shanghai = datetime(2026, 10, 15, 14, 13, 17, tzinfo=ZoneInfo(TZ))
        assert until == hub_utc.timestamp()
        assert until < local_shanghai.timestamp()  # different calendar window
        assert since == datetime(2026, 10, 15, tzinfo=ZoneInfo(TZ)).timestamp()

    def test_hub_time_early_in_local_day_yields_short_window(self):
        # Hub time 00:05+08:00 (16:05 UTC on the previous UTC day): the window
        # must be ~5 minutes, not a full day.
        hub_utc = datetime(2026, 10, 15, 16, 5, 0, tzinfo=dt_timezone.utc)
        since, until = day_bounds_from_hub(hub_utc, TZ)
        minutes = (until - since) / 60
        assert minutes == pytest.approx(5, rel=0.01)
        # The naive misinterpretation would have stamped 16:05 onto the
        # reporting timezone and produced a ~16-hour window for the wrong day.
        naive_since, naive_until = day_bounds(hub_utc.replace(tzinfo=None), TZ)
        assert (naive_until - naive_since) / 60 == pytest.approx(965, rel=0.01)


# ---------------------------------------------------------------------------
# Clock-skew warning
# ---------------------------------------------------------------------------


def _client_for_skew() -> WorkBuddyClient:
    return WorkBuddyClient(
        base_url="http://127.0.0.1:1",
        panel_password="x",
        panel_token="",
        timeout_seconds=1.0,
        max_response_bytes=65536,
        session=None,  # never used by _warn_clock_skew under test
    )


class TestClockSkewWarning:
    def test_skew_above_threshold_warns(self, caplog):
        from astrbot_plugin_workbuddy_usage.main import WorkBuddyUsagePlugin

        plugin = WorkBuddyUsagePlugin.__new__(WorkBuddyUsagePlugin)
        hub_now = datetime.now(dt_timezone.utc).replace(
            year=datetime.now(dt_timezone.utc).year - 1
        )
        with caplog.at_level(logging.WARNING, logger="astrbot.workbuddy_usage"):
            plugin._warn_clock_skew(hub_now)
        assert any("clock skew" in rec.message for rec in caplog.records)
        assert any("hub=" in rec.message and "local=" in rec.message for rec in caplog.records)

    def test_no_skew_no_warning(self, caplog):
        from astrbot_plugin_workbuddy_usage.main import WorkBuddyUsagePlugin

        plugin = WorkBuddyUsagePlugin.__new__(WorkBuddyUsagePlugin)
        hub_now = datetime.now(dt_timezone.utc)
        with caplog.at_level(logging.WARNING, logger="astrbot.workbuddy_usage"):
            plugin._warn_clock_skew(hub_now)
        assert not any("clock skew" in rec.message for rec in caplog.records)

    def test_threshold_is_120_seconds(self):
        assert CLOCK_SKEW_WARN_SECONDS == 120
