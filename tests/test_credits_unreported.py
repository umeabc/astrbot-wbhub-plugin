"""v1.3.9 — CodeArts reports tokens, not credits.

Its usage rows all carry ``credit: 0.0`` and no ``credits`` object (verified
against a live 2.0.6 Hub), so every 积分 figure derived from them is a
fabricated zero: "/wb hw" used to print "已消耗积分：0" and
"1,999,274 Token / 0 积分". Those views now print tokens only, while the
BALANCE lines stay — the CodeArts roster does report ``credits.remain``.
"""
from __future__ import annotations

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


class TestRenderWithoutCredits:
    def _report(self, *, channel: str, credits: int, tokens: int = 1000,
                reported: bool = False):
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
            credits_reported=reported,
        )

    def test_header_omits_the_credits_line(self):
        text = format_report(self._report(channel="hw", credits=0))
        assert "已消耗 Token：1,000" in text
        assert "已消耗积分" not in text

    def test_account_line_is_tokens_only_but_keeps_the_balance(self):
        text = format_report(self._report(channel="hw", credits=0))
        line = next(l for l in text.splitlines() if l.startswith("acc-1"))
        assert line == "acc-1 [hw]：已消耗 1,000 Token，剩余 12 积分"

    def test_model_line_is_tokens_only(self):
        text = format_report(self._report(channel="hw", credits=0))
        assert "m-1：1,000 Token" in text
        assert "m-1：1,000 Token / 0 积分" not in text

    def test_unattributed_line_drops_the_credit_part(self):
        report = self._report(channel="hw", credits=0)
        report = report.__class__(
            **{**report.__dict__, "excluded_tokens": 25, "excluded_credits": 0}
        )
        text = format_report(report)
        assert "未归属消耗：25 Token" in text
        assert "未归属消耗：0 积分" not in text

    def test_credit_channels_are_untouched(self):
        text = format_report(self._report(channel="cn", credits=3, reported=True))
        assert "已消耗积分：3" in text
        assert "acc-1 [cn]：已消耗 3 积分，剩余 12 积分" in text
        assert "m-1：1,000 Token / 3 积分" in text


class TestLiveCodeArtsView:
    async def test_hw_command_has_no_zero_credit_noise(self, credits_hub, credits_session):
        plugin = channel_plugin(credits_hub, credits_session)
        text = text_of(await run(plugin, "/wb hw"))
        assert "已消耗积分" not in text
        assert "/ 0 积分" not in text
        assert "已消耗 3,333 Token" in text
        assert "可用积分：12" in text

    async def test_cn_command_still_reports_credits(self, credits_hub, credits_session):
        plugin = channel_plugin(credits_hub, credits_session)
        text = text_of(await run(plugin, "/wb cn"))
        assert "已消耗积分：1" in text
        assert "已消耗 1 积分，剩余 12 积分" in text
