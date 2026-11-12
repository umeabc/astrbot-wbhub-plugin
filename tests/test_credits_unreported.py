"""v1.3.12: CodeArts renders both recorded credits and legacy zero values."""
from __future__ import annotations

from decimal import Decimal

import pytest

from astrbot_plugin_workbuddy_usage.reporting import aggregate, format_report
from conftest import make_account, make_analytics, make_model, make_stat
from test_channels import ChannelHub, channel_plugin  # noqa: F401 - fixtures
from test_ranges import TZ, run, text_of


@pytest.fixture()
async def credits_hub():
    hub = ChannelHub()
    await hub.start()
    yield hub
    await hub.stop()


@pytest.fixture()
async def credits_session():
    import aiohttp
    async with aiohttp.ClientSession() as session:
        yield session


def _payload(realm: str, *, tokens: int, credit: int):
    return make_analytics(
        [make_account(f"{realm}-account", "user", realm,
                      make_stat(total_tokens=tokens, credit=credit), {},
                      remain=12 if realm != "codearts" else None)],
        [make_model("shared-model", make_stat(total_tokens=tokens - 1, credit=credit))],
        make_stat(total_tokens=tokens, credit=credit),
    )


class TestRenderWithCredits:
    def _report(self, *, channel: str, credits: int, tokens: int = 1000):
        return aggregate(
            make_analytics(
                [make_account("acc-1", "n", channel,
                              make_stat(total_tokens=tokens, credit=credits), {},
                              remain=12)],
                [make_model("m-1", make_stat(total_tokens=tokens, credit=credits))],
                make_stat(total_tokens=tokens, credit=credits),
            ),
            __import__("datetime").date(2026, 11, 9), TZ,
            __import__("datetime").datetime.now(),
            channel=channel,
        )

    def test_header_includes_the_credits_line(self):
        text = format_report(self._report(channel="hw", credits=0))
        assert "已消耗 Token：1,000" in text
        assert "已消耗积分：0" in text

    def test_account_line_has_credits_and_balance(self):
        text = format_report(self._report(channel="hw", credits=0))
        line = next(l for l in text.splitlines() if l.startswith("acc-1"))
        assert line == "acc-1 [hw]：已消耗 1,000 Token / 0 积分，剩余 12 积分"

    def test_model_line_has_credits(self):
        text = format_report(self._report(channel="hw", credits=0))
        assert "m-1：1,000 Token" in text
        assert "m-1：1,000 Token / 0 积分" in text

    def test_unattributed_line_keeps_the_credit_part(self):
        report = self._report(channel="hw", credits=0)
        report = report.__class__(
            **{**report.__dict__, "excluded_tokens": 25, "excluded_credits": Decimal("0")}
        )
        text = format_report(report)
        assert "未归属消耗：0 积分 / 25 Token" in text
        assert "未归属消耗：0 积分" in text

    def test_credit_channels_are_untouched(self):
        text = format_report(self._report(channel="cn", credits=3))
        assert "已消耗积分：3" in text
        assert "acc-1 [cn]：已消耗 3 积分，剩余 12 积分" in text
        assert "m-1：1,000 Token / 3 积分" in text


class TestLiveCodeArtsView:
    async def test_hw_command_reports_recorded_credits(self, credits_hub, credits_session):
        plugin = channel_plugin(credits_hub, credits_session)
        text = text_of(await run(plugin, "/wb hw"))
        assert "已消耗积分：1" in text
        assert "shared-model：3,267 Token / 1 积分" in text
        assert "已消耗 3,333 Token / 1 积分，剩余 12 积分" in text
        assert "可用积分：12" in text

    async def test_cn_command_still_reports_credits(self, credits_hub, credits_session):
        plugin = channel_plugin(credits_hub, credits_session)
        text = text_of(await run(plugin, "/wb cn"))
        assert "已消耗积分：1" in text
        assert "已消耗 1 积分，剩余 12 积分" in text


@pytest.mark.parametrize("credit", (0, 7))
@pytest.mark.parametrize("window", ("", "0", "1", "7"))
async def test_hw_credit_values_across_windows(credits_hub, credits_session, credit, window):
    credits_hub.payloads["codearts"] = _payload("codearts", tokens=1000, credit=credit)
    plugin = channel_plugin(credits_hub, credits_session)
    text = text_of(await run(plugin, f"/wb hw {window}".strip()))
    assert f"已消耗积分：{credit}" in text
    assert f"已消耗 1,000 Token / {credit} 积分" in text
    assert f"shared-model：999 Token / {credit} 积分" in text
    assert ("剩余 12 积分" in text) is (window != "7")
    assert ("可用积分：12" in text) is (window != "7")


@pytest.mark.parametrize("credit", (0, 7))
async def test_all_stays_token_only_with_codearts_credits(credits_hub, credits_session, credit):
    credits_hub.payloads["all"] = _payload("codearts", tokens=1000, credit=credit)
    plugin = channel_plugin(credits_hub, credits_session)
    text = text_of(await run(plugin, "/wb all"))
    assert "已消耗 Token：1,000" in text
    assert "shared-model：999 Token" in text
    assert "积分" not in text
    assert "账号" not in text
