"""v1.3.8 channel commands: parsing, source-shaped HTTP data and rendering."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from aiohttp import web
import pytest
import pytest_asyncio

from astrbot_plugin_workbuddy_usage.reporting import (
    BadRange,
    HelpRequested,
    RANGE_ALL,
    RANGE_TODAY,
    format_report,
    parse_channel_args,
    parse_range_arg,
    range_bounds,
)
from conftest import StubHub, make_account, make_analytics, make_model, make_stat
from test_ranges import FakeEvent, TZ, make_plugin, run, text_of, wire


CHANNELS = ("cn", "int", "hw", "all")
RANGES = ("", "0", "1", "3", "7", "30", "week", "month", "all")
REALMS = {"cn": "cn", "int": "intl", "hw": "codearts", "all": "all"}
TITLES = {
    "cn": "WorkBuddy",
    "int": "WorkBuddy 国际版",
    "hw": "CodeArts",
    "all": "WorkBuddy 全渠道",
}


class TestChannelParsing:
    @pytest.mark.parametrize("channel", CHANNELS)
    @pytest.mark.parametrize("token", RANGES)
    def test_each_channel_accepts_every_range(self, channel, token):
        args = [channel] + ([token] if token else [])
        actual_channel, spec = parse_channel_args(args)
        assert actual_channel == channel
        assert spec == parse_range_arg(token)

    @pytest.mark.parametrize("token", RANGES[:-1])
    def test_bare_ranges_are_identical_to_cn(self, token):
        args = [token] if token else []
        assert parse_channel_args(args) == parse_channel_args(["cn", *args])

    def test_all_channel_is_distinct_from_all_time(self):
        assert parse_channel_args(["all"]) == ("all", RANGE_TODAY)
        assert parse_channel_args(["cn", "all"]) == ("cn", RANGE_ALL)
        assert parse_channel_args(["all", "all"]) == ("all", RANGE_ALL)

    @pytest.mark.parametrize("channel", CHANNELS)
    def test_channel_and_named_range_are_case_insensitive(self, channel):
        assert parse_channel_args([channel.upper(), "MONTH"]) == (
            channel, parse_range_arg("month")
        )

    @pytest.mark.parametrize(
        "args",
        (["xx", "7"], ["intl", "7"], ["codearts", "0"], ["3", "7"],
         ["cn", "2"], ["hw", "31"], ["all", "bad"], ["cn", "7", "0"]),
    )
    def test_invalid_channel_range_or_extra_argument_is_rejected(self, args):
        with pytest.raises(BadRange):
            parse_channel_args(args)

    @pytest.mark.parametrize("args", (["help"], ["HELP"], ["?"], ["--help"]))
    def test_help_keeps_the_existing_parser_contract(self, args):
        with pytest.raises(HelpRequested):
            parse_channel_args(args)


def channel_payload(realm: str):
    """Hub already folds summary/models for realm; do not reconstruct them.

    The model total is deliberately below the account total, as the upstream
    model fold omits billed errors. The cumulative summary is also independent
    of the account all_time rows, which are zero in this fixture.
    """
    values = {"cn": 1111, "intl": 2222, "codearts": 3333}
    realms = tuple(values) if realm == "all" else (realm,)
    accounts = [
        make_account(f"{key}-account", f"{key} user", key,
                     make_stat(total_tokens=values[key], credit=1), {}, remain=12)
        for key in realms
    ]
    total = sum(values[key] for key in realms)
    payload = make_analytics(
        accounts,
        [make_model("shared-model", make_stat(total_tokens=total - 66, credit=1))],
        make_stat(total_tokens=total, credit=len(realms)),
    )
    payload["realm"] = realm
    payload["summary"]["all_time"] = make_stat(total_tokens=99999)
    return payload


class ChannelHub(StubHub):
    """Realm-aware stub, including the independent CodeArts pool endpoint."""

    def __init__(self):
        super().__init__()
        self.when = datetime.now(timezone.utc).replace(microsecond=0)
        self.analytics_date = self.accounts_date = format_datetime(self.when, usegmt=True)
        self.payloads = {realm: channel_payload(realm) for realm in REALMS.values()}
        self.accounts_body = {"accounts": [
            {"uid": "cn-account", "nickname": "cn user", "realm": "cn",
             "credits": {"remain": 12}},
            {"uid": "intl-account", "nickname": "intl user", "realm": "intl",
             "credits": {"remain": 12}},
        ]}
        self.codearts_calls = []
        # The envelope identifies the provider; v1.10.9 account view rows
        # themselves carry no realm, product or provider fields.
        self.codearts_body = {"provider": "codearts", "accounts": [{
            "uid": "codearts-account", "nickname": "hw user",
            "disabled": False, "credits": {"remain": 12},
        }]}
        self.codearts_status = 200

    async def start(self):
        app = web.Application()
        app.router.add_post("/panel/login", self._login)
        app.router.add_get("/usage/analytics", self._analytics)
        app.router.add_get("/accounts", self._accounts)
        app.router.add_get("/codearts/accounts", self._codearts_accounts)
        app.router.add_get("/updates", self._updates)
        app.router.add_post("/updates/check", self._updates_check)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        host, port = self._runner.addresses[0][:2]
        self.base_url = f"http://{host}:{port}"

    async def _analytics(self, request):
        self.payload = self.payloads[request.query.get("realm", "all")]
        return await super()._analytics(request)

    async def _codearts_accounts(self, request):
        self.codearts_calls.append({"token": request.headers.get("X-Panel-Token")})
        if request.headers.get("X-Panel-Token") not in self.valid_tokens:
            return web.json_response({"ok": False}, status=401)
        return web.json_response(self.codearts_body, status=self.codearts_status,
                                 headers={"Date": self.accounts_date})


@pytest_asyncio.fixture
async def channel_hub():
    hub = ChannelHub()
    await hub.start()
    yield hub
    await hub.stop()


def channel_plugin(hub, session, **config):
    plugin = make_plugin(update_notice=False, **config)
    wire(plugin, hub, session)
    return plugin


class TestChannelRequests:
    @pytest.mark.parametrize("channel", CHANNELS)
    @pytest.mark.parametrize("token", RANGES)
    async def test_channel_range_selects_one_analytics_query_and_correct_bounds(
        self, channel_hub, session, channel, token
    ):
        plugin = channel_plugin(channel_hub, session)
        spec = parse_range_arg(token)
        report = await plugin._get_report(channel_hub.when, spec, channel=channel)
        assert len(channel_hub.analytics_calls) == 1
        call = channel_hub.analytics_calls[0]
        assert call["realm"] == REALMS[channel]
        since, until = range_bounds(spec, channel_hub.when, TZ)
        assert int(call["since"]) == int(since)
        assert int(call["until"]) == int(until)
        text = format_report(report)
        label = "当日" if spec.is_default else spec.label
        assert text.splitlines()[0] == f"{TITLES[channel]} {label}用量"
        assert ("累计消耗 Token：99,999" in text) is spec.is_detailed
        assert len(channel_hub.accounts_calls) == (0 if channel == "hw" else 1)
        assert len(channel_hub.codearts_calls) == (1 if channel == "hw" else 0)

    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_command_routes_to_the_selected_channel(self, channel_hub, session, channel):
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, f"/wb {channel} 7"))
        assert text.splitlines()[0] == f"{TITLES[channel]} 近 7 天用量"
        assert channel_hub.analytics_calls[-1]["realm"] == REALMS[channel]

    @pytest.mark.parametrize("token", RANGES[:-1])
    async def test_legacy_command_is_byte_identical_to_explicit_cn(
        self, channel_hub, session, token
    ):
        plugin = channel_plugin(channel_hub, session)
        implicit = await run(plugin, f"/wb {token}".strip())
        explicit = await run(plugin, f"/wb cn {token}".strip())
        assert [r.chain[0].text for r in implicit] == [r.chain[0].text for r in explicit]
        assert {call["realm"] for call in channel_hub.analytics_calls} == {"cn"}

    @pytest.mark.parametrize("channel, own, other", (
        ("cn", "cn-accou", "intl-acc"),
        ("int", "intl-acc", "cn-accou"),
    ))
    async def test_live_workbuddy_roster_does_not_leak_other_channels(
        self, channel_hub, session, channel, own, other
    ):
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, f"/wb {channel}"))
        assert "账号（1）：" in text
        assert own in text and other not in text
        assert "可用积分：12" in text
        assert "已删除" not in text

    async def test_hw_uses_independent_pool_with_missing_realm_and_inherits_balance(
        self, channel_hub, session
    ):
        # Analytics does not carry CodeArts live credit fields; its own pool
        # supplies balance and disabled state, including an idle account.
        channel_hub.payloads["codearts"]["accounts"][0].pop("credits")
        channel_hub.codearts_body["accounts"][0]["disabled"] = True
        channel_hub.codearts_body["accounts"].append({
            "uid": "idle-hw-account", "nickname": "idle",
            "disabled": False, "credits": {"remain": 8},
        })
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, "/wb hw 0"))
        assert "账号（2）：" in text
        assert "codearts [codearts]【已停用】" in text
        assert "已消耗 3,333 Token / 1 积分，剩余 12 积分" in text
        assert "idle-hw- [codearts]" in text
        assert "剩余 12 积分" in text and "剩余 8 积分" in text
        assert "可用积分：20" in text
        assert "cn-accou" not in text and "intl-acc" not in text
        assert "已删除" not in text
        assert channel_hub.accounts_calls == []
        assert len(channel_hub.codearts_calls) == 1

    @pytest.mark.parametrize("token", ("0", "7"))
    async def test_hw_keeps_deleted_token_only_usage_but_omits_empty_deleted_rows(
        self, channel_hub, session, token
    ):
        payload = channel_hub.payloads["codearts"]
        # Historical analytics rows may have an empty account-axis realm;
        # the Hub's usage.realm filter is the authority for their channel.
        payload["accounts"].extend([
            make_account("dead-hw-account", "deleted", "",
                         make_stat(total_tokens=321, credit=0), {}, with_credits=False),
            make_account("zero-hw-account", "empty deleted", "",
                         make_stat(), {}, with_credits=False),
        ])
        payload["summary"]["window"]["total_tokens"] += 321
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, f"/wb hw {token}"))
        assert "Token：3,654" in text
        assert "账号（2）：" in text
        deleted = next(line for line in text.splitlines() if line.startswith("dead-hw-"))
        assert deleted == "dead-hw-【已删除】：已消耗 321 Token / 0 积分"
        assert "剩余" not in deleted
        assert "zero-hw-" not in text

    @pytest.mark.parametrize("token", RANGES)
    async def test_hw_cooling_is_detailed_only_without_assuming_until_timezone(
        self, channel_hub, session, token
    ):
        channel_hub.codearts_body["accounts"][0].update({
            "cooling": True,
            "until": "2026-11-05 22:15:00",
        })
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, f"/wb hw {token}".strip()))
        assert ("【冷却】" in text) is parse_range_arg(token).is_detailed
        assert "恢复" not in text
        assert "22:15" not in text

    @pytest.mark.parametrize("token", RANGES)
    async def test_all_uses_the_hub_fold_and_exposes_only_tokens(
        self, channel_hub, session, token
    ):
        plugin = channel_plugin(channel_hub, session)
        report = await plugin._get_report(channel_hub.when, parse_range_arg(token), channel="all")
        text = format_report(report)
        assert "Token：6,666" in text
        assert "shared-model：6,600 Token" in text
        assert text.count("shared-model：") == 1
        assert "账号" not in text and "积分" not in text
        assert "剩余" not in text and "已删除" not in text
        assert len(channel_hub.analytics_calls) == 1
        assert channel_hub.analytics_calls[0]["realm"] == "all"
        # One inexpensive ordinary pool call preserves the existing Hub Date
        # clock correction. No per-channel analytics fan-out is introduced.
        assert len(channel_hub.accounts_calls) == 1
        assert channel_hub.codearts_calls == []

    async def test_all_does_not_depend_on_pool_endpoint_availability(self, channel_hub, session):
        channel_hub.accounts_status = channel_hub.codearts_status = 503
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, "/wb all"))
        assert "Token：6,666" in text
        assert "警告" not in text
        assert len(channel_hub.accounts_calls) == 1
        assert channel_hub.codearts_calls == []

    async def test_hw_pool_failure_keeps_analytics_and_does_not_mark_accounts_deleted(
        self, channel_hub, session
    ):
        channel_hub.codearts_status = 503
        channel_hub.payloads["codearts"]["accounts"][0].pop("credits")
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, "/wb hw 0"))
        assert text.startswith("CodeArts 当日用量")
        assert "Token：3,333" in text and "codearts [codearts]" in text
        assert "剩余 — 积分" in text and "已删除" not in text
        assert len(channel_hub.codearts_calls) == 1
        assert channel_hub.accounts_calls == []

    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_clock_correction_refetch_preserves_channel_and_range(
        self, channel_hub, session, channel
    ):
        channel_hub.accounts_status = channel_hub.codearts_status = 503
        plugin = channel_plugin(channel_hub, session)
        spec = parse_range_arg("7")
        report = await plugin._get_report(
            channel_hub.when - timedelta(days=2), spec, channel=channel
        )
        calls = channel_hub.analytics_calls
        assert len(calls) == 2
        assert {call["realm"] for call in calls} == {REALMS[channel]}
        since, until = range_bounds(spec, channel_hub.when, TZ)
        assert int(calls[1]["since"]) == int(since)
        assert int(calls[1]["until"]) == int(until)
        assert int(calls[0]["since"]) != int(calls[1]["since"])
        assert report.total_tokens == channel_hub.payloads[REALMS[channel]]["summary"]["window"]["total_tokens"]

    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_expired_token_login_replay_preserves_analytics_realm(
        self, channel_hub, session, channel
    ):
        channel_hub.password = "pw"
        channel_hub.valid_tokens.clear()
        plugin = channel_plugin(channel_hub, session)
        payload = await plugin._client.fetch_analytics(123.0, 456.0, realm=REALMS[channel])
        assert payload["realm"] == REALMS[channel]
        assert len(channel_hub.login_calls) == 1
        assert len(channel_hub.analytics_calls) == 2
        assert channel_hub.analytics_calls[0]["token"] == "preexisting-token"
        assert channel_hub.analytics_calls[1]["token"].startswith("tok-")
        assert all(
            call["realm"] == REALMS[channel]
            and call["since"] == "123" and call["until"] == "456"
            for call in channel_hub.analytics_calls
        )

    async def test_codearts_pool_login_replay_uses_its_own_endpoint(self, channel_hub, session):
        channel_hub.password = "pw"
        channel_hub.valid_tokens.clear()
        plugin = channel_plugin(channel_hub, session)
        payload = await plugin._client.fetch_codearts_accounts()
        assert payload["accounts"][0]["realm"] == "codearts"
        assert payload["accounts"][0]["enabled"] is True
        assert len(channel_hub.login_calls) == 1
        assert len(channel_hub.codearts_calls) == 2
        assert channel_hub.accounts_calls == []


class TestChannelCommandGuards:
    @pytest.mark.parametrize("command", (
        "/wb xx 7", "/wb intl 7", "/wb cn 2", "/wb hw 31",
        "/wb all bad", "/wb cn 7 0", "/wb bad\u0007channel 7",
    ))
    async def test_invalid_input_keeps_existing_error_and_makes_no_request(
        self, channel_hub, session, command
    ):
        plugin = channel_plugin(channel_hub, session)
        event = FakeEvent(command)
        text = text_of([message async for message in plugin.wb(event)])
        assert text.startswith("参数错误") and "/wb help" in text
        assert "\u0007" not in text
        assert event.stopped
        assert channel_hub.analytics_calls == channel_hub.accounts_calls == channel_hub.codearts_calls == []

    async def test_help_describes_channels_ranges_and_examples_without_network(
        self, channel_hub, session
    ):
        plugin = channel_plugin(channel_hub, session)
        text = text_of(await run(plugin, "/wb help"))
        for expected in ("渠道", "国内", "国际", "CodeArts", "全渠道", "/wb cn", "/wb int", "/wb hw", "/wb all"):
            assert expected in text
        assert "/wb cn 0" in text and "/wb cn all" in text
        assert channel_hub.analytics_calls == channel_hub.accounts_calls == channel_hub.codearts_calls == []

    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_channel_commands_keep_permission_gate(self, channel_hub, session, channel):
        plugin = channel_plugin(channel_hub, session, allow_admin=False)
        event = FakeEvent(f"/wb {channel} 7")
        text = text_of([message async for message in plugin.wb(event)])
        assert text.startswith("无权查询 WorkBuddy 用量")
        assert event.stopped
        assert channel_hub.analytics_calls == channel_hub.accounts_calls == channel_hub.codearts_calls == []

    async def test_channels_share_sender_cooldown_and_local_replies_do_not_consume_it(
        self, channel_hub, session
    ):
        plugin = channel_plugin(channel_hub, session, cooldown_seconds=60)
        await run(plugin, "/wb xx 7")
        await run(plugin, "/wb help")
        first = text_of(await run(plugin, "/wb cn 0"))
        second = text_of(await run(plugin, "/wb all 7"))
        from astrbot_plugin_workbuddy_usage.main import ERR_COOLDOWN
        assert first.startswith("WorkBuddy 当日用量")
        assert second == ERR_COOLDOWN
        assert len(channel_hub.analytics_calls) == 1

    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_channel_output_uses_existing_lossless_pagination(
        self, channel_hub, session, channel
    ):
        channel_hub.payloads[REALMS[channel]]["models"] = [
            make_model(f"model-{index:03d}", make_stat(total_tokens=1))
            for index in range(70)
        ]
        plugin = channel_plugin(channel_hub, session, max_message_chars=300, max_lines_per_message=5)
        results = await run(plugin, f"/wb {channel} 7")
        pages = [result.chain[0].text for result in results]
        assert len(pages) > 1
        bodies = []
        for index, page in enumerate(pages, 1):
            heading, body = page.split("\n", 1)
            assert heading == f"[WorkBuddy {index}/{len(pages)}]"
            assert len(body) <= 300 and len(body.splitlines()) <= 5
            bodies.append(body)
        rejoined = "\n".join(bodies)
        report = await plugin._get_report(channel_hub.when, parse_range_arg("7"), channel=channel)
        assert rejoined == format_report(report)
        for index in range(70):
            assert rejoined.count(f"model-{index:03d}：") == 1
