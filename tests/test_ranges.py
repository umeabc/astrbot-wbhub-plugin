"""v1.3.3 — `/wb <arg>`: ranged reports and the help/error contract.

/wb            today (unchanged, byte-identical output)
/wb 3|7|30     rolling calendar days, today included
/wb week       this week (Monday 00:00)
/wb month      this month (1st 00:00)
/wb all        everything the Hub still has
/wb help       static help
anything else  参数错误 + hint, no Hub request

All offline (aiohttp stub server + pure helpers).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_workbuddy_usage.models import DailyReport
from conftest import make_account, make_analytics, make_model, make_stat

from astrbot_plugin_workbuddy_usage.reporting import (
    RANGE_ALL,
    RANGE_MONTH,
    RANGE_TODAY,
    RANGE_WEEK,
    BadRange,
    HelpRequested,
    format_report,
    parse_range_arg,
    range_bounds,
)

TZ = "Asia/Shanghai"
TZ_OBJ = ZoneInfo(TZ)
# Wednesday 2026-10-28 15:00 +08:00
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=TZ_OBJ)


def ts(d: date, hour: int = 0) -> float:
    return datetime(d.year, d.month, d.day, hour, tzinfo=TZ_OBJ).timestamp()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParseRangeArg:
    @pytest.mark.parametrize("token", ["", "  ", "today", "TODAY"])
    def test_default_and_today(self, token):
        assert parse_range_arg(token) is RANGE_TODAY

    @pytest.mark.parametrize("n", [3, 7, 30])
    def test_rolling_days(self, n):
        spec = parse_range_arg(str(n))
        assert spec.key == str(n)
        assert spec.label == f"近 {n} 天"
        assert not spec.is_default and not spec.is_all_time

    def test_zero_is_the_default_and_one_is_a_day_window(self):
        assert parse_range_arg("0") is RANGE_TODAY
        one = parse_range_arg("1")
        assert one.key == "1" and one.label == "近 1 天"
        assert one.is_detailed          # /wb 1 keeps the current-state block
        assert not parse_range_arg("3").is_detailed
        assert RANGE_TODAY.is_detailed

    def test_named_windows(self):
        assert parse_range_arg("week") is RANGE_WEEK
        assert parse_range_arg("Month") is RANGE_MONTH
        assert parse_range_arg("all") is RANGE_ALL
        assert parse_range_arg(" all ") is RANGE_ALL

    @pytest.mark.parametrize("token", ["help", "HELP", "?", "-h", "--help"])
    def test_help_tokens(self, token):
        with pytest.raises(HelpRequested):
            parse_range_arg(token)

    @pytest.mark.parametrize("token", ["2", "5", "31", "365", "abc", "weeks", "今天", "3d"])
    def test_everything_else_is_rejected(self, token):
        with pytest.raises(BadRange) as exc:
            parse_range_arg(token)
        assert exc.value.token == token.lower()


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------


class TestRangeBounds:
    def test_today_matches_day_bounds(self):
        since, until = range_bounds(RANGE_TODAY, NOW, TZ)
        assert since == ts(date(2026, 10, 28))
        assert until == NOW.timestamp()

    @pytest.mark.parametrize(
        "token, first_day",
        [
            ("1", date(2026, 10, 28)),   # today only
            ("3", date(2026, 10, 26)),   # today + 2 previous days
            ("7", date(2026, 10, 22)),
            ("30", date(2026, 9, 29)),
        ],
    )
    def test_rolling_windows_include_today(self, token, first_day):
        since, until = range_bounds(parse_range_arg(token), NOW, TZ)
        assert since == ts(first_day)
        assert until == NOW.timestamp()

    def test_week_starts_monday(self):
        since, _ = range_bounds(RANGE_WEEK, NOW, TZ)
        assert since == ts(date(2026, 10, 26))  # Monday
        # Sunday belongs to the week that started six days earlier.
        sunday = datetime(2026, 11, 1, 9, 0, tzinfo=TZ_OBJ)
        since_sun, _ = range_bounds(RANGE_WEEK, sunday, TZ)
        assert since_sun == ts(date(2026, 10, 26))

    def test_month_starts_on_the_first(self):
        since, _ = range_bounds(RANGE_MONTH, NOW, TZ)
        assert since == ts(date(2026, 10, 1))

    def test_all_has_no_lower_bound(self):
        since, until = range_bounds(RANGE_ALL, NOW, TZ)
        assert since == 0.0
        assert until == NOW.timestamp()

    def test_window_is_cut_in_the_configured_timezone(self):
        # 2026-10-28 00:30 +08 is 2026-10-27 16:30 UTC: "today" must still be
        # the 28th, and /wb 3 must reach back to the 26th local midnight.
        early = datetime(2026, 10, 28, 0, 30, tzinfo=TZ_OBJ)
        since, _ = range_bounds(parse_range_arg("3"), early, TZ)
        assert since == ts(date(2026, 10, 26))

    def test_naive_now_is_stamped_with_the_reporting_zone(self):
        since, until = range_bounds(RANGE_TODAY, datetime(2026, 10, 28, 15, 0), TZ)
        assert since == ts(date(2026, 10, 28))
        assert until == NOW.timestamp()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def make_report(**kw) -> DailyReport:
    base = dict(
        day=date(2026, 10, 28),
        timezone=TZ,
        as_of=NOW,
        total_tokens=1200,
        total_credits=Decimal("3.25"),
        total_tokens_all_time=987654,
    )
    base.update(kw)
    return DailyReport(**base)


class TestRangeRendering:
    def test_default_report_is_unchanged(self):
        text = format_report(make_report())
        assert text.splitlines()[:5] == [
            "WorkBuddy 当日用量",
            "2026-10-28",
            "当日已消耗 Token：1,200",
            "当日已消耗积分：3.25",
            "累计消耗 Token：987,654",
        ]

    def test_rolling_window_renders_range_and_labels(self):
        report = make_report(
            range_label="近 7 天",
            range_start=datetime(2026, 10, 22, tzinfo=TZ_OBJ),
            range_end=datetime(2026, 10, 28, 15, tzinfo=TZ_OBJ),
        )
        assert format_report(report).splitlines()[:5] == [
            "WorkBuddy 近 7 天用量",
            "2026-10-22 ~ 2026-10-28",
            "近 7 天已消耗 Token：1,200",
            "近 7 天已消耗积分：3.25",
            "累计消耗 Token：987,654",
        ]

    def test_empty_model_block_mentions_the_window(self):
        ranged = make_report(
            range_label="近 30 天",
            range_start=datetime(2026, 10, 3, tzinfo=TZ_OBJ),
            range_end=datetime(2026, 10, 28, 15, tzinfo=TZ_OBJ),
        )
        assert "使用模型统计：区间内暂无用量" in format_report(ranged)
        assert "使用模型统计：今日暂无用量" in format_report(make_report())

    def test_all_time_window_hides_the_duplicate_cumulative_line(self):
        report = make_report(
            range_label="全部时间",
            range_start=None,
            range_end=datetime(2026, 10, 28, 15, tzinfo=TZ_OBJ),
            detailed=False,
        )
        lines = format_report(report).splitlines()
        assert lines[0] == "WorkBuddy 全部时间用量"
        assert lines[1] == "有记录以来 ~ 2026-10-28"
        assert lines[2] == "全部时间已消耗 Token：1,200"
        assert not any(line.startswith("累计消耗 Token") for line in lines)


# ---------------------------------------------------------------------------
# Plugin-level command contract
# ---------------------------------------------------------------------------


class FakeEvent:
    def __init__(self, text: str = "/wb", sender: str = "10001") -> None:
        self.message_str = text
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


def wire(plugin, stub_hub, session):
    from astrbot_plugin_workbuddy_usage.client import WorkBuddyClient

    plugin._session = session
    plugin._client = WorkBuddyClient(
        base_url=stub_hub.base_url,
        panel_password="pw",
        panel_token="preexisting-token",
        timeout_seconds=5.0,
        max_response_bytes=5 * 1024 * 1024,
        session=session,
    )


async def run(plugin, text: str):
    return [m async for m in plugin.wb(FakeEvent(text))]


def text_of(results) -> str:
    return results[0].chain[0].text


def midnight_today() -> float:
    return datetime.now(TZ_OBJ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class TestCommandArgs:
    @pytest.mark.parametrize(
        "message, expected",
        [
            ("/wb", []),
            ("/wb 3", ["3"]),
            ("wb 3", ["3"]),
            ("3", ["3"]),               # adapter already stripped the command
            ("/wb\u30003", ["3"]),      # full-width space
            ("  /wb   week  ", ["week"]),
            ("/WB 7", ["7"]),
            ("@bot /wb 3", ["3"]),
            ("[CQ:at,qq=1] /wb month", ["month"]),
        ],
    )
    def test_argument_extraction(self, message, expected):
        plugin = make_plugin()
        assert plugin._wb_args(FakeEvent(message)) == expected

    async def test_help_needs_no_hub_request(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb help"))
        assert "/wb 3" in text and "/wb all" in text and "/wb help" in text
        assert stub_hub.analytics_calls == []
        assert stub_hub.accounts_calls == []

    async def test_unknown_argument_is_rejected_without_a_request(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 5"))
        assert text.startswith("参数错误：5")
        assert "/wb help" in text
        assert stub_hub.analytics_calls == []

    async def test_two_arguments_are_rejected(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 3 7"))
        assert text.startswith("参数错误")
        assert stub_hub.analytics_calls == []

    async def test_control_characters_are_not_echoed(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb bad\u0007token"))
        assert "\u0007" not in text
        assert "参数错误" in text

    async def test_help_does_not_consume_the_cooldown(self, stub_hub, session):
        plugin = make_plugin(cooldown_seconds=60)
        wire(plugin, stub_hub, session)
        await run(plugin, "/wb help")
        results = await run(plugin, "/wb")
        assert "WorkBuddy 当日用量" in text_of(results)
        assert len(stub_hub.analytics_calls) == 1

    async def test_default_command_still_reports_today(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb"))
        assert text.splitlines()[0] == "WorkBuddy 当日用量"
        call = stub_hub.analytics_calls[-1]
        assert int(call["since"]) == int(midnight_today())

    async def test_rolling_window_hits_the_hub_with_the_right_bounds(
        self, stub_hub, session
    ):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 7"))
        call = stub_hub.analytics_calls[-1]
        assert int(call["since"]) == int(midnight_today() - 6 * 86400)
        assert abs(int(call["until"]) - time.time()) < 120
        assert text.splitlines()[0] == "WorkBuddy 近 7 天用量"
        assert text.splitlines()[2].startswith("近 7 天已消耗 Token：")

    async def test_week_and_month_use_calendar_boundaries(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        await run(plugin, "/wb week")
        since_week = int(stub_hub.analytics_calls[-1]["since"])
        await run(plugin, "/wb month")
        since_month = int(stub_hub.analytics_calls[-1]["since"])

        today = datetime.now(TZ_OBJ).date()
        monday = today - timedelta(days=today.weekday())
        assert since_week == int(ts(monday))
        assert since_month == int(ts(today.replace(day=1)))

    async def test_all_time_has_no_lower_bound_and_no_cumulative_line(
        self, stub_hub, session
    ):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb all"))
        call = stub_hub.analytics_calls[-1]
        assert int(call["since"]) == 0
        assert text.splitlines()[0] == "WorkBuddy 全部时间用量"
        assert text.splitlines()[1].startswith("有记录以来 ~ ")
        assert not any(line.startswith("累计消耗 Token") for line in text.splitlines())

    async def test_range_report_keeps_accounts_and_models(self, stub_hub, session):
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 30"))
        # The roster/model blocks are window-independent: present on the stub
        # (which serves an empty payload) exactly as they are for today.
        assert "账号：" in text
        assert "使用模型统计：" in text


# ---------------------------------------------------------------------------
# v1.3.4: which views print current-state extras
# ---------------------------------------------------------------------------


def detailed_payload():
    """One account (with a balance) and one model, both with real usage."""
    acct = make_account(
        "aaaa1111", "n", "cn",
        make_stat(total_tokens=1200, credit=2.5), {}, remain=42.5,
    )
    model = make_model("model-x", make_stat(total_tokens=900, credit=1.75))
    return make_analytics([acct], [model], make_stat(total_tokens=1200, credit=2.5))


def live_accounts_body():
    """The same account plus a live per-model 429 (absolute expiry)."""
    return {"accounts": [{
        "uid": "aaaa1111",
        "nickname": "n",
        "realm": "cn",
        "credits": {"remain": 42.5},
        "cooldownFor": 300,
        "inCooldown": True,
        "modelCooldowns": [{"model": "model-x", "expiresAt": int(time.time()) + 600}],
    }]}


class TestDetailedViews:
    """ /wb, /wb 0 and /wb 1 show 累计 / 剩余 / 429; wider windows do not. """

    def setup_stub(self, stub_hub):
        stub_hub.payload = detailed_payload()
        stub_hub.accounts_body = live_accounts_body()

    async def test_default_view_has_the_current_state_block(self, stub_hub, session):
        self.setup_stub(stub_hub)
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb"))
        assert "累计消耗 Token：" in text
        assert "剩余 42.5 积分" in text
        assert "model-x被429" in text
        assert "冷却，" in text

    async def test_zero_is_identical_to_the_default(self, stub_hub, session):
        self.setup_stub(stub_hub)
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        zero = text_of(await run(plugin, "/wb 0"))
        default = text_of(await run(plugin, "/wb"))
        assert zero == default

    async def test_one_day_window_keeps_them(self, stub_hub, session):
        self.setup_stub(stub_hub)
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 1"))
        lines = text.splitlines()
        assert lines[0] == "WorkBuddy 近 1 天用量"
        assert lines[1] == datetime.now(TZ_OBJ).date().isoformat()
        assert "累计消耗 Token：" in text
        assert "剩余 42.5 积分" in text
        assert "model-x被429" in text

    async def test_wider_window_hides_all_three(self, stub_hub, session):
        self.setup_stub(stub_hub)
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        for arg in ("3", "7", "30", "week", "month", "all"):
            stub_hub.analytics_calls.clear()
            text = text_of(await run(plugin, f"/wb {arg}"))
            assert "累计消耗 Token" not in text, arg
            assert "剩余" not in text, arg
            assert "被429" not in text and "额度用满" not in text, arg
            assert "冷却，" not in text, arg
            assert "近" in text or "本周" in text or "本月" in text or "全部" in text

    async def test_structural_status_is_kept_in_wide_windows(self, stub_hub, session):
        # Expired credentials are a structural fact, not a timed recovery
        # state: wide windows still surface them.
        self.setup_stub(stub_hub)
        stub_hub.accounts_body = {"accounts": [{
            "uid": "aaaa1111", "nickname": "n", "realm": "cn",
            "tokenExpired": True,
        }]}
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 30"))
        assert "凭证过期" in text

    async def test_pool_balance_line_in_detailed_views_only(self, stub_hub, session):
        self.setup_stub(stub_hub)
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        for arg in ("", "0", "1"):
            text = text_of(await run(plugin, f"/wb {arg}".strip()))
            lines = text.splitlines()
            idx = next(i for i, l in enumerate(lines) if l.startswith("累计消耗 Token"))
            assert lines[idx + 1] == "可用积分：42.5", arg
        assert "可用积分" not in text_of(await run(plugin, "/wb 3"))

    async def test_model_line_reports_tokens_and_credits(self, stub_hub, session):
        self.setup_stub(stub_hub)
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 3"))
        assert "model-x：900 Token / 1.75 积分" in text

    async def test_deleted_account_rows_appear_with_marker(self, stub_hub, session):
        stub_hub.payload = make_analytics(
            [
                make_account("aaaa1111", "n", "cn",
                             make_stat(total_tokens=100, credit=3.0), {},
                             remain=10.0),
                make_account("dead0000", "gone", "cn",
                             make_stat(total_tokens=50, credit=1.0), {},
                             with_credits=False),
            ],
            [], make_stat(total_tokens=150, credit=4.0),
        )
        stub_hub.accounts_body = {"accounts": [{
            "uid": "aaaa1111", "nickname": "n", "realm": "cn",
            "credits": {"remain": 10.0},
        }]}
        plugin = make_plugin()
        wire(plugin, stub_hub, session)
        text = text_of(await run(plugin, "/wb 7"))
        assert "【已删除】" in text
        assert "已删除 1 个账号" not in text
        assert "dead0000" in text
