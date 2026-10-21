"""v1.2.0 — gateway update notice: client endpoints, notice decision,
report rendering, throttled background check, terminate cleanup, /wbdiag.

All offline (aiohttp stub server + pure functions).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from astrbot_plugin_workbuddy_usage.client import (
    HubAuthError,
    HubProtocolError,
    HubUnavailableError,
    WorkBuddyClient,
)
from astrbot_plugin_workbuddy_usage.reporting import (
    append_update_notice,
    derive_update_notice,
    format_report,
    parse_version_tuple,
)
from conftest import make_analytics, make_stat

# NOTE: module contains sync tests; asyncio_mode=auto handles both.


def make_client(hub, session, **kw) -> WorkBuddyClient:
    return WorkBuddyClient(
        base_url=hub.base_url,
        panel_password=kw.get("password", hub.password),
        panel_token=kw.get("token", ""),
        timeout_seconds=kw.get("timeout", 5.0),
        max_response_bytes=kw.get("max_bytes", 5 * 1024 * 1024),
        session=session,
        proxy=kw.get("proxy", ""),
    )


# ---------------------------------------------------------------------------
# Pure notice decision
# ---------------------------------------------------------------------------


class TestDeriveUpdateNotice:
    def test_newer_version_shown(self):
        payload = {"current_version": "1.7.24", "latest_version": "1.7.25"}
        assert derive_update_notice(payload) == "网关有更新，最新版本号v1.7.25"

    def test_equal_no_notice(self):
        payload = {"current_version": "1.7.24", "latest_version": "1.7.24"}
        assert derive_update_notice(payload) == ""

    def test_older_no_notice(self):
        payload = {"current_version": "1.7.25", "latest_version": "1.7.24"}
        assert derive_update_notice(payload) == ""

    def test_latest_tag_used_when_version_null(self):
        payload = {"current_version": "1.7.24", "latest_version": None,
                   "latest_tag": "v1.7.25"}
        assert derive_update_notice(payload) == "网关有更新，最新版本号v1.7.25"

    def test_leading_v_not_doubled(self):
        payload = {"current_version": "v1.7.24", "latest_tag": "v1.7.25"}
        text = derive_update_notice(payload)
        assert text == "网关有更新，最新版本号v1.7.25"
        assert "vv" not in text

    def test_unparseable_candidate_no_notice(self):
        payload = {"current_version": "1.7.24", "latest_version": "not-a-version"}
        assert derive_update_notice(payload) == ""

    def test_unparseable_current_no_notice(self):
        payload = {"current_version": "dev", "latest_version": "1.7.25"}
        assert derive_update_notice(payload) == ""

    def test_both_empty_no_notice(self):
        assert derive_update_notice(
            {"current_version": "1.7.24", "latest_version": None, "latest_tag": ""}
        ) == ""
        assert derive_update_notice({}) == ""

    def test_not_a_mapping_no_notice(self):
        assert derive_update_notice(None) == ""
        assert derive_update_notice("oops") == ""  # type: ignore[arg-type]

    def test_missing_current_no_notice(self):
        assert derive_update_notice({"latest_version": "1.7.25"}) == ""

    def test_update_available_alone_not_trusted(self):
        # Hub says update_available but versions are equal → no notice.
        payload = {"current_version": "1.7.24", "latest_version": "1.7.24",
                   "update_available": True}
        assert derive_update_notice(payload) == ""

    def test_update_available_false_but_newer_still_shown(self):
        payload = {"current_version": "1.7.24", "latest_version": "1.7.30",
                   "update_available": False}
        assert derive_update_notice(payload) == "网关有更新，最新版本号v1.7.30"

    def test_parse_version_tuple(self):
        assert parse_version_tuple("1.7.24") == (1, 7, 24)
        assert parse_version_tuple("v1.7.24") == (1, 7, 24)
        assert parse_version_tuple(" 1.7.24 ") == (1, 7, 24)
        assert parse_version_tuple("1.7") is None
        assert parse_version_tuple("1.7.24.1") is None
        assert parse_version_tuple("") is None
        assert parse_version_tuple(None) is None
        assert parse_version_tuple(1724) is None


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _baseline_report_text() -> str:
    from datetime import datetime, timezone as dt_timezone
    from astrbot_plugin_workbuddy_usage.reporting import aggregate

    accounts = [{
        "uid": "u1", "nickname": "n", "realm": "cn", "domain": "",
        "window": make_stat(total_tokens=100, credit=1.5),
        "window_models": {"m": {"requests": 1, "tokens": 100, "cost_cny": 0.0}},
        "credits": {"remain": 9.0},
    }]
    payload = make_analytics(
        accounts, [], make_stat(total_tokens=100, credit=1.5)
    )
    now = datetime(2025, 10, 17, 7, 0, 0, tzinfo=dt_timezone.utc)
    report = aggregate(payload, now.date(), "Asia/Shanghai", now)
    return format_report(report)


class TestReportRendering:
    def test_notice_is_last_line(self):
        baseline = _baseline_report_text()
        text = append_update_notice(baseline, "网关有更新，最新版本号v1.7.25")
        assert text.endswith("网关有更新，最新版本号v1.7.25")
        assert text.splitlines()[-1] == "网关有更新，最新版本号v1.7.25"

    def test_notice_after_warnings(self):
        baseline = _baseline_report_text()
        with_warning = baseline + "\n警告：账号积分合计与总积分不一致。"
        text = append_update_notice(with_warning, "网关有更新，最新版本号v1.7.25")
        lines = text.splitlines()
        assert lines[-2].startswith("警告：")
        assert lines[-1] == "网关有更新，最新版本号v1.7.25"

    def test_no_notice_byte_identical(self):
        baseline = _baseline_report_text()
        assert append_update_notice(baseline, "") == baseline
        assert append_update_notice(baseline, "") is baseline

    def test_full_render_no_notice_when_current(self):
        # The live-Hub situation: current == latest → no notice anywhere.
        text = append_update_notice(
            _baseline_report_text(),
            derive_update_notice(
                {"current_version": "1.7.24", "latest_version": "1.7.24",
                 "latest_tag": "v1.7.24", "update_available": False}
            ),
        )
        assert "网关有更新" not in text


# ---------------------------------------------------------------------------
# Client endpoints
# ---------------------------------------------------------------------------


class TestClientUpdates:
    async def test_fetch_updates_returns_payload(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": None, "latest_tag": ""}
        client = make_client(stub_hub, session, token="preexisting-token")
        payload = await client.fetch_updates()
        assert payload["current_version"] == "1.7.24"

    async def test_fetch_updates_uses_panel_token(self, stub_hub, session):
        stub_hub.updates_body = {}
        client = make_client(stub_hub, session, token="preexisting-token")
        await client.fetch_updates()
        assert stub_hub.updates_calls[-1]["token"] == "preexisting-token"

    async def test_fetch_updates_401_renews_once(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24"}
        client = make_client(stub_hub, session, token="stale-token")
        payload = await client.fetch_updates()
        assert payload["current_version"] == "1.7.24"
        assert len(stub_hub.login_calls) == 1

    async def test_fetch_updates_transport_and_schema_taxonomy(
        self, stub_hub, session
    ):
        from astrbot_plugin_workbuddy_usage.client import HubRateLimitError

        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.updates_status = 500
        with pytest.raises(HubUnavailableError):
            await client.fetch_updates()
        stub_hub.updates_status = 429
        with pytest.raises(HubRateLimitError):
            await client.fetch_updates()
        stub_hub.updates_status = 200
        stub_hub.updates_body = [1, 2, 3]  # top level not an object
        with pytest.raises(HubProtocolError):
            await client.fetch_updates()

    async def test_fetch_updates_tolerant_schema(self, stub_hub, session):
        stub_hub.updates_body = {"weird": {"x": 1}, "future": [1]}
        client = make_client(stub_hub, session, token="preexisting-token")
        payload = await client.fetch_updates()
        assert payload["weird"] == {"x": 1}

    async def test_check_updates_posts_empty_body(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": "1.7.24"}
        client = make_client(stub_hub, session, token="preexisting-token")
        payload = await client.check_updates()
        assert payload["latest_version"] == "1.7.24"
        assert stub_hub.update_check_calls == [{"token": "preexisting-token",
                                                "body": {}}]

    async def test_check_updates_auth_failure(self, stub_hub, session):
        client = make_client(stub_hub, session, token="stale-token",
                             password="wrong-password")
        stub_hub.updates_check_status = 401
        with pytest.raises(HubAuthError):
            await client.check_updates()


# ---------------------------------------------------------------------------
# Plugin wiring: report path, throttle, terminate, /wbdiag
# ---------------------------------------------------------------------------


class FakeEvent:
    def __init__(self, sender: str = "10001", group: str | None = None,
                 admin: bool = True) -> None:
        self._sender = sender
        self._group = group
        self._admin = admin
        self.stopped = False

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_sender_id(self) -> str:
        return self._sender

    def get_group_id(self) -> str | None:
        return self._group

    def is_admin(self) -> bool:
        return self._admin

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str):
        from astrbot.api.event import MessageEventResult
        return MessageEventResult([type("Plain", (), {"text": text})()])


def make_plugin(**cfg: Any):
    from astrbot_plugin_workbuddy_usage.main import WorkBuddyUsagePlugin

    defaults: dict[str, Any] = {
        "base_url": "http://127.0.0.1:1",  # never contacted directly
        "panel_password": "pw",
        "panel_token": "tok",
        "cooldown_seconds": 0,
        "allow_admin": True,
        "allow_private": True,
    }
    defaults.update(cfg)
    return WorkBuddyUsagePlugin(context=None, config=defaults)  # type: ignore[arg-type]


async def collect_wb(plugin, ev):
    return [m async for m in plugin.wb(ev)]


async def collect_diag(plugin, ev):
    return [m async for m in plugin.wbdiag(ev)]


def wire_stub(plugin, stub_hub, session):
    """Attach a real client pointed at the stub Hub."""
    plugin._session = session
    plugin._client = make_client(stub_hub, session, token="preexisting-token")


class TestPluginReportPath:
    async def test_notice_rendered_last_against_stub(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": "1.7.25"}
        plugin = make_plugin(update_check_interval_hours=168)
        wire_stub(plugin, stub_hub, session)
        results = await collect_wb(plugin, FakeEvent())
        text = results[0].chain[0].text
        assert text.splitlines()[-1] == "网关有更新，最新版本号v1.7.25"

    async def test_updates_transport_failure_renders_without_notice(
        self, stub_hub, session
    ):
        stub_hub.updates_status = 500
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        results = await collect_wb(plugin, FakeEvent())
        text = results[0].chain[0].text
        assert "网关有更新" not in text
        assert "WorkBuddy 当日用量" in text  # report still rendered

    async def test_updates_auth_failure_renders_without_notice(
        self, stub_hub, session
    ):
        stub_hub.updates_check_status = 401
        stub_hub.updates_status = 401
        plugin = make_plugin(panel_password="dummy-password")
        wire_stub(plugin, stub_hub, session)
        # preexisting-token is valid for analytics/accounts; /updates 401
        # triggers one renewal+replay then fails if still 401 → but with a
        # valid password it renews fine. Force repeated 401 via bad token:
        stub_hub.updates_status = 401
        stub_hub.valid_tokens = set()  # even renewed tokens rejected
        results = await collect_wb(plugin, FakeEvent())
        text = results[0].chain[0].text
        assert "网关有更新" not in text
        assert "WorkBuddy 当日用量" in text

    async def test_updates_schema_failure_renders_without_notice(
        self, stub_hub, session
    ):
        stub_hub.updates_status = 200
        stub_hub.updates_body = "not-an-object"
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        results = await collect_wb(plugin, FakeEvent())
        assert "网关有更新" not in results[0].chain[0].text

    async def test_update_notice_false_skips_get_and_check(
        self, stub_hub, session
    ):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": "9.9.9"}
        plugin = make_plugin(update_notice=False)
        wire_stub(plugin, stub_hub, session)
        results = await collect_wb(plugin, FakeEvent())
        assert "网关有更新" not in results[0].chain[0].text
        assert stub_hub.updates_calls == []
        assert stub_hub.update_check_calls == []
        assert plugin._update_check_task is None


class TestBackgroundCheckThrottle:
    async def test_fires_when_interval_elapsed_even_with_data(
        self, stub_hub, session
    ):
        # The live situation: latest_version already present and equal to
        # current. A background check MUST still be scheduled (the plugin
        # must not go permanently silent just because it once saw data).
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": "1.7.24",
                                 "latest_tag": "v1.7.24"}
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        await collect_wb(plugin, FakeEvent())
        assert plugin._update_check_task is not None
        await asyncio.sleep(0.05)  # let the background task actually issue its request
        assert stub_hub.update_check_calls, "background check should fire"
        await plugin._update_check_task
        # After the check, the stored notice reflects the fresh payload
        # (equal versions → no notice).
        assert plugin._update_notice == ""

    async def test_not_fired_within_interval(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": "1.7.24"}
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        plugin._last_update_check_ts = time.monotonic()  # just checked
        await collect_wb(plugin, FakeEvent())
        assert stub_hub.update_check_calls == []
        assert plugin._update_check_task is None

    async def test_never_more_than_one_in_flight(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": "1.7.24"}
        stub_hub.updates_check_delay = 0.3
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        await collect_wb(plugin, FakeEvent())
        first = plugin._update_check_task
        # A second /wb while the first check is still running must NOT spawn
        # another task.
        await collect_wb(plugin, FakeEvent())
        assert plugin._update_check_task is first
        await first

    async def test_report_path_does_not_await_check(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": "1.7.24"}
        stub_hub.updates_check_delay = 0.3
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        start = time.monotonic()
        await collect_wb(plugin, FakeEvent())
        elapsed = time.monotonic() - start
        assert elapsed < 0.2, "report path must not await the background check"
        await plugin._update_check_task

    async def test_failure_swallowed_and_no_storm(self, stub_hub, session):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": None, "latest_tag": ""}
        stub_hub.updates_check_status = 500
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        await collect_wb(plugin, FakeEvent())
        task = plugin._update_check_task
        await task  # must not raise
        assert plugin._update_notice == ""
        assert len(stub_hub.update_check_calls) == 1

    async def test_newer_version_found_appears_next_report(
        self, stub_hub, session
    ):
        stub_hub.updates_body = {"current_version": "1.7.24",
                                 "latest_version": None, "latest_tag": ""}
        stub_hub.updates_check_result = {
            "current_version": "1.7.24", "latest_version": "1.7.25",
        }
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        # First /wb: no notice (no version data yet), check fired.
        results = await collect_wb(plugin, FakeEvent())
        assert "网关有更新" not in results[0].chain[0].text
        await plugin._update_check_task
        assert plugin.report_notice() == "网关有更新，最新版本号v1.7.25"
        # Second /wb: notice now shown, and NO second check within interval.
        calls_before = len(stub_hub.update_check_calls)
        results = await collect_wb(plugin, FakeEvent())
        assert results[0].chain[0].text.splitlines()[-1] == (
            "网关有更新，最新版本号v1.7.25"
        )
        assert len(stub_hub.update_check_calls) == calls_before


class TestTerminate:
    async def test_terminate_cancels_pending_check(self, stub_hub, session):
        stub_hub.updates_check_delay = 5.0
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        plugin._last_update_check_ts = None
        plugin._maybe_schedule_update_check()
        task = plugin._update_check_task
        assert task is not None and not task.done()
        await plugin.terminate()
        assert task.cancelled() or task.done()
        assert plugin._update_check_task is None

    async def test_terminate_safe_without_task(self, stub_hub, session):
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        await plugin.terminate()  # must not raise
        await plugin.terminate()  # double-terminate also safe


class TestWbDiagNoticeState:
    def _text(self, results):
        return results[0].chain[0].text

    async def test_diag_shows_known_notice(self):
        plugin = make_plugin()
        plugin._client = None
        plugin._session = None
        plugin._update_notice = "网关有更新，最新版本号v1.7.25"
        text = self._text(await collect_diag(plugin, FakeEvent()))
        assert "网关更新: 有 v1.7.25" in text
        assert "有 网关有更新" not in text  # never repeat the full sentence

    async def test_diag_shows_none_when_unknown(self):
        plugin = make_plugin()
        plugin._client = None
        plugin._session = None
        text = self._text(await collect_diag(plugin, FakeEvent()))
        assert "网关更新: 无或未知（诊断不联网）" in text

    async def test_diag_shows_disabled(self):
        plugin = make_plugin(update_notice=False)
        plugin._client = None
        plugin._session = None
        text = self._text(await collect_diag(plugin, FakeEvent()))
        assert "网关更新: 已关闭" in text

    async def test_diag_makes_no_network_calls(self, stub_hub, session):
        plugin = make_plugin()
        wire_stub(plugin, stub_hub, session)
        await collect_diag(plugin, FakeEvent())
        assert stub_hub.updates_calls == []
        assert stub_hub.update_check_calls == []
