"""v1.0.4 — per-account STATUS rendering and /accounts roster extension.

Covers: status_segments (order, units, recovery times), roster merge,
placeholder handling, nickname suppression and /accounts-failure fallback.
All offline.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_workbuddy_usage.client import HubProtocolError, WorkBuddyClient
from astrbot_plugin_workbuddy_usage.models import AccountsPayload
from astrbot_plugin_workbuddy_usage.reporting import (
    aggregate,
    format_report,
    status_segments,
)
from conftest import make_analytics, make_account, make_stat

TZ = "Asia/Shanghai"
TZ_OBJ = ZoneInfo(TZ)

# Fixed Hub instant: 2025-10-17 15:18:00 UTC+8 (07:18:00 UTC).
HUB_NOW = datetime(2025, 10, 17, 7, 18, 0, tzinfo=dt_timezone.utc)
# 1h1m later → 10-17 16:19 per the relative-seconds path.
BREAKER_SECONDS = 3660


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


def live_account(**overrides) -> dict:
    """A NORMAL /accounts entry (everything at rest)."""
    acct: dict = {
        "uid": "deadbeef",
        "nickname": "13800138000",  # phone number — must never be printed
        "realm": "cn",
        "enabled": True,
        "cooldownFor": None,
        "inCooldown": False,
        "breakerFor": None,
        "degradeFor": None,
        "modelCooldowns": [],
        "reserveBlocked": False,
        "dailyLimitBlocked": False,
        "creditLimitReached": False,
        "lastError": "some upstream error text",
        "expiresAt": None,
        "expiresIn": "25 days",
        "hasRefreshToken": True,
        "credits": {"remain": 2834.96},
    }
    acct.update(overrides)
    return acct


def accounts_payload(*accounts: dict) -> AccountsPayload:
    return {"accounts": list(accounts)}


def build_report(
    live_entries,
    analytics_accounts=None,
    phone="13800138000",
):
    """Aggregate an analytics payload joined to /accounts entries."""
    accounts = list(analytics_accounts if analytics_accounts is not None else
                    [make_account(e["uid"], phone, e.get("realm", "cn")) for e in live_entries])
    summary = make_stat(total_tokens=sum(int(a["window"]["total_tokens"]) for a in accounts),
                        credit=sum(Decimal(str(a["window"]["credit"])) for a in accounts))
    payload = make_analytics(accounts, [], summary)
    return aggregate(
        payload,
        HUB_NOW.astimezone(TZ_OBJ).date(),
        TZ,
        HUB_NOW.astimezone(TZ_OBJ),
        accounts_payload=accounts_payload(*live_entries),
        hub_now=HUB_NOW,
    )


def line_for(text: str, uid: str) -> str:
    return next(l for l in text.split("\n") if l.startswith(uid[:8]))


class TestStatusSegments:
    async def test_normal_account_no_parentheses(self):
        seg = status_segments(live_account(), TZ, HUB_NOW)
        assert seg == ""
        report = build_report([live_account()])
        line = line_for(format_report(report), "deadbeef")
        assert "(" not in line and "：" in line

    async def test_breaker_relative_seconds_use_hub_clock(self):
        seg = status_segments(live_account(breakerFor=BREAKER_SECONDS), TZ, HUB_NOW)
        # hub_now (10-17 15:18 北京时间) + 3660s → 16:19
        assert seg == "熔断，10-17 16:19 恢复"

    async def test_degrade_relative_seconds(self):
        seg = status_segments(live_account(degradeFor=120), TZ, HUB_NOW)
        assert seg == "降级，10-17 15:20 恢复"

    async def test_cooldown_relative_seconds(self):
        seg = status_segments(live_account(cooldownFor=300, inCooldown=True), TZ, HUB_NOW)
        assert seg == "冷却，10-17 15:23 恢复"

    async def test_incooldown_without_cooldownfor(self):
        seg = status_segments(live_account(cooldownFor=None, inCooldown=True), TZ, HUB_NOW)
        assert seg == "冷却，10-17 15:18 恢复"

    async def test_model_429_absolute_epoch_not_added_to_now(self):
        # expiresAt is ABSOLUTE epoch: 2025-10-17 07:23:00Z = 15:23 北京时间.
        # Rendered AS-IS — if the code added it to now the date would be absurd.
        epoch = 1760685780
        seg = status_segments(
            live_account(modelCooldowns=[{"model": "deepseek-v4.1-flash",
                                          "expiresAt": epoch}]),
            TZ, HUB_NOW,
        )
        assert seg == "deepseek-v4.1-flash被429，10-17 15:23 恢复"

    async def test_model_capped_at_quota_exhaustion(self):
        epoch = 1760685780
        seg = status_segments(
            live_account(modelCooldowns=[{"model": "deepseek-v4.1-flash",
                                          "expiresAt": epoch, "cappedAt": 1760680000}]),
            TZ, HUB_NOW,
        )
        assert seg == "deepseek-v4.1-flash额度用满，10-17 15:23 恢复"

    async def test_multiple_statuses_documented_order(self):
        epoch = 1760687400  # 10-17 15:50 北京时间
        seg = status_segments(
            live_account(
                breakerFor=60,             # 熔断 15:19
                degradeFor=120,            # 降级 15:20
                cooldownFor=300,           # 冷却 15:23
                modelCooldowns=[{"model": "m-a", "expiresAt": epoch}],  # 429 15:50
                reserveBlocked=True,
                dailyLimitBlocked=True,
                creditLimitReached=True,
            ),
            TZ, HUB_NOW,
        )
        assert seg == (
            "熔断，10-17 15:19 恢复；降级，10-17 15:20 恢复；"
            "冷却，10-17 15:23 恢复；m-a被429，10-17 15:50 恢复；"
            "保留积分停单；当日限额停单；积分达限（仅免费模型）"
        )

    async def test_disabled_no_time(self):
        seg = status_segments(live_account(enabled=False), TZ, HUB_NOW)
        assert seg == "已停用"

    async def test_boolean_guards(self):
        assert status_segments(live_account(reserveBlocked=True), TZ, HUB_NOW) == "保留积分停单"
        assert status_segments(live_account(dailyLimitBlocked=True), TZ, HUB_NOW) == "当日限额停单"
        assert status_segments(live_account(creditLimitReached=True), TZ, HUB_NOW) == "积分达限（仅免费模型）"

    async def test_odd_model_cooldown_entry_skipped_not_fatal(self):
        seg = status_segments(
            live_account(modelCooldowns=[{"no_model": 1}, {"model": "m", "expiresAt": "junk"}]),
            TZ, HUB_NOW,
        )
        assert seg == ""

    async def test_none_hub_now_falls_back_to_local_clock(self):
        seg = status_segments(live_account(breakerFor=0), TZ, None)
        assert seg.startswith("熔断，") and seg.endswith(" 恢复")


class TestReportIntegration:
    async def test_status_line_rendering(self):
        report = build_report([live_account(breakerFor=BREAKER_SECONDS, uid="1111aaaa")])
        text = format_report(report)
        line = line_for(text, "1111aaaa")
        assert line == "1111aaaa [cn]【熔断，10-17 16:19 恢复】：已消耗 0 积分，剩余 500 积分"

    async def test_multiple_statuses_in_report_line(self):
        epoch = 1760685780
        report = build_report(
            [live_account(
                uid="2222bbbb", realm="intl",
                modelCooldowns=[{"model": "deepseek-v4.1-flash", "expiresAt": epoch}],
            )]
        )
        line = line_for(format_report(report), "2222bbbb")
        assert "【deepseek-v4.1-flash被429，10-17 15:23 恢复】" in line

    async def test_roster_extended_with_accounts_only_entry(self):
        # /accounts knows an account that analytics does not mention.
        analytics_accounts = [make_account("u1", "账号一", "cn",
                                           make_stat(total_tokens=100, credit=2.5))]
        report = build_report(
            [live_account(uid="u1"), live_account(uid="only-hub", realm="intl", credits={"remain": 123.5})],
            analytics_accounts=analytics_accounts,
        )
        text = format_report(report)
        line = line_for(text, "only-hub")
        assert line.startswith("only-hub [intl]：已消耗 0 积分，剩余 123.5 积分")
        assert "账号（2）：" in text

    async def test_placeholder_excluded_and_unattributed_line_kept(self):
        placeholder = make_account("(unattributed)", "", "cn",
                                   make_stat(total_tokens=500, credit=1.25),
                                   with_credits=False)
        real = make_account("u1", "x", "cn", make_stat(total_tokens=10, credit=0.1))
        report = build_report([live_account(uid="u1")],
                              analytics_accounts=[real, placeholder])
        text = format_report(report)
        assert "(unattributed" not in text
        assert "未归属消耗：1.25 积分 / 500 Token" in text
        assert "账号（1）：" in text

    async def test_sort_consumed_desc_then_uid(self):
        analytics_accounts = [
            make_account("z", "z", "cn", make_stat(credit=1.0)),
            make_account("a", "a", "cn", make_stat(credit=3.0)),
            make_account("m", "m", "cn", make_stat(credit=1.0)),
        ]
        entries = [live_account(uid=u, nickname="13900000000")
                   for u in ("z", "a", "m", "aaa")]
        report = build_report(entries, analytics_accounts=analytics_accounts)
        text = format_report(report)
        lines = text.split("\n")
        idx = lines.index("账号（4）：")
        uids = [l.split(" ")[0].split("[")[0].split("(")[0] for l in lines[idx + 1:idx + 5]]
        assert uids == ["a", "m", "z", "aaa"]

    async def test_nickname_phone_never_printed(self):
        report = build_report([live_account(uid="u1", nickname="13800138000")])
        text = format_report(report)
        assert "13800138000" not in text
        assert "138" not in text.replace("13800138000", "")

    async def test_forbidden_fields_not_printed(self):
        report = build_report([live_account(uid="u1", lastError="boom",
                                            expiresIn="25 days",
                                            hasRefreshToken=True)])
        text = format_report(report)
        assert "boom" not in text and "25 days" not in text


class TestAccountsFailureFallback:
    async def test_failing_accounts_call_still_renders_without_status(self):
        # Simulates the /wb path when fetch_accounts raised: aggregate runs
        # with accounts_payload=None → valid report, no statuses, no extension.
        analytics_accounts = [make_account("u1", "x", "cn",
                                           make_stat(total_tokens=10, credit=0.5))]
        payload = make_analytics(analytics_accounts, [], make_stat(total_tokens=10, credit=0.5))
        report = aggregate(
            payload, HUB_NOW.astimezone(TZ_OBJ).date(), TZ,
            HUB_NOW.astimezone(TZ_OBJ),
            accounts_payload=None, hub_now=HUB_NOW,
        )
        text = format_report(report)
        assert "u1 [cn]：" in text
        assert "(" not in text

    async def test_no_hub_now_uses_local_clock_for_relative(self):
        report = build_report([live_account(breakerFor=60)])
        text = format_report(report)
        assert "熔断，" in text and " 恢复】" in text
        assert "【" in text and "】" in text


class TestClientFetchAccounts:
    async def test_fetch_accounts_uses_realm_all_and_token(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        payload = await client.fetch_accounts()
        assert payload["accounts"] == []

    async def test_fetch_accounts_validates_uid_skips_bad_rows(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.accounts_body = {
            "accounts": [
                {"uid": "ok1", "enabled": True},
                {"uid": "", "enabled": False},
                {"enabled": False},
                "not-a-dict",
                {"uid": 42},
            ],
            "storage": "memory",
            "usable": 1,
            "future_field": {"x": 1},
        }
        payload = await client.fetch_accounts()
        assert [a["uid"] for a in payload["accounts"]] == ["ok1"]
        assert payload["storage"] == "memory"
        assert payload["usable"] == 1

    async def test_fetch_accounts_not_a_list_rejected(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.accounts_body = {"accounts": "nope"}
        with pytest.raises(HubProtocolError):
            await client.fetch_accounts()

    async def test_fetch_accounts_401_renews_once(self, stub_hub, session):
        client = make_client(stub_hub, session, token="stale-token")
        stub_hub.accounts_body = {"accounts": [{"uid": "a"}]}
        payload = await client.fetch_accounts()
        assert len(stub_hub.login_calls) == 1
        assert payload["accounts"][0]["uid"] == "a"

    async def test_fetch_accounts_429_and_500_taxonomy(self, stub_hub, session):
        from astrbot_plugin_workbuddy_usage.client import (
            HubRateLimitError,
            HubUnavailableError,
        )
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.accounts_status = 429
        with pytest.raises(HubRateLimitError):
            await client.fetch_accounts()
        stub_hub.accounts_status = 500
        with pytest.raises(HubUnavailableError):
            await client.fetch_accounts()
        stub_hub.accounts_status = 200

    async def test_fetch_accounts_oversize_rejected(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token", max_bytes=10)
        stub_hub.accounts_body = {"accounts": [{"uid": "a" * 100}]}
        with pytest.raises(HubProtocolError, match="max_response_bytes"):
            await client.fetch_accounts()

    async def test_fetch_accounts_updates_hub_date(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.accounts_date = "Mon, 17 Mar 2025 08:00:00 GMT"
        await client.fetch_accounts()
        assert client.hub_now() == datetime(2025, 3, 17, 8, 0, 0,
                                            tzinfo=dt_timezone.utc)


class TestLivePayloadFormatting:
    async def test_operator_example_line_shapes(self):
        # Mirrors the operator's spec sample exactly.
        epoch = 1760685780  # 10-17 15:23 北京时间
        report = build_report([
            live_account(uid="deadbeef"),
            live_account(uid="1111aaaa", breakerFor=BREAKER_SECONDS),
            live_account(uid="2222bbbb", realm="intl",
                         modelCooldowns=[{"model": "deepseek-v4.1-flash",
                                          "expiresAt": epoch}]),
        ])
        text = format_report(report)
        assert "deadbeef [cn]：" in text
        assert "1111aaaa [cn]【熔断，10-17 16:19 恢复】：" in text
        assert "2222bbbb [intl]【deepseek-v4.1-flash被429，10-17 15:23 恢复】：" in text


class TestDisabledModelsSegment:
    """v1.0.5 — disabledModels (operator-configured per-account blacklist)."""

    async def test_non_empty_list_renders_exact_segment(self):
        seg = status_segments(
            live_account(disabledModels=["glm-5.3", "kimi-k2.6"]), TZ, HUB_NOW
        )
        assert seg == "已禁用模型：glm-5.3、kimi-k2.6"

    async def test_empty_absent_or_non_list_renders_nothing(self):
        for value in ([], None):
            kw = {} if value is None else {"disabledModels": value}
            assert status_segments(live_account(**kw), TZ, HUB_NOW) == ""
        # live_account() never sets disabledModels → absent field
        assert status_segments(live_account(), TZ, HUB_NOW) == ""
        assert status_segments(live_account(disabledModels="glm-5.3"), TZ, HUB_NOW) == ""

    async def test_junk_entries_skipped(self):
        seg = status_segments(
            live_account(disabledModels=["", 42, None, True, "glm-5.3"]), TZ, HUB_NOW
        )
        assert seg == "已禁用模型：glm-5.3"
        # All junk → no segment at all.
        assert status_segments(
            live_account(disabledModels=["", 42, None]), TZ, HUB_NOW
        ) == ""

    async def test_case_preserved_exactly(self):
        seg = status_segments(
            live_account(disabledModels=["GLM-5.3", "Kimi-K2.6"]), TZ, HUB_NOW
        )
        assert seg == "已禁用模型：GLM-5.3、Kimi-K2.6"

    async def test_cap_at_six_ids_with_total(self):
        models = [f"m{i}" for i in range(8)]
        seg = status_segments(live_account(disabledModels=models), TZ, HUB_NOW)
        assert seg == "已禁用模型：m0、m1、m2、m3、m4、m5 等 8 个"

    async def test_exactly_six_ids_no_cap_suffix(self):
        models = [f"m{i}" for i in range(6)]
        seg = status_segments(live_account(disabledModels=models), TZ, HUB_NOW)
        assert seg == "已禁用模型：m0、m1、m2、m3、m4、m5"


class TestFailsEarlyWarning:
    """v1.0.5 — consecutive-failure early warning (fails >= 2, no blocker)."""

    async def test_fails_one_no_segment(self):
        assert status_segments(live_account(fails=1, failWindow=600), TZ, HUB_NOW) == ""

    async def test_fails_two_with_window(self):
        seg = status_segments(live_account(fails=2, failWindow=600), TZ, HUB_NOW)
        assert seg == "连续失败 2 次（600 秒窗口）"

    async def test_fails_missing_bool_nonint_no_segment(self):
        assert status_segments(live_account(failWindow=600), TZ, HUB_NOW) == ""
        assert status_segments(
            live_account(fails=True, failWindow=600), TZ, HUB_NOW
        ) == ""
        assert status_segments(
            live_account(fails="2", failWindow=600), TZ, HUB_NOW
        ) == ""
        assert status_segments(
            live_account(fails=2.5, failWindow=600), TZ, HUB_NOW
        ) == ""

    async def test_fail_window_missing_no_parenthetical(self):
        seg = status_segments(live_account(fails=3), TZ, HUB_NOW)
        assert seg == "连续失败 3 次"

    async def test_fail_window_invalid_no_parenthetical(self):
        for w in (0, -5, "600", True, 1.5):
            seg = status_segments(live_account(fails=2, failWindow=w), TZ, HUB_NOW)
            assert seg == "连续失败 2 次"

    async def test_suppressed_when_breaker_shown(self):
        seg = status_segments(
            live_account(fails=5, failWindow=600, breakerFor=BREAKER_SECONDS),
            TZ, HUB_NOW,
        )
        assert seg == f"熔断，10-17 16:19 恢复"

    async def test_suppressed_when_degrade_shown(self):
        seg = status_segments(
            live_account(fails=4, failWindow=600, degradeFor=120), TZ, HUB_NOW
        )
        assert seg == "降级，10-17 15:20 恢复"

    async def test_suppressed_when_cooldown_shown(self):
        seg = status_segments(
            live_account(fails=3, failWindow=600, cooldownFor=300), TZ, HUB_NOW
        )
        assert seg == "冷却，10-17 15:23 恢复"

    async def test_not_suppressed_by_unrelated_blockers(self):
        # Non-time blockers (停单/达限) do NOT suppress the failure warning;
        # order: existing 1-8 first, the failure segment last.
        seg = status_segments(
            live_account(fails=2, failWindow=600, reserveBlocked=True), TZ, HUB_NOW
        )
        assert seg == "保留积分停单；连续失败 2 次（600 秒窗口）"


class TestModelCooldownsStartedAtTolerance:
    """v1.7.7+ adds optional startedAt to modelCooldowns entries — must be
    tolerated without breaking parsing or changing the wording."""

    async def test_429_entry_with_started_at(self):
        epoch = 1760685780  # 10-17 15:23 北京时间
        seg = status_segments(
            live_account(modelCooldowns=[{
                "model": "deepseek-v4.1-flash",
                "expiresAt": epoch,
                "startedAt": epoch - 300,
            }]),
            TZ, HUB_NOW,
        )
        assert seg == "deepseek-v4.1-flash被429，10-17 15:23 恢复"

    async def test_capped_entry_with_started_at(self):
        epoch = 1760685780
        seg = status_segments(
            live_account(modelCooldowns=[{
                "model": "deepseek-v4.1-flash",
                "expiresAt": epoch,
                "cappedAt": epoch - 60,
                "startedAt": epoch - 300,
            }]),
            TZ, HUB_NOW,
        )
        assert seg == "deepseek-v4.1-flash额度用满，10-17 15:23 恢复"


class TestNewSegmentsOrdering:
    """Segments 9 and 10 come last; joined with ；like the others."""

    async def test_full_multi_segment_ordering(self):
        epoch = 1760685780  # 10-17 15:23
        seg = status_segments(
            live_account(
                enabled=False,
                reserveBlocked=True,
                modelCooldowns=[{"model": "glm-5.3", "expiresAt": epoch}],
                disabledModels=["glm-5.3", "kimi-k2.6"],
                fails=2,
                failWindow=600,
            ),
            TZ, HUB_NOW,
        )
        # No 熔断/降级/冷却 blocker present → the failure warning shows last.
        assert seg == (
            "已停用；glm-5.3被429，10-17 15:23 恢复；"
            "保留积分停单；已禁用模型：glm-5.3、kimi-k2.6；"
            "连续失败 2 次（600 秒窗口）"
        )

    async def test_full_ordering_with_cooldown_suppresses_failure(self):
        epoch = 1760685780
        seg = status_segments(
            live_account(
                enabled=False,
                cooldownFor=300,
                reserveBlocked=True,
                modelCooldowns=[{"model": "glm-5.3", "expiresAt": epoch}],
                disabledModels=["glm-5.3", "kimi-k2.6"],
                fails=2,
                failWindow=600,
            ),
            TZ, HUB_NOW,
        )
        # 冷却 already names the blocker → 连续失败 is redundant and suppressed.
        assert seg == (
            "已停用；冷却，10-17 15:23 恢复；glm-5.3被429，10-17 15:23 恢复；"
            "保留积分停单；已禁用模型：glm-5.3、kimi-k2.6"
        )

    async def test_both_new_segments_together(self):
        seg = status_segments(
            live_account(
                disabledModels=["glm-5.3"], fails=2, failWindow=600
            ),
            TZ, HUB_NOW,
        )
        assert seg == "已禁用模型：glm-5.3；连续失败 2 次（600 秒窗口）"

    async def test_backward_compatible_old_hub_payload(self):
        # An older Hub payload without any of the new fields renders exactly
        # as before (the pin).
        assert status_segments(live_account(), TZ, HUB_NOW) == ""
        epoch = 1760685780
        seg = status_segments(
            live_account(breakerFor=BREAKER_SECONDS,
                         modelCooldowns=[{"model": "glm-5.3", "expiresAt": epoch}]),
            TZ, HUB_NOW,
        )
        assert seg == (
            "熔断，10-17 16:19 恢复；glm-5.3被429，10-17 15:23 恢复"
        )


# ---------------------------------------------------------------------------
# v1.0.6 — v1.7.19 contract: restrictedFor / tokenExpired / needRelogin and
# the historical lastError fallback.
# ---------------------------------------------------------------------------

class TestRestrictedFor:
    async def test_renders_time_from_hub_clock(self):
        # 7200s after HUB_NOW → 10-17 17:18. Distinct from cooldownFor to pin
        # that each relative field reads its own key.
        seg = status_segments(
            live_account(restrictedFor=7200, cooldownFor=3660),
            TZ, HUB_NOW,
        )
        assert seg == "上游风控限制，10-17 17:18 恢复；冷却，10-17 16:19 恢复"

    @pytest.mark.parametrize("bad", [True, False, "7200", None])
    async def test_rejects_bool_none_str(self, bad):
        assert status_segments(live_account(restrictedFor=bad), TZ, HUB_NOW) == ""

    async def test_order_before_breaker(self):
        seg = status_segments(
            live_account(restrictedFor=60, breakerFor=3660),
            TZ, HUB_NOW,
        )
        assert seg.startswith("上游风控限制，10-17 15:19 恢复；熔断，10-17 16:19 恢复")
        assert seg.index("上游风控限制") < seg.index("熔断")


class TestCredentialExpired:
    async def test_token_expired_first(self):
        seg = status_segments(
            live_account(tokenExpired=True, breakerFor=3660),
            TZ, HUB_NOW,
        )
        assert seg.startswith("凭证过期，需重新登录；")
        assert seg == "凭证过期，需重新登录；熔断，10-17 16:19 恢复"

    async def test_need_relogin_same_single_segment(self):
        assert status_segments(live_account(needRelogin=True), TZ, HUB_NOW) == (
            "凭证过期，需重新登录"
        )

    async def test_both_true_one_segment(self):
        seg = status_segments(
            live_account(tokenExpired=True, needRelogin=True),
            TZ, HUB_NOW,
        )
        assert seg == "凭证过期，需重新登录"
        assert seg.count("凭证过期") == 1

    async def test_disabled_before_restriction(self):
        seg = status_segments(
            live_account(tokenExpired=True, enabled=False, restrictedFor=60),
            TZ, HUB_NOW,
        )
        assert seg == "凭证过期，需重新登录；已停用；上游风控限制，10-17 15:19 恢复"


STALE_429 = (
    'HTTP 429: {"code":14003,"msg":"too many requests",'
    '"requestId":"abc123","url":"https://api.example.com/v1/chat"}'
)


class TestHistoricalErrorFallback:
    async def test_429_with_code(self):
        seg = status_segments(live_account(lastError=STALE_429), TZ, HUB_NOW)
        assert seg == "近期429（code 14003，当前未限制）"

    async def test_429_without_code(self):
        seg = status_segments(live_account(lastError="HTTP 429: too many requests"), TZ, HUB_NOW)
        assert seg == "近期429（当前未限制）"

    async def test_non_429_status(self):
        seg = status_segments(live_account(lastError='HTTP 502: {"msg":"bad gateway"}'), TZ, HUB_NOW)
        assert seg == "最近错误 502（当前未限制）"

    async def test_junk_renders_nothing(self):
        for junk in ["boom", "some upstream error text", 'no status {"code":14003}']:
            assert status_segments(live_account(lastError=junk), TZ, HUB_NOW) == ""

    async def test_raw_body_never_appears(self):
        for err in [STALE_429, "HTTP 502: " + STALE_429]:
            out = status_segments(live_account(lastError=err), TZ, HUB_NOW)
            assert "requestId" not in out
            assert "abc123" not in out
            assert "too many requests" not in out
            assert "api.example.com" not in out

    async def test_segment_capped_defensively(self):
        out = status_segments(
            live_account(lastError="HTTP 429: " + "x" * 500), TZ, HUB_NOW
        )
        assert len(out) <= 80

    @pytest.mark.parametrize("override", [
        {"cooldownFor": 60},
        {"breakerFor": 60},
        {"degradeFor": 60},
        {"restrictedFor": 60},
        {"modelCooldowns": [{"model": "glm-5.3", "expiresAt": 1760685780}]},
        {"disabledModels": ["glm-5.3"]},
        {"fails": 2},
        {"enabled": False},
        {"tokenExpired": True},
        {"reserveBlocked": True},
        {"dailyLimitBlocked": True},
        {"creditLimitReached": True},
    ])
    async def test_fallback_suppressed_by_any_other_segment(self, override):
        assert status_segments(live_account(lastError=STALE_429, **override), TZ, HUB_NOW) != ""

    async def test_backward_compatible_old_payload(self):
        # A payload with none of the new fields renders exactly as before.
        seg = status_segments(
            live_account(breakerFor=BREAKER_SECONDS,
                         modelCooldowns=[{"model": "glm-5.3", "expiresAt": 1760685780}]),
            TZ, HUB_NOW,
        )
        assert seg == "熔断，10-17 16:19 恢复；glm-5.3被429，10-17 15:23 恢复"
