"""Pure-logic tests for aggregation, formatting and pagination (offline)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_workbuddy_usage.models import DailyReport
from astrbot_plugin_workbuddy_usage.reporting import (
    aggregate,
    day_bounds,
    format_credit,
    format_report,
    paginate_report,
)
from conftest import (
    make_analytics,
    make_account,
    make_distribution,
    make_model,
    make_stat,
)

TZ = "Asia/Shanghai"


# ---------------------------------------------------------------------------
# day_bounds
# ---------------------------------------------------------------------------


class TestDayBounds:
    def test_shanghai_midnight(self):
        now = datetime(2024, 3, 15, 10, 30, 0, tzinfo=ZoneInfo(TZ))
        since, until = day_bounds(now, TZ)
        midnight = datetime(2024, 3, 15, 0, 0, 0, tzinfo=ZoneInfo(TZ))
        assert since == midnight.timestamp()
        assert until == now.timestamp()
        assert until > since

    def test_naive_now_treated_as_local(self):
        now = datetime(2024, 3, 15, 10, 30, 0)
        since, _ = day_bounds(now, TZ)
        midnight = datetime(2024, 3, 15, 0, 0, 0, tzinfo=ZoneInfo(TZ))
        assert since == midnight.timestamp()

    def test_month_rollover(self):
        now = datetime(2024, 3, 1, 0, 0, 30, tzinfo=ZoneInfo(TZ))
        since, _ = day_bounds(now, TZ)
        assert datetime.fromtimestamp(since, ZoneInfo(TZ)).day == 1

    def test_year_rollover(self):
        now = datetime(2025, 1, 1, 0, 0, 1, tzinfo=ZoneInfo(TZ))
        since, _ = day_bounds(now, TZ)
        local = datetime.fromtimestamp(since, ZoneInfo(TZ))
        assert (local.year, local.month, local.day) == (2025, 1, 1)

    def test_dst_zone_boundary(self):
        # Lord Howe Island has a 30-minute DST shift; ZoneInfo handles it.
        tz = "Australia/Lord_Howe"
        now = datetime(2024, 10, 6, 3, 0, 0, tzinfo=ZoneInfo(tz))  # DST start day
        since, _ = day_bounds(now, tz)
        local = datetime.fromtimestamp(since, ZoneInfo(tz))
        assert (local.hour, local.minute) in ((0, 0), (0, 30))

    def test_captured_before_midnight_completes_after(self):
        # The query bounds must be derived from the captured 'now', not the
        # completion time; aggregate() receives the captured day unchanged.
        now = datetime(2024, 3, 15, 23, 59, 59, tzinfo=ZoneInfo(TZ))
        since, until = day_bounds(now, TZ)
        later = datetime(2024, 3, 16, 0, 0, 5, tzinfo=ZoneInfo(TZ))
        assert until <= now.timestamp()
        assert since < until < later.timestamp()


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


class TestAggregate:
    def _known_payload(self):
        a1 = make_account("u1", "账号一", "intl",
                          make_stat(requests=10, errors=1, total_tokens=1200, credit=2.5),
                          {"m-a": make_distribution(requests=8, tokens=1000)})
        a2 = make_account("u2", "账号二", "cn", make_stat(), {})  # zero traffic
        a3 = make_account("u3", "历史账号", "intl",
                          make_stat(requests=3, total_tokens=300, credit=0.75), {},
                          with_credits=False)  # historical / unattributed
        models = [
            # The billed error row (200 tokens / 0.25 credit inside u1's 1200)
            # never reaches the model fold (Hub skips errors), so model totals
            # (1300 tokens / 3.0 credits) are legitimately below summary.
            make_model("model-a", make_stat(requests=9, total_tokens=900, credit=1.75)),
            make_model("model-b", make_stat(requests=4, total_tokens=400, credit=1.25)),
        ]
        summary = make_stat(requests=17, errors=1, total_tokens=1500, credit=3.25)
        return make_analytics([a1, a2, a3], models, summary)

    def test_known_fixture_totals_and_rows(self):
        report = aggregate(self._known_payload(), date(2024, 3, 15), TZ,
                           datetime(2024, 3, 15, 12, 0, tzinfo=ZoneInfo(TZ)))
        assert report.total_tokens == 1500
        assert report.total_credits == Decimal("3.25")
        assert len(report.accounts) == 3  # zero and historical rows preserved
        assert len(report.models) == 2
        assert report.warnings == ()
        # zero account present with zero credits/tokens
        a2 = next(a for a in report.accounts if a.uid == "u2")
        assert a2.credits == 0 and a2.tokens == 0
        # historical account listed, not dropped
        a3 = next(a for a in report.accounts if a.uid == "u3")
        assert a3.credits == Decimal("0.75")

    def test_billed_error_rows_keep_model_totals_below_summary(self):
        """Regression pin: with a billed-failed contribution, the model axis
        legitimately totals LESS than the summary because the Hub's per-model
        fold skips error rows (_bump_analytics_models). This must NOT warn."""
        report = aggregate(self._known_payload(), date(2024, 3, 15), TZ, datetime.now())
        model_tokens = sum(m.tokens for m in report.models)
        model_credits = sum((m.credits for m in report.models), Decimal("0"))
        assert model_tokens < report.total_tokens  # 1300 < 1500: error-row gap
        assert model_credits < report.total_credits  # 3.0 < 3.25
        assert report.warnings == ()  # and crucially: no warning is emitted

    def test_sort_credits_desc_then_uid(self):
        report = aggregate(self._known_payload(), date(2024, 3, 15), TZ, datetime.now())
        credits = [a.credits for a in report.accounts]
        assert credits == sorted(credits, reverse=True)
        # model-b (1.25) after model-a (2.0)
        assert [m.model for m in report.models] == ["model-a", "model-b"]

    def test_reconcile_warning_on_token_mismatch(self):
        payload = self._known_payload()
        payload["summary"]["window"]["total_tokens"] = 9999
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert any("Token" in w for w in report.warnings)
        # authoritative totals are NOT altered by the mismatch
        assert report.total_tokens == 9999

    def test_reconcile_warning_on_credit_mismatch(self):
        payload = self._known_payload()
        payload["summary"]["window"]["credit"] = 9.9
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert any("积分" in w for w in report.warnings)

    def test_no_invented_account_model_credits(self):
        # window_models distributions carry no credit; the report must not
        # fabricate an account×model credit matrix.
        payload = self._known_payload()
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert not hasattr(report, "account_models")
        assert all(not hasattr(a, "models") for a in report.accounts)

    def test_zero_day(self):
        payload = make_analytics(
            [make_account("u1", "a", "intl", make_stat(), {})],
            [make_model("m", make_stat())],
            make_stat(),
        )
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert report.total_tokens == 0
        assert report.total_credits == Decimal("0")
        assert report.accounts and report.models == ()

    def test_unknown_model_id_case_preserved(self):
        payload = make_analytics(
            [],
            [make_model("Gemini-2.5-PRO-Preview", make_stat(requests=1, total_tokens=10, credit=0.1))],
            make_stat(requests=1, total_tokens=10, credit=0.1),
        )
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert report.models[0].model == "Gemini-2.5-PRO-Preview"

    def test_hundreds_of_accounts_all_preserved(self):
        accounts = [
            make_account(f"u{i}", f"账号{i}", "intl",
                         make_stat(requests=1, total_tokens=10, credit=0.1), {})
            for i in range(300)
        ]
        summary = make_stat(requests=300, total_tokens=3000, credit=30.0)
        payload = make_analytics(accounts, [], summary)
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert len(report.accounts) == 300
        assert report.warnings == ()


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


class TestFormat:
    def test_format_credit_six_dp_half_up_trim(self):
        assert format_credit(Decimal("2.5")) == "2.5"
        assert format_credit(Decimal("0.0000005")) == "0.000001"  # HALF_UP
        assert format_credit(Decimal("1.0000000")) == "1"
        assert format_credit(Decimal("0")) == "0"
        assert format_credit(Decimal("0.1234564999")) == "0.123456"

    def test_full_template(self):
        models_mod = __import__("astrbot_plugin_workbuddy_usage.models", fromlist=["AccountUsage"])
        report = DailyReport(
            day=date(2024, 3, 15), timezone=TZ,
            as_of=datetime(2024, 3, 15, 18, 30, 5, tzinfo=ZoneInfo(TZ)),
            total_tokens=1234567, total_credits=Decimal("12.5"),
            accounts=(models_mod.AccountUsage(
                uid="u1", name="账号一", realm="intl",
                credits=Decimal("2.5"), tokens=1200, current=True,
                remaining=Decimal("497.5")),),
            models=(models_mod.ModelUsage(
                model="model-a", credits=Decimal("2.0"), tokens=1100),),
            warnings=("示例警告。",),
        )
        text = format_report(report)
        assert text.splitlines()[:6] == [
            "WorkBuddy 当日用量",
            "2024-03-15",
            "当日已消耗 Token：1,234,567",
            "当日已消耗积分：12.5",
            "累计消耗 Token：—",
            "账号（1）：",
        ]
        assert "u1 [intl]：已消耗 2.5 积分，剩余 497.5 积分" in text
        assert "使用模型统计：" in text
        assert "model-a：2 积分" in text
        assert "警告：示例警告。" in text

    def test_deleted_lines_absent(self):
        models_mod = __import__("astrbot_plugin_workbuddy_usage.models", fromlist=["AccountUsage"])
        report = DailyReport(
            day=date(2024, 3, 15), timezone=TZ,
            as_of=datetime(2024, 3, 15, 18, 30, 5, tzinfo=ZoneInfo(TZ)),
            total_tokens=1, total_credits=Decimal("1"),
            accounts=(models_mod.AccountUsage(
                uid="u1", name="账号一", realm="intl",
                credits=Decimal("1"), tokens=1, current=True),),
            models=(models_mod.ModelUsage(
                model="model-a", credits=Decimal("1"), tokens=1),),
        )
        text = format_report(report)
        assert "口径" not in text
        assert "注：" not in text
        assert "日期：" not in text
        assert "截至：" not in text
        assert "Asia/Shanghai" not in text
        assert "总 Token：" not in text
        assert "总积分：" not in text
        # title preserved, date line is bare ISO
        assert text.splitlines()[0] == "WorkBuddy 当日用量"
        assert text.splitlines()[1] == "2024-03-15"

    def test_uid_first_eight_chars_nickname_absent_realm_shown(self):
        models_mod = __import__("astrbot_plugin_workbuddy_usage.models", fromlist=["AccountUsage"])
        report = DailyReport(
            day=date(2024, 3, 15), timezone=TZ, as_of=datetime.now(),
            total_tokens=0, total_credits=Decimal("0"),
            accounts=(models_mod.AccountUsage(
                uid="deadbeef-1111-2222-3333-444455556666", name="13800138000",
                realm="cn", credits=Decimal("308.9"), tokens=1, current=True,
                remaining=Decimal("2789.41")),),
        )
        text = format_report(report)
        assert "deadbeef [cn]：已消耗 308.9 积分，剩余 2789.41 积分" in text
        # masking: the phone-number nickname and the full uid never appear;
        # the short realm marker is expected output.
        assert "13800138000" not in text
        assert "6969-40c4-bf2a-62b10b01b5df" not in text
        assert "deadbeef [cn]" in text

    def test_realm_markers_rendered_for_cn_and_intl(self):
        models_mod = __import__("astrbot_plugin_workbuddy_usage.models", fromlist=["AccountUsage"])
        report = DailyReport(
            day=date(2024, 3, 15), timezone=TZ, as_of=datetime.now(),
            total_tokens=0, total_credits=Decimal("0"),
            accounts=(
                models_mod.AccountUsage(
                    uid="a2", name="n2", realm="cn",
                    credits=Decimal("19.8"), tokens=1, current=True,
                    remaining=Decimal("3658.25")),
                models_mod.AccountUsage(
                    uid="a1", name="n1", realm="intl",
                    credits=Decimal("0"), tokens=0, current=True,
                    remaining=Decimal("378.84")),
            ),
        )
        text = format_report(report)
        assert "a1 [intl]：已消耗 0 积分，剩余 378.84 积分" in text
        assert "a2 [cn]：已消耗 19.8 积分，剩余 3658.25 积分" in text

    def test_empty_realm_renders_no_empty_brackets(self):
        models_mod = __import__("astrbot_plugin_workbuddy_usage.models", fromlist=["AccountUsage"])
        report = DailyReport(
            day=date(2024, 3, 15), timezone=TZ, as_of=datetime.now(),
            total_tokens=0, total_credits=Decimal("0"),
            accounts=(models_mod.AccountUsage(
                uid="u9", name="n", realm="",
                credits=Decimal("1"), tokens=1, current=True,
                remaining=Decimal("2")),),
        )
        text = format_report(report)
        assert "u9：已消耗 1 积分，剩余 2 积分" in text
        assert "[]" not in text

    def test_remaining_unavailable_shows_dash(self):
        models_mod = __import__("astrbot_plugin_workbuddy_usage.models", fromlist=["AccountUsage"])
        report = DailyReport(
            day=date(2024, 3, 15), timezone=TZ, as_of=datetime.now(),
            total_tokens=0, total_credits=Decimal("0"),
            accounts=(models_mod.AccountUsage(
                uid="u1", name="n", realm="",
                credits=Decimal("0"), tokens=0, current=False,
                remaining=None),),
        )
        text = format_report(report)
        assert "u1：已消耗 0 积分，剩余 — 积分" in text
        assert "[]" not in text  # empty realm renders no brackets

    def test_unattributed_line_only_when_nonzero(self):
        models_mod = __import__("astrbot_plugin_workbuddy_usage.models", fromlist=["AccountUsage"])
        # zero excluded usage → no extra line
        base = DailyReport(
            day=date(2024, 3, 15), timezone=TZ, as_of=datetime.now(),
            total_tokens=0, total_credits=Decimal("0"),
            accounts=(models_mod.AccountUsage(
                uid="u1", name="n", realm="", credits=Decimal("0"),
                tokens=0, current=True),),
            excluded_credits=Decimal("0"), excluded_tokens=0,
        )
        assert "未归属消耗" not in format_report(base)
        # non-zero excluded usage → exactly one extra line after account list
        nonzero = DailyReport(
            day=date(2024, 3, 15), timezone=TZ, as_of=datetime.now(),
            total_tokens=25, total_credits=Decimal("0.5"),
            accounts=(models_mod.AccountUsage(
                uid="u1", name="n", realm="", credits=Decimal("0.5"),
                tokens=25, current=True),),
            excluded_credits=Decimal("0.5"), excluded_tokens=25,
        )
        lines = format_report(nonzero).splitlines()
        assert lines[lines.index("账号（1）：") + 2] == "未归属消耗：0.5 积分 / 25 Token"

    def test_empty_template(self):
        report = DailyReport(day=date(2024, 3, 15), timezone=TZ,
                             as_of=datetime(2024, 3, 15, 0, 0, 1, tzinfo=ZoneInfo(TZ)),
                             total_tokens=0, total_credits=Decimal("0"))
        text = format_report(report)
        assert "账号：无账号" in text
        assert "使用模型统计：今日暂无用量" in text


# ---------------------------------------------------------------------------
# placeholder exclusion (aggregate)
# ---------------------------------------------------------------------------


class TestPlaceholderExclusion:
    def _payload_with_placeholder(self, credit=0.0, tokens=0):
        real = make_account("deadbeef-1111-2222-3333-444455556666", "13800138000", "cn",
                            make_stat(total_tokens=10, credit=1.0), {},
                            remain=Decimal("2789.41"))
        ph = make_account("(unattributed)", "(unattributed)", "",
                          make_stat(total_tokens=tokens, credit=credit), {},
                          with_credits=False)
        summary = make_stat(total_tokens=10 + tokens, credit=1.0 + credit)
        return make_analytics([real, ph], [], summary)

    def test_placeholder_excluded_count_three_not_four(self):
        accounts = [
            make_account(f"uid-{i}-aaaaaaaa", f"n{i}", "cn", make_stat(), {})
            for i in range(3)
        ] + [
            make_account("(unattributed)", "(unattributed)", "", make_stat(), {}, with_credits=False),
            make_account("(unknown)", "(unknown)", "", make_stat(), {}, with_credits=False),
        ]
        payload = make_analytics(accounts, [], make_stat())
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert len(report.accounts) == 3
        assert all(a.uid not in ("(unattributed)", "(unknown)") for a in report.accounts)

    def test_placeholder_zero_prints_nothing_extra(self):
        report = aggregate(self._payload_with_placeholder(), date(2024, 3, 15), TZ, datetime.now())
        text = format_report(report)
        assert len(report.accounts) == 1
        assert "未归属消耗" not in text

    def test_placeholder_nonzero_reported(self):
        report = aggregate(self._payload_with_placeholder(credit=0.5, tokens=25),
                           date(2024, 3, 15), TZ, datetime.now())
        text = format_report(report)
        assert "未归属消耗：0.5 积分 / 25 Token" in text
        assert len(report.accounts) == 1

    def test_zero_consumption_real_account_kept(self):
        accounts = [
            make_account("u1", "n1", "cn", make_stat(), {}),
            make_account("u2", "n2", "cn", make_stat(), {}),
            make_account("(unattributed)", "(unattributed)", "", make_stat(), {}, with_credits=False),
        ]
        payload = make_analytics(accounts, [], make_stat())
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert len(report.accounts) == 2

    def test_remaining_parsed_from_credits_remain(self):
        payload = make_analytics(
            [make_account("u1", "n", "cn", make_stat(), {}, remain=Decimal("2789.41"))],
            [], make_stat(),
        )
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert report.accounts[0].remaining == Decimal("2789.41")

    def test_remaining_absent_when_no_credits_object(self):
        payload = make_analytics(
            [make_account("u1", "n", "cn", make_stat(), {}, with_credits=False)],
            [], make_stat(),
        )
        report = aggregate(payload, date(2024, 3, 15), TZ, datetime.now())
        assert report.accounts[0].remaining is None
        assert "剩余 — 积分" in format_report(report)

    def test_placeholder_still_reconciles_with_summary(self):
        # summary folds ALL rows (including placeholders) → no false warning
        report = aggregate(self._payload_with_placeholder(credit=0.5, tokens=25),
                           date(2024, 3, 15), TZ, datetime.now())
        assert report.warnings == ()


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def _long_report_text(self, n_accounts: int = 200, n_models: int = 100) -> str:
        lines = ["WorkBuddy 当日用量", "2024-03-15",
                 "当日已消耗 Token：1", "当日已消耗积分：0", f"账号（{n_accounts}）："]
        lines += [f"u{i}：已消耗 0 积分，剩余 500 积分" for i in range(n_accounts)]
        lines.append("使用模型统计：")
        lines += [f"model-{i}：0 积分" for i in range(n_models)]
        return "\n".join(lines)

    def test_no_loss_across_pages(self):
        text = self._long_report_text()
        pages = paginate_report(text, max_chars=1200, max_lines=30)
        assert len(pages) > 1
        rejoined = "\n".join(p.split("\n", 1)[1] for p in pages)
        assert rejoined == text  # every line preserved, in order

    def test_page_limits_respected(self):
        text = self._long_report_text()
        pages = paginate_report(text, max_chars=1200, max_lines=30)
        for page in pages:
            body = page.split("\n", 1)[1]
            assert len(body) <= 1200
            assert body.count("\n") + 1 <= 30
            assert page.startswith("[WorkBuddy ")

    def test_single_page_not_prefixed(self):
        text = "a\nb\nc"
        assert paginate_report(text, 100, 10) == [text]

    def test_oversized_single_line_hard_split(self):
        line = "x" * 5000
        pages = paginate_report(line, 1000, 10)
        rejoined = "".join(p.split("\n", 1)[1] if "\n" in p else p.split("\n", 1)[1]
                           for p in pages)
        assert "x" * 5000 == rejoined.replace("\n", "")

    def test_no_entry_dropped_hundreds(self):
        text = self._long_report_text(300, 200)
        pages = paginate_report(text, 1800, 35)
        rejoined = "\n".join(p.split("\n", 1)[1] for p in pages)
        for i in range(300):
            assert f"u{i}：已消耗 0 积分，剩余 500 积分" in rejoined
        for i in range(200):
            assert f"model-{i}：0 积分" in rejoined


# ---------------------------------------------------------------------------
# Cumulative (all-time) token consumption line
# ---------------------------------------------------------------------------


class TestAllTimeTokens:
    DAY = date(2024, 3, 15)
    NOW = datetime(2024, 3, 15, 12, 0, tzinfo=ZoneInfo(TZ))

    def _payload(self, all_time: dict | None, **summary_window_kwargs):
        payload = make_analytics(
            accounts=[make_account("u1", "账号一", "cn",
                                   make_stat(total_tokens=100, credit=1.0), {})],
            models=[make_model("model-a", make_stat(total_tokens=100, credit=1.0))],
            summary_window=make_stat(total_tokens=100, credit=1.0, **summary_window_kwargs),
        )
        if all_time is None:
            del payload["summary"]["all_time"]
        else:
            payload["summary"]["all_time"] = all_time
        return payload

    def _render(self, all_time: dict | None, **kwargs) -> str:
        report = aggregate(self._payload(all_time, **kwargs), self.DAY, TZ, self.NOW)
        return format_report(report)

    def test_all_time_line_present_and_positioned(self):
        text = self._render(make_stat(total_tokens=981527465, credit=100.0))
        lines = text.split("\n")
        idx_credits = lines.index("当日已消耗积分：1")
        idx_all_time = lines.index("累计消耗 Token：981,527,465")
        idx_accounts = next(i for i, l in enumerate(lines) if l.startswith("账号（"))
        assert idx_all_time == idx_credits + 1
        assert idx_accounts == idx_all_time + 1

    def test_all_time_value_is_comma_formatted(self):
        assert "累计消耗 Token：1,234,567" in self._render(make_stat(total_tokens=1234567, credit=1.0))

    def test_all_time_absent_renders_dash_not_zero(self):
        text = self._render(None)
        assert "累计消耗 Token：—" in text
        assert "累计消耗 Token：0" not in text

    def test_all_time_total_tokens_null_treated_as_unavailable(self):
        raw = make_stat(total_tokens=100, credit=1.0)
        raw["total_tokens"] = None
        assert "累计消耗 Token：—" in self._render(raw)

    def test_all_time_total_tokens_bool_rejected(self):
        raw = make_stat(total_tokens=100, credit=1.0)
        raw["total_tokens"] = True
        assert "累计消耗 Token：—" in self._render(raw)

    def test_all_time_total_tokens_nonintegral_rejected(self):
        raw = make_stat(total_tokens=100, credit=1.0)
        raw["total_tokens"] = 981527465.5
        assert "累计消耗 Token：—" in self._render(raw)

    def test_all_time_non_mapping_block_treated_as_unavailable(self):
        assert "累计消耗 Token：—" in self._render("not-a-mapping")
