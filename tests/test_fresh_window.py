"""v1.3.2 — the report window must be built from a FRESH Hub clock.

Regression: ``_get_report`` read ``client.hub_now()`` before fetching anything.
That value is left behind by the previous request (the last /wb, or login), so
the window could end 10 minutes — or a whole day — in the past. /wb then
re-reported the earlier numbers ("有时候显示之前的用量").

The window is now built from the Date header of the response that just
arrived (/accounts, fetched first); a capture older than
HUB_CLOCK_FRESH_SECONDS is rejected, and when /accounts is unavailable the
local-clock window is corrected once against the analytics Date.

All offline (aiohttp stub server).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import format_datetime
from zoneinfo import ZoneInfo

from astrbot_plugin_workbuddy_usage.client import WorkBuddyClient
from astrbot_plugin_workbuddy_usage.main import HUB_CLOCK_FRESH_SECONDS

TZ = "Asia/Shanghai"
TZ_OBJ = ZoneInfo(TZ)


def midnight_of(instant: datetime) -> datetime:
    return instant.astimezone(TZ_OBJ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def http_date(instant: datetime) -> str:
    return format_datetime(instant.astimezone(dt_timezone.utc), usegmt=True)


class FakeEvent:
    def __init__(self, sender: str = "10001") -> None:
        self._sender = sender
        self.stopped = False

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_sender_id(self) -> str:
        return self._sender

    def get_group_id(self) -> str | None:
        return None

    def is_admin(self) -> bool:
        return True

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str):
        from astrbot.api.event import MessageEventResult

        return MessageEventResult([type("Plain", (), {"text": text})()])


def make_plugin(**cfg):
    from astrbot_plugin_workbuddy_usage.main import WorkBuddyUsagePlugin

    defaults = {
        "base_url": "http://127.0.0.1:1",
        "panel_password": "pw",
        "panel_token": "tok",
        "cooldown_seconds": 0,
        "allow_admin": True,
        "allow_private": True,
    }
    defaults.update(cfg)
    return WorkBuddyUsagePlugin(context=None, config=defaults)


def wire(plugin, stub_hub, session) -> WorkBuddyClient:
    client = WorkBuddyClient(
        base_url=stub_hub.base_url,
        panel_password="pw",
        panel_token="preexisting-token",
        timeout_seconds=5.0,
        max_response_bytes=5 * 1024 * 1024,
        session=session,
    )
    plugin._session = session
    plugin._client = client
    return client


async def run_wb(plugin):
    return [m async for m in plugin.wb(FakeEvent())]


async def run_diag(plugin):
    return [m async for m in plugin.wbdiag(FakeEvent())]


def text_of(results) -> str:
    return results[0].chain[0].text


# ---------------------------------------------------------------------------
# Client-level freshness bookkeeping
# ---------------------------------------------------------------------------


class TestHubClockAge:
    async def test_age_is_none_until_a_date_header(self, stub_hub, session):
        client = wire(make_plugin(), stub_hub, session)
        assert client.hub_now() is None
        assert client.hub_now_age() is None
        await client.fetch_analytics(1.0, 2.0)
        age = client.hub_now_age()
        assert age is not None
        assert 0.0 <= age < HUB_CLOCK_FRESH_SECONDS

    async def test_each_response_refreshes_the_capture(self, stub_hub, session):
        client = wire(make_plugin(), stub_hub, session)
        await client.fetch_analytics(1.0, 2.0)
        first = client.hub_now()
        await client.fetch_accounts()
        second = client.hub_now()
        assert first is not None and second is not None
        assert second >= first
        assert client.hub_now_age() < HUB_CLOCK_FRESH_SECONDS


# ---------------------------------------------------------------------------
# The regression: a captured clock from an earlier /wb must not set the window
# ---------------------------------------------------------------------------


class TestWindowUsesFreshClock:
    def test_stale_capture_is_rejected(self):
        plugin = make_plugin()
        client = WorkBuddyClient(
            base_url="http://127.0.0.1:1",
            panel_password="",
            panel_token="t",
            timeout_seconds=1.0,
            max_response_bytes=65536,
            session=None,  # no I/O in this test
        )
        plugin._client = client
        client._hub_now = datetime.now(dt_timezone.utc)
        client._hub_now_monotonic = time.monotonic()
        assert plugin._fresh_hub_clock() == client._hub_now
        client._hub_now_monotonic = time.monotonic() - (HUB_CLOCK_FRESH_SECONDS + 1)
        assert plugin._fresh_hub_clock() is None
        # An unparseable/absent Date leaves no capture at all.
        client._hub_now_monotonic = None
        assert plugin._fresh_hub_clock() is None

    async def test_yesterdays_capture_does_not_set_the_window(
        self, stub_hub, session
    ):
        plugin = make_plugin()
        client = wire(plugin, stub_hub, session)
        # What the previous /wb left behind: a whole day old.
        client._hub_now = datetime.now(dt_timezone.utc) - timedelta(days=1)
        client._hub_now_monotonic = time.monotonic() - 86400

        results = await run_wb(plugin)
        call = stub_hub.analytics_calls[-1]
        today = midnight_of(datetime.now(dt_timezone.utc))

        assert int(call["since"]) == int(today.timestamp())
        assert abs(int(call["until"]) - time.time()) < 120
        # The rendered day is today, not the stale capture's day.
        assert text_of(results).splitlines()[1] == today.date().isoformat()

    async def test_thirty_minute_old_capture_does_not_truncate_the_window(
        self, stub_hub, session
    ):
        plugin = make_plugin()
        client = wire(plugin, stub_hub, session)
        # Within the same day, so the *day* is right but `until` used to be
        # the previous query's instant: recent usage was silently missing.
        client._hub_now = datetime.now(dt_timezone.utc) - timedelta(minutes=30)
        client._hub_now_monotonic = time.monotonic() - 1800

        await run_wb(plugin)
        call = stub_hub.analytics_calls[-1]
        assert abs(int(call["until"]) - time.time()) < 120
        assert int(call["since"]) == int(
            midnight_of(datetime.now(dt_timezone.utc)).timestamp()
        )

    async def test_fresh_capture_still_governs_the_window(self, stub_hub, session):
        # The Hub clock remains the authority: a fresh /accounts Date is used
        # verbatim, so the window ends at the Hub's instant (not local now).
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        hub_instant = datetime.now(dt_timezone.utc) - timedelta(seconds=300)
        stub_hub.accounts_date = http_date(hub_instant)

        await run_wb(plugin)
        call = stub_hub.analytics_calls[-1]
        assert int(call["until"]) == int(hub_instant.timestamp())
        assert int(call["since"]) == int(midnight_of(hub_instant).timestamp())


# ---------------------------------------------------------------------------
# /accounts unavailable: provisional local window, corrected once by the Hub
# ---------------------------------------------------------------------------


class TestAccountsDownFallback:
    async def test_same_day_needs_no_second_analytics_call(
        self, stub_hub, session
    ):
        stub_hub.accounts_status = 500
        plugin = make_plugin()
        wire(plugin, stub_hub, session)

        await run_wb(plugin)
        assert len(stub_hub.analytics_calls) == 1
        call = stub_hub.analytics_calls[-1]
        assert int(call["since"]) == int(
            midnight_of(datetime.now(dt_timezone.utc)).timestamp()
        )

    async def test_different_hub_day_refetches_once(self, stub_hub, session):
        # UTC-hosted AstrBot reporting a +08:00 day, or a host clock that
        # crossed midnight early: the analytics Date disagrees about the day.
        yesterday = datetime.now(dt_timezone.utc) - timedelta(days=1)
        stub_hub.accounts_status = 500
        stub_hub.analytics_date = http_date(yesterday)
        plugin = make_plugin()
        wire(plugin, stub_hub, session)

        results = await run_wb(plugin)
        assert len(stub_hub.analytics_calls) == 2
        last = stub_hub.analytics_calls[-1]
        assert int(last["since"]) == int(midnight_of(yesterday).timestamp())
        assert int(last["until"]) == int(yesterday.timestamp())
        assert text_of(results).splitlines()[1] == midnight_of(yesterday).date().isoformat()


# ---------------------------------------------------------------------------
# /wbdiag must expose the age of the remembered Hub clock
# ---------------------------------------------------------------------------


class TestDiagClockAge:
    async def test_stale_capture_is_annotated_with_its_age(self, stub_hub, session):
        plugin = make_plugin()
        client = wire(plugin, stub_hub, session)
        client._hub_now = datetime.now(dt_timezone.utc) - timedelta(minutes=10)
        client._hub_now_monotonic = time.monotonic() - 600

        text = text_of(await run_diag(plugin))
        hub_line = next(l for l in text.splitlines() if l.startswith("Hub 时间:"))
        assert "采集于" in hub_line and "秒前" in hub_line

    async def test_never_captured_stays_unknown(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run_diag(plugin))
        hub_line = next(l for l in text.splitlines() if l.startswith("Hub 时间:"))
        assert "未知" in hub_line
