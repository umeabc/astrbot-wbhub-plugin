"""Authorization tests with a fake AstrBot event object (no AstrBot install)."""
from __future__ import annotations

from typing import Any

import pytest

from astrbot_plugin_workbuddy_usage.main import WorkBuddyUsagePlugin


class FakeConfig(dict):
    """Minimal stand-in for AstrBotConfig (dict-like with .get)."""


class FakeEvent:
    def __init__(self, platform: str = "aiocqhttp", sender: str | None = "10001",
                 group: str | None = None, admin: bool = False) -> None:
        self._platform = platform
        self._sender = sender
        self._group = group
        self._admin = admin
        self.stopped = False

    def get_platform_name(self) -> str:
        return self._platform

    def get_sender_id(self) -> str | None:
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


def make_plugin(**cfg: Any) -> WorkBuddyUsagePlugin:
    defaults = {
        "allowed_qq_users": [],
        "allowed_qq_groups": [],
        "allow_admin": True,
        "allow_private": True,
    }
    defaults.update(cfg)
    return WorkBuddyUsagePlugin(context=None, config=FakeConfig(defaults))  # type: ignore[arg-type]


class TestMultiPlatform:
    """Any platform is now authorized via the same allowlists."""

    @pytest.mark.parametrize("platform", ["telegram", "discord", "wecom", "lark"])
    def test_other_platforms_authorized(self, platform):
        plugin = make_plugin(allowed_users=["10001"])
        ev = FakeEvent(platform=platform, sender="10001", group=None)
        assert plugin._allowed(ev) is True

    def test_non_qq_allowed_if_admin(self):
        plugin = make_plugin()
        ev = FakeEvent(platform="telegram", sender="x", admin=True)
        assert plugin._allowed(ev) is True


class TestLegacyKeys:
    def test_legacy_users_still_authorize_qq(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        assert plugin._allowed(FakeEvent(platform="aiocqhttp", group=None, sender="10001")) is True

    def test_legacy_users_authorize_other_platforms(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        assert plugin._allowed(FakeEvent(platform="telegram", group=None, sender="10001")) is True

    def test_legacy_and_new_keys_union(self):
        plugin = make_plugin(allowed_users=["10001"], allowed_qq_users=["20002"])
        assert plugin._allowed(FakeEvent(platform="telegram", group=None, sender="10001")) is True
        assert plugin._allowed(FakeEvent(platform="aiocqhttp", group=None, sender="20002")) is True
        assert plugin._allowed(FakeEvent(platform="telegram", group=None, sender="99999")) is False

    def test_legacy_groups_still_authorize(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allowed_qq_groups=["20001"])
        assert plugin._allowed(FakeEvent(group="20001", sender="10001")) is True


class TestPlatformGate:
    def test_gate_empty_allows_all(self):
        plugin = make_plugin(allowed_users=["10001"])
        assert plugin._allowed(FakeEvent(platform="telegram", group=None, sender="10001")) is True

    def test_gate_matching_platform_allowed(self):
        plugin = make_plugin(allowed_platforms=["telegram"], allowed_users=["10001"])
        ev = FakeEvent(platform="telegram", group=None, sender="10001")
        assert plugin._allowed(ev) is True

    def test_gate_mismatch_denies(self):
        plugin = make_plugin(allowed_platforms=["telegram"], allowed_users=["10001"])
        ev = FakeEvent(platform="aiocqhttp", group=None, sender="10001")
        r = plugin._deny_reason(ev)
        assert r == "无权查询 WorkBuddy 用量。（当前消息平台不在 allowed_platforms 中：aiocqhttp）"

    def test_gate_case_insensitive(self):
        plugin = make_plugin(allowed_platforms=["Telegram"], allowed_users=["10001"])
        assert plugin._allowed(FakeEvent(platform="TELEGRAM", group=None, sender="10001")) is True

    def test_gate_fail_closed_on_empty_adapter(self):
        plugin = make_plugin(allowed_platforms=["telegram"], allowed_users=["10001"])
        ev = FakeEvent(platform="", group=None, sender="10001")
        assert plugin._allowed(ev) is False
        r = plugin._deny_reason(ev)
        assert r == "无权查询 WorkBuddy 用量。（当前消息平台不在 allowed_platforms 中：未知）"

    def test_gate_applies_to_admin_too(self):
        plugin = make_plugin(allowed_platforms=["telegram"])
        assert plugin._allowed(FakeEvent(platform="aiocqhttp", sender="x", admin=True)) is False


class TestAdminBypass:
    def test_admin_allowed_private(self):
        plugin = make_plugin(allow_private=True)
        assert plugin._allowed(FakeEvent(group=None, admin=True)) is True

    def test_admin_respects_allow_private_false(self):
        plugin = make_plugin(allow_private=False)
        assert plugin._allowed(FakeEvent(group=None, admin=True)) is False

    def test_admin_allowed_group(self):
        plugin = make_plugin()
        assert plugin._allowed(FakeEvent(group="20001", admin=True)) is True

    def test_admin_bypass_disabled(self):
        plugin = make_plugin(allow_admin=False)
        assert plugin._allowed(FakeEvent(group="20001", admin=True)) is False


class TestWhitelists:
    def test_empty_allowlists_deny_non_admin(self):
        plugin = make_plugin()
        assert plugin._allowed(FakeEvent(group="20001", admin=False)) is False
        assert plugin._allowed(FakeEvent(group=None, admin=False)) is False

    def test_private_user_whitelisted(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        assert plugin._allowed(FakeEvent(group=None, sender="10001")) is True

    def test_private_user_not_whitelisted(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        assert plugin._allowed(FakeEvent(group=None, sender="10002")) is False

    def test_group_requires_both_sender_and_group(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allowed_qq_groups=["20001"])
        assert plugin._allowed(FakeEvent(group="20001", sender="10001")) is True
        # user ok, group wrong
        assert plugin._allowed(FakeEvent(group="20002", sender="10001")) is False
        # group ok, user wrong
        assert plugin._allowed(FakeEvent(group="20001", sender="10002")) is False

    def test_group_whitelist_alone_never_authorizes(self):
        plugin = make_plugin(allowed_qq_groups=["20001"])
        assert plugin._allowed(FakeEvent(group="20001", sender="99999")) is False

    def test_numeric_ids_normalized_to_strings(self):
        plugin = make_plugin(allowed_qq_users=[10001], allowed_qq_groups=[20001])


class TestCredentialsDefaults:
    def test_shipped_defaults_have_credentials_configured(self):
        # The shipped schema defaults panel_password to "admin", so a
        # config built from defaults alone must report credentials configured.
        plugin = make_plugin(panel_password="admin", panel_token="")
        assert plugin._credentials_configured() is True

    def test_both_empty_reports_unconfigured(self):
        plugin = make_plugin(panel_password="", panel_token="")
        assert plugin._credentials_configured() is False
