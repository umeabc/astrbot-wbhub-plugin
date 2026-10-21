"""Tests for _id_set normalization, denial reasons, and the /wbdiag command."""
from __future__ import annotations

import logging
from typing import Any

from astrbot_plugin_workbuddy_usage.main import WorkBuddyUsagePlugin, _id_set

from test_access import FakeEvent, FakeConfig, make_plugin


class TestIdSet:
    def test_empty_list(self):
        assert _id_set([]) == set()

    def test_list_of_str(self):
        assert _id_set(["10001"]) == {"10001"}

    def test_list_of_int(self):
        assert _id_set([10001]) == {"10001"}

    def test_bare_string(self):
        # The regression: a bare string must NOT be split into characters.
        assert _id_set("10001") == {"10001"}

    def test_comma_separated_ascii(self):
        assert _id_set("10001, 10002") == {"10001", "10002"}

    def test_comma_separated_fullwidth(self):
        assert _id_set("10001，10002") == {"10001", "10002"}

    def test_empty_string(self):
        assert _id_set("") == set()

    def test_none(self):
        assert _id_set(None) == set()

    def test_int_scalar(self):
        assert _id_set(12345) == {"12345"}

    def test_float_integral_scalar(self):
        assert _id_set(12345.0) == {"12345"}

    def test_float_non_integral(self):
        assert _id_set(12345.5) == {"12345.5"}

    def test_large_int(self):
        assert _id_set(123456789012345678) == {"123456789012345678"}

    def test_set_input(self):
        assert _id_set({"10001", "10002"}) == {"10001", "10002"}

    def test_tuple_input(self):
        assert _id_set(("10001", 10002)) == {"10001", "10002"}

    def test_whitespace_stripped(self):
        assert _id_set("  10001  , 10002 ") == {"10001", "10002"}

    def test_odd_objects_ignored(self):
        class Weird:
            def __str__(self):
                raise RuntimeError("boom")

        assert _id_set(Weird()) == set()
        assert _id_set([None, "", "10001"]) == {"10001"}


class TestBareStringAuthorization:
    """The operator-facing bug: config value stored as bare string '10001'."""

    def test_private_bare_string_authorizes(self):
        plugin = make_plugin(allowed_qq_users="10001")
        assert plugin._allowed(FakeEvent(group=None, sender="10001")) is True

    def test_private_bare_string_denies_other(self):
        plugin = make_plugin(allowed_qq_users="10001")
        assert plugin._allowed(FakeEvent(group=None, sender="10002")) is False

    def test_group_bare_strings_authorize(self):
        plugin = make_plugin(allowed_qq_users="10001", allowed_qq_groups="20001")
        assert plugin._allowed(FakeEvent(group="20001", sender="10001")) is True

    def test_comma_separated_string_authorizes(self):
        plugin = make_plugin(allowed_qq_users="10001，10002")
        assert plugin._allowed(FakeEvent(group=None, sender="10002")) is True

    def test_numeric_list_authorizes(self):
        plugin = make_plugin(allowed_qq_users=[10001], allowed_qq_groups=[20001])
        assert plugin._allowed(FakeEvent(group="20001", sender="10001")) is True


class TestDenyReasons:
    def _reason(self, plugin, ev):
        return plugin._deny_reason(ev)

    def test_non_qq_adapter_reason(self):
        plugin = make_plugin()
        r = self._reason(plugin, FakeEvent(platform="telegram", sender="10001"))
        assert r == "无权查询 WorkBuddy 用量。（当前消息平台不受支持：telegram）"

    def test_non_qq_adapter_unknown_name(self):
        plugin = make_plugin()
        r = self._reason(plugin, FakeEvent(platform="", sender="10001"))
        assert r == "无权查询 WorkBuddy 用量。（当前消息平台不受支持：未知）"

    def test_sender_id_missing(self):
        plugin = make_plugin()
        r = self._reason(plugin, FakeEvent(platform="aiocqhttp", sender=None))
        assert r == "无权查询 WorkBuddy 用量。（无法识别你的账号标识）"

    def test_private_allow_private_false(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allow_private=False)
        r = self._reason(plugin, FakeEvent(group=None, sender="10001", admin=True))
        assert r == "无权查询 WorkBuddy 用量。（私聊查询已被关闭：allow_private=false）"

    def test_private_user_missing(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        r = self._reason(plugin, FakeEvent(group=None, sender="10002"))
        assert r == "无权查询 WorkBuddy 用量。（你的账号不在 allowed_qq_users 白名单中）"

    def test_group_user_missing_distinct_from_private(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allowed_qq_groups=["20001"])
        r = self._reason(plugin, FakeEvent(group="20001", sender="10002"))
        assert r == (
            "无权查询 WorkBuddy 用量。（你的账号不在 allowed_qq_users 白名单中；"
            "群聊要求 allowed_qq_users 与 allowed_qq_groups 同时命中）"
        )
        assert r != self._reason(plugin, FakeEvent(group=None, sender="10002"))

    def test_group_missing_distinct(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        r = self._reason(plugin, FakeEvent(group="20001", sender="10001"))
        assert r == "无权查询 WorkBuddy 用量。（本群不在 allowed_qq_groups 白名单中）"

    def test_group_both_missing_reports_sender(self):
        plugin = make_plugin()
        r = self._reason(plugin, FakeEvent(group="20001", sender="10002"))
        assert "allowed_qq_users 白名单中；" in r

    def test_admin_bypass_disabled(self):
        plugin = make_plugin(allow_admin=False)
        r = self._reason(plugin, FakeEvent(group="20001", sender="10001", admin=True))
        assert r == "无权查询 WorkBuddy 用量。（管理员豁免已被 allow_admin=false 关闭）"

    def test_admin_bypass_disabled_private(self):
        plugin = make_plugin(allow_admin=False, allowed_qq_users=["10001"])
        r = self._reason(plugin, FakeEvent(group=None, sender="10001", admin=True))
        assert r == "无权查询 WorkBuddy 用量。（管理员豁免已被 allow_admin=false 关闭）"

    def test_denial_is_logged_at_info(self, caplog):
        plugin = make_plugin(allowed_qq_users=["10001"])
        with caplog.at_level(logging.INFO, logger="astrbot.stub"):
            plugin._deny_reason(FakeEvent(group=None, sender="10002"))
        assert any("denied" in rec.message and "白名单" in rec.message
                   for rec in caplog.records)

    def test_allowed_produces_no_log(self, caplog):
        plugin = make_plugin(allowed_qq_users=["10001"])
        with caplog.at_level(logging.INFO, logger="astrbot.stub"):
            assert plugin._deny_reason(FakeEvent(group=None, sender="10001")) == ""
        assert not any("denied" in rec.message for rec in caplog.records)


class TestWbDiag:
    def _collect(self, plugin, ev):
        import asyncio

        async def run():
            return [msg async for msg in plugin.wbdiag(ev)]

        return asyncio.run(run())

    def test_private_diag_unauthorized(self):
        plugin = make_plugin(allowed_qq_users=[])  # empty -> refused
        ev = FakeEvent(platform="aiocqhttp", sender="10001", group=None)
        results = self._collect(plugin, ev)
        assert results, "wbdiag must reply even to an unauthorized caller"
        text = results[0].chain[0].text if hasattr(results[0], "chain") else str(results[0])
        assert "10001" in text
        assert "拒绝" in text and "allowed_qq_users" in text

    def test_group_diag_unauthorized(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allowed_qq_groups=["20001"])
        ev = FakeEvent(platform="aiocqhttp", sender="99999", group="20002")
        results = self._collect(plugin, ev)
        text = results[0].chain[0].text
        assert "群聊" in text
        assert "99999" in text and "20002" in text
        assert "拒绝" in text

    def test_diag_allowed(self):
        plugin = make_plugin(allowed_qq_users="10001")
        ev = FakeEvent(platform="aiocqhttp", sender="10001", group=None)
        results = self._collect(plugin, ev)
        text = results[0].chain[0].text
        assert "允许" in text

    def test_diag_does_not_print_configured_ids(self):
        plugin = make_plugin(allowed_qq_users="10001", allowed_qq_groups="20001")
        # stranger asks in a group
        ev = FakeEvent(platform="aiocqhttp", sender="99999", group="20002")
        results = self._collect(plugin, ev)
        text = results[0].chain[0].text
        assert "10001" not in text  # other users' ids never leak
        assert "20001" not in text

    def test_diag_no_network_or_credentials_required(self):
        # plugin with no client/session at all must still answer
        plugin = make_plugin()
        plugin._client = None
        plugin._session = None
        ev = FakeEvent(platform="aiocqhttp", sender="10001", group=None)
        results = self._collect(plugin, ev)
        assert results


class TestWbDenialReplies:
    """Each of the 8 denial outcomes gets its own exact chat string."""

    def _collect_wb(self, plugin, ev):
        import asyncio

        async def run():
            return [m async for m in plugin.wb(ev)]

        return asyncio.run(run())

    def _first_text(self, plugin, ev):
        return self._collect_wb(plugin, ev)[0].chain[0].text

    def test_case1_adapter_not_qq(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        ev = FakeEvent(platform="telegram", sender="10001")
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（当前消息平台不受支持：telegram）"
        )

    def test_case2_sender_id_missing(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        ev = FakeEvent(platform="aiocqhttp", sender=None)
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（无法识别你的账号标识）"
        )

    def test_case2_sender_id_raises_does_not_crash(self):
        class RaisingEvent(FakeEvent):
            def get_sender_id(self):
                raise RuntimeError("no sender id")

        plugin = make_plugin(allowed_qq_users=["10001"])
        ev = RaisingEvent(platform="aiocqhttp", sender="10001")
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（无法识别你的账号标识）"
        )

    def test_case3_private_closed(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allow_private=False)
        ev = FakeEvent(platform="aiocqhttp", sender="10001", group=None)
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（私聊查询已被关闭：allow_private=false）"
        )

    def test_case4_private_not_whitelisted(self):
        plugin = make_plugin(allowed_qq_users=["10001"])
        ev = FakeEvent(platform="aiocqhttp", sender="10002", group=None)
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（你的账号不在 allowed_qq_users 白名单中）"
        )

    def test_case6_group_sender_not_whitelisted(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allowed_qq_groups=["20001"])
        ev = FakeEvent(platform="aiocqhttp", sender="10002", group="20001")
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（你的账号不在 allowed_qq_users 白名单中；"
            "群聊要求 allowed_qq_users 与 allowed_qq_groups 同时命中）"
        )

    def test_case7_group_not_whitelisted(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allowed_qq_groups=["20001"])
        ev = FakeEvent(platform="aiocqhttp", sender="10001", group="20002")
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（本群不在 allowed_qq_groups 白名单中）"
        )

    def test_case8_admin_bypass_disabled(self):
        plugin = make_plugin(allowed_qq_users=["10001"], allow_admin=False)
        ev = FakeEvent(platform="aiocqhttp", sender="10001", group="20001", admin=True)
        assert self._first_text(plugin, ev) == (
            "无权查询 WorkBuddy 用量。（管理员豁免已被 allow_admin=false 关闭）"
        )

    def test_authorized_private_user_passes_gate_to_not_configured(self):
        """Happy path: authorized non-admin private user gets past the gate.

        With no Hub configured the next error is ERR_NOT_CONFIGURED — proving
        the denial branch did not swallow an allowed caller.
        """
        plugin = make_plugin(allowed_qq_users="10001")
        plugin._client = None
        plugin._session = None
        ev = FakeEvent(platform="aiocqhttp", sender="10001", group=None)
        assert self._first_text(plugin, ev) == "WorkBuddy 查询未配置，请联系管理员。"
