"""Reporting layer: timezone day bounds, aggregation, formatting, pagination.

Pure logic module — no I/O. All credit math uses Decimal; aggregation never
rounds intermediate values. Display rounding is HALF_UP, up to six decimals.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import replace as dataclasses_replace
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .models import AccountUsage, AccountsPayload, AnalyticsPayload, DailyReport, ModelUsage

logger = logging.getLogger("astrbot.workbuddy_usage")

# Tolerance when reconciling account/model sums against summary totals.
_RECONCILE_TOLERANCE = Decimal("0.000001")
_NAME_MAX_LEN = 80
# Hub-synthetic placeholder rows for requests that cannot be attributed to a
# real account. They carry no `credits` object and must be excluded from the
# account list (but still participate in the reconciliation against summary).
_PLACEHOLDER_UIDS = {"(unattributed)", "(unknown)"}


def _sanitize_name(name: object, fallback: str) -> str:
    """Strip control characters, normalize, cap length; keep uid as identifier."""
    if not isinstance(name, str) or not name.strip():
        return fallback
    cleaned = "".join(
        ch for ch in unicodedata.normalize("NFC", name) if not unicodedata.category(ch).startswith("C")
    )
    cleaned = cleaned.strip() or fallback
    if len(cleaned) > _NAME_MAX_LEN:
        cleaned = cleaned[:_NAME_MAX_LEN]
    return cleaned


def day_bounds(now: datetime, timezone: str) -> tuple[float, float]:
    """Return (since, until) epoch seconds covering the local day of ``now``.

    ``now`` may be timezone-naive (treated as the target zone) or aware.
    A tz-aware ``now`` is used verbatim; a naive one is stamped with
    ``timezone``. Callers should pass an aware instant taken from the Hub's
    clock (see day_bounds_from_hub) whenever available.
    """
    tz = ZoneInfo(timezone)
    if now.tzinfo is None:
        local = now.replace(tzinfo=tz)
    else:
        local = now.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # until = command-receipt now (inclusive upper bound)
    return midnight.timestamp(), local.timestamp()


def day_bounds_from_hub(hub_now_utc: datetime, timezone: str) -> tuple[float, float]:
    """Day bounds computed from the Hub's clock (tz-aware UTC instant).

    The Hub's instant is converted into the configured IANA timezone and the
    local calendar-day [midnight, now] window is derived from it. Passing a
    naive datetime raises ValueError — the Hub's clock is always aware; the
    caller must fall back to the local-clock path instead.
    """
    if hub_now_utc.tzinfo is None:
        raise ValueError("hub_now_utc must be timezone-aware")
    return day_bounds(hub_now_utc, timezone)


def aggregate(
    payload: AnalyticsPayload,
    day: date,
    timezone: str,
    as_of: datetime,
    accounts_payload: AccountsPayload | None = None,
    hub_now: datetime | None = None,
) -> DailyReport:
    """Build a DailyReport from a validated analytics payload.

    - summary.window is authoritative for totals;
    - real account rows are preserved (including zero-traffic rows) so sums
      reconcile; Hub-synthetic placeholder rows (uid `(unattributed)` /
      `(unknown)`) are EXCLUDED from the account list but their usage still
      participates in the reconciliation against summary.window;
    - per-model credits come ONLY from models[].window (whole-instance);
    - accounts_payload (GET /accounts?realm=all) optionally extends the
      roster (accounts missing from analytics, consumed 0) and supplies the
      per-account STATUS text; hub_now is the Hub instant of that response
      and the base for relative-seconds recovery times. Either may be None
      (Hub-status unavailable) — the report still renders, without status.
    """
    warnings: list[str] = []
    summary_window = payload.get("summary", {}).get("window", {})
    total_tokens = int(summary_window.get("total_tokens", 0))
    total_credits = Decimal(str(summary_window.get("credit", "0")))

    # Cumulative (all-time) tokens from summary.all_time.total_tokens, when the
    # Hub provides the all_time block. Strict integer parsing (reject bool/None
    # and non-integral values); absent/unusable → None, rendered as "—" later.
    total_tokens_all_time: int | None = None
    summary = payload.get("summary", {})
    all_time = summary.get("all_time")
    if isinstance(all_time, Mapping):
        raw_all_time_tokens = all_time.get("total_tokens")
        if raw_all_time_tokens is not None and not isinstance(raw_all_time_tokens, bool):
            try:
                d = Decimal(str(raw_all_time_tokens))
                if d.is_finite() and d == d.to_integral_value():
                    total_tokens_all_time = int(d)
            except (ArithmeticError, ValueError, TypeError):
                total_tokens_all_time = None

    accounts: list[AccountUsage] = []
    acct_token_sum = 0
    acct_credit_sum = Decimal("0")
    excluded_token_sum = 0
    excluded_credit_sum = Decimal("0")
    # Per-account STATUS text from /accounts?realm=all, keyed by uid. One odd
    # account must never poison the rest: status_segments is defensive, but
    # wrapped anyway so a failure only drops that account's status.
    status_map: dict[str, str] = {}
    if accounts_payload is not None:
        for entry in accounts_payload.get("accounts", ()):
            uid = entry.get("uid")
            if not isinstance(uid, str) or not uid:
                continue
            try:
                status_map[uid] = status_segments(entry, timezone, hub_now)
            except Exception:  # noqa: BLE001 - status is best-effort
                logger.warning("workbuddy_usage: status rendering failed for %s", uid)

    for idx, acct in enumerate(payload.get("accounts", ())):
        window = acct.get("window", {})
        credits = Decimal(str(window.get("credit", "0")))
        tokens = int(window.get("total_tokens", 0))
        has_credits = "credits" in acct

        # Remaining credit balance from credits.remain. This is the account's
        # balance, NOT today's consumption (window.credit). Missing/invalid → None.
        remaining: Decimal | None = None
        credits_obj = acct.get("credits")
        if isinstance(credits_obj, Mapping):
            raw_remain = credits_obj.get("remain")
            if raw_remain is not None and not isinstance(raw_remain, bool):
                try:
                    d = Decimal(str(raw_remain))
                    if d.is_finite():
                        remaining = d
                except (ArithmeticError, ValueError, TypeError):
                    remaining = None

        uid = acct.get("uid", f"(unattributed-{idx})")
        is_placeholder = str(uid).strip().lower() in _PLACEHOLDER_UIDS
        if is_placeholder:
            # Placeholder: excluded from the list, but its usage is tracked so
            # real consumption is never silently hidden (see 未归属消耗 line).
            excluded_token_sum += tokens
            excluded_credit_sum += credits
            # Reconciliation still runs over ALL rows (summary folds them all).
            acct_token_sum += tokens
            acct_credit_sum += credits
            continue

        accounts.append(
            AccountUsage(
                uid=uid,
                name=_sanitize_name(acct.get("nickname"), str(uid)),
                realm=acct.get("realm", ""),
                credits=credits,
                tokens=tokens,
                current=has_credits,
                has_credits=has_credits,
                remaining=remaining,
                status=status_map.get(str(uid), ""),
            )
        )
        acct_token_sum += tokens
        acct_credit_sum += credits

    models: list[ModelUsage] = []
    for mdl in payload.get("models", ()):
        window = mdl.get("window", {})
        credits = Decimal(str(window.get("credit", "0")))
        tokens = int(window.get("total_tokens", 0))
        # Skip all-time-only zero models: no current-day requests/errors/tokens/credits.
        if (
            tokens == 0
            and credits == 0
            and int(window.get("requests", 0)) == 0
            and int(window.get("errors", 0)) == 0
        ):
            continue
        models.append(ModelUsage(model=mdl.get("model", "(unknown)"), credits=credits, tokens=tokens))

    # Extend the roster with /accounts-only rows (consumed 0), then sort.
    accounts = merge_accounts_roster(accounts, payload, accounts_payload)
    accounts = [_maybe_with_status(a, status_map) for a in accounts]

    # Sort: credits desc, then uid/model lexicographically.
    accounts.sort(key=lambda a: (-a.credits, a.uid))
    models.sort(key=lambda m: (-m.credits, m.model))

    # Reconciliation warnings — never alter totals, never double-sum axes.
    # ONLY the account axis is reconciled: the Hub folds summary from ALL rows
    # (including billed error rows) but the per-model fold skips error rows
    # entirely (wb_proxy.py _feed_analytics vs _bump_analytics_models), so
    # sum(models) is legitimately LESS than summary whenever a failed request
    # was billed. Warning on that would train users to ignore warnings.
    if acct_token_sum != total_tokens:
        warnings.append(
            f"账号 Token 合计 {acct_token_sum} 与总 Token {total_tokens} 不一致。"
        )
    if abs(acct_credit_sum - total_credits) > _RECONCILE_TOLERANCE:
        warnings.append("账号积分合计与总积分不一致。")

    return DailyReport(
        day=day,
        timezone=timezone,
        as_of=as_of,
        total_tokens=total_tokens,
        total_credits=total_credits,
        accounts=tuple(accounts),
        models=tuple(models),
        warnings=tuple(warnings),
        excluded_credits=excluded_credit_sum,
        excluded_tokens=excluded_token_sum,
        total_tokens_all_time=total_tokens_all_time,
    )


def _fmt_rel_time(seconds: int, hub_now: datetime, tz: ZoneInfo) -> str:
    """Render a recovery time for a RELATIVE-seconds field (cooldownFor etc.).

    recovery = hub_now (the instant the /accounts response was generated)
    + seconds, formatted MM-DD HH:MM in the reporting timezone.
    """
    return (hub_now + timedelta(seconds=seconds)).astimezone(tz).strftime("%m-%d %H:%M")


def _fmt_abs_time(epoch_seconds: Any, tz: ZoneInfo) -> str | None:
    """Render a recovery time for an ABSOLUTE-epoch field (modelCooldowns
    expiresAt / cappedAt). The epoch is used AS-IS — never added to now.
    Returns None when the value is not a usable (non-bool) finite number.
    """
    if isinstance(epoch_seconds, bool) or epoch_seconds is None:
        return None
    try:
        d = Decimal(str(epoch_seconds))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    instant = datetime.fromtimestamp(float(d), tz=dt_timezone.utc)
    return instant.astimezone(tz).strftime("%m-%d %H:%M")


def status_segments(
    account: Mapping[str, Any],
    tz_name: str,
    hub_now: datetime | None,
) -> str:
    """Build the per-account STATUS text from /accounts live state.

    Returns "" for a normal account (nothing is printed). Segments are in the
    documented order and joined with '；'; the caller wraps them in (…).

    UNITS: cooldownFor / breakerFor / degradeFor are RELATIVE seconds (base =
    the /accounts response's Hub clock, falling back to the local UTC clock
    when unavailable); modelCooldowns[].expiresAt / cappedAt are ABSOLUTE
    epochs rendered AS-IS.
    """
    tz = ZoneInfo(tz_name)
    base = hub_now if hub_now is not None else datetime.now(dt_timezone.utc)

    def rel(seconds: Any) -> str | None:
        # Relative seconds: nonneg int (bool rejected; 0 must not appear —
        # the Hub normalizes it to null — but a 0 here yields "now" anyway).
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None
        try:
            n = int(seconds)
        except (ArithmeticError, ValueError, TypeError, OverflowError):
            return None
        return _fmt_rel_time(n, base, tz)

    segments: list[str] = []
    # 1. credential expired → 凭证过期，需重新登录 (no time). v1.7.19 sends
    #    tokenExpired and needRelogin as the same condition; treat them as ONE
    #    condition and never emit two segments. An expired credential makes
    #    the account unusable regardless of the enabled flag.
    if account.get("tokenExpired") is True or account.get("needRelogin") is True:
        segments.append("凭证过期，需重新登录")
    # 2. enabled=false → 已停用 (no time)
    if account.get("enabled") is False:
        segments.append("已停用")
    # 3. restrictedFor → 上游风控限制，{time} 恢复. v1.7.19 added this
    #    RELATIVE-seconds field (upstream account-level risk control,
    #    code 11140); it outranks a self-inflicted breaker.
    if (t := rel(account.get("restrictedFor"))) is not None:
        segments.append(f"上游风控限制，{t} 恢复")
    # 4. breakerFor → 熔断，{time} 恢复
    if (t := rel(account.get("breakerFor"))) is not None:
        segments.append(f"熔断，{t} 恢复")
    # 3. degradeFor → 降级，{time} 恢复
    if (t := rel(account.get("degradeFor"))) is not None:
        segments.append(f"降级，{t} 恢复")
    # 4. cooldownFor / inCooldown → 冷却，{time} 恢复
    t = rel(account.get("cooldownFor"))
    if t is None and account.get("inCooldown") is True:
        t = _fmt_rel_time(0, base, tz)  # deadline within the current second
    if t is not None:
        segments.append(f"冷却，{t} 恢复")
    # 5. modelCooldowns entries → {model}被429，{t} 恢复, or with cappedAt:
    #    {model}额度用满，{t} 恢复 (time from expiresAt, ABSOLUTE epoch)
    cooldowns = account.get("modelCooldowns")
    if isinstance(cooldowns, list):
        for entry in cooldowns:
            if not isinstance(entry, Mapping):
                continue
            model = entry.get("model")
            if not isinstance(model, str) or not model:
                continue
            t = _fmt_abs_time(entry.get("expiresAt"), tz)
            if t is None:
                continue
            label = "额度用满" if "cappedAt" in entry else "被429"
            segments.append(f"{model}{label}，{t} 恢复")
    # 6-8. boolean guards (no time)
    if account.get("reserveBlocked") is True:
        segments.append("保留积分停单")
    if account.get("dailyLimitBlocked") is True:
        segments.append("当日限额停单")
    if account.get("creditLimitReached") is True:
        segments.append("积分达限（仅免费模型）")
    # 9. disabledModels → operator-configured per-account model blacklist.
    #    Distinct from modelCooldowns: the account keeps serving other models.
    #    Render ids exactly as the Hub sends them (upstream stores lowercase).
    #    Cap the rendered list at 6 ids and append 等 N 个 with the real total,
    #    so a very long list cannot blow up the message.
    disabled = account.get("disabledModels")
    if isinstance(disabled, list):
        ids = [m for m in disabled if isinstance(m, str) and m]
        if ids:
            shown = ids[:6]
            tail = f" 等 {len(ids)} 个" if len(ids) > 6 else ""
            segments.append(f"已禁用模型：{'、'.join(shown)}{tail}")
    # 10. Consecutive hard-failure early warning. Only for fails >= 2: one
    #     transient failure is background noise, but two means the next hard
    #     failure is the one that matters. Suppressed when a blocker
    #     (熔断/降级/冷却) is already shown — the count would be redundant.
    #     The breaker threshold is NOT exposed by the API, so the count is
    #     reported factually with no "还差 N 次熔断" wording.
    fails = account.get("fails")
    if (
        isinstance(fails, int)
        and not isinstance(fails, bool)
        and fails >= 2
        and not any(s.startswith(("熔断", "降级", "冷却")) for s in segments)
    ):
        fail_window = account.get("failWindow")
        if isinstance(fail_window, int) and not isinstance(fail_window, bool) and fail_window > 0:
            segments.append(f"连续失败 {fails} 次（{fail_window} 秒窗口）")
        else:
            segments.append(f"连续失败 {fails} 次")
    # 11. Historical-error fallback. lastError is a HISTORICAL record: it
    #     survives after a model cooldown pill clears (upstream v1.7.14+),
    #     so an account can look idle yet carry a stale 429 with NO current
    #     block and NO unblock time. Only show that history when no other
    #     segment was produced at all, and never dump the raw body (it is a
    #     truncated upstream JSON containing a requestId) — extract only the
    #     HTTP status and the numeric "code". Unparseable text renders nothing.
    if not segments:
        last_error = account.get("lastError")
        if isinstance(last_error, str) and last_error:
            status_m = re.search(r"HTTP\s+(\d{3})", last_error)
            code_m = re.search(r'"code"\s*[:=]\s*(\d+)', last_error)
            if status_m and status_m.group(1) == "429":
                if code_m:
                    text = f"近期429（code {code_m.group(1)}，当前未限制）"
                else:
                    text = "近期429（当前未限制）"
                segments.append(text[:80])
            elif status_m:
                segments.append(f"最近错误 {status_m.group(1)}（当前未限制）"[:80])
    return "；".join(segments)


def _maybe_with_status(
    row: AccountUsage, status_map: Mapping[str, str]
) -> AccountUsage:
    """Attach pre-rendered status to a row that does not have one yet."""
    if row.status:
        return row
    status = status_map.get(row.uid, "")
    if not status:
        return row
    return dataclasses_replace(row, status=status)


def merge_accounts_roster(
    report_accounts: list[AccountUsage],
    analytics_payload: AnalyticsPayload,
    accounts_payload: AccountsPayload | None,
) -> list[AccountUsage]:
    """Extend the roster so every account in /accounts?realm=all is listed.

    - analytics rows stay the primary source (consumed credits / remaining);
    - an account present only in /accounts is appended with consumed 0 and
      remaining from credits.remain;
    - placeholder uids ((unattributed)/(unknown)) are never listed;
    - sort: consumed credits desc, then uid.
    """
    listed_uids = {a.uid for a in report_accounts}
    if accounts_payload is not None:
        for entry in accounts_payload.get("accounts", ()):
            uid = entry.get("uid")
            if not isinstance(uid, str) or not uid or uid.strip().lower() in _PLACEHOLDER_UIDS:
                continue
            if uid in listed_uids:
                continue
            listed_uids.add(uid)
            remaining: Decimal | None = None
            credits_obj = entry.get("credits")
            if isinstance(credits_obj, Mapping):
                raw_remain = credits_obj.get("remain")
                if raw_remain is not None and not isinstance(raw_remain, bool):
                    try:
                        d = Decimal(str(raw_remain))
                        if d.is_finite():
                            remaining = d
                    except (ArithmeticError, ValueError, TypeError):
                        remaining = None
            report_accounts.append(
                AccountUsage(
                    uid=uid,
                    name=_sanitize_name(entry.get("nickname"), uid),
                    realm=entry.get("realm") if isinstance(entry.get("realm"), str) else "",
                    credits=Decimal("0"),
                    tokens=0,
                    current=True,
                    has_credits=isinstance(credits_obj, Mapping),
                    remaining=remaining,
                )
            )
    # Analytics placeholders must never leak into the list even if a Hub
    # echoed one back in /accounts.
    report_accounts = [a for a in report_accounts if a.uid.strip().lower() not in _PLACEHOLDER_UIDS]
    report_accounts.sort(key=lambda a: (-a.credits, a.uid))
    return report_accounts


def format_credit(value: Decimal) -> str:
    """Render a credit Decimal: up to 6 dp, HALF_UP, trailing zeros trimmed."""
    quantized = value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def format_report(report: DailyReport) -> str:
    """Render the v1.0.2 plain-text template."""
    lines = [
        "WorkBuddy 当日用量",
        report.day.isoformat(),
        f"当日已消耗 Token：{_fmt_int(report.total_tokens)}",
        f"当日已消耗积分：{format_credit(report.total_credits)}",
        # Cumulative all-time consumption; "—" when the Hub gave no all_time block.
        f"累计消耗 Token："
        + ("—" if report.total_tokens_all_time is None else _fmt_int(report.total_tokens_all_time)),
    ]
    if report.accounts:
        lines.append(f"账号（{len(report.accounts)}）：")
        for a in report.accounts:
            realm = f" [{a.realm}]" if a.realm else ""
            # STATUS: appended in parentheses between the realm marker and the
            # full-width colon. Normal accounts (status "") show NOTHING.
            status = f"({a.status})" if a.status else ""
            remain = "—" if a.remaining is None else format_credit(a.remaining)
            lines.append(
                f"{a.uid[:8]}{realm}{status}：已消耗 {format_credit(a.credits)} 积分，剩余 {remain} 积分"
            )
    else:
        lines.append("账号：无账号")
    if report.excluded_credits != 0 or report.excluded_tokens != 0:
        lines.append(
            f"未归属消耗：{format_credit(report.excluded_credits)} 积分 / "
            f"{_fmt_int(report.excluded_tokens)} Token"
        )
    if report.models:
        lines.append("使用模型统计：")
        for m in report.models:
            lines.append(f"{m.model}：{format_credit(m.credits)} 积分")
    else:
        lines.append("使用模型统计：今日暂无用量")
    for w in report.warnings:
        lines.append(f"警告：{w}")
    return "\n".join(lines)


def paginate_report(text: str, max_chars: int, max_lines: int) -> list[str]:
    """Split plain text into complete pages at line boundaries.

    - never drops or truncates a full line's content: an overlong individual
      line is hard-split into chunks;
    - each page has <= max_chars payload chars and <= max_lines lines
      (the [WorkBuddy i/N] prefix is added afterwards and is not counted).
    """
    if max_chars < 1 or max_lines < 1:
        raise ValueError("max_chars and max_lines must be positive")

    # First, break any individual line longer than max_chars.
    physical: list[str] = []
    for line in text.split("\n"):
        while len(line) > max_chars:
            physical.append(line[:max_chars])
            line = line[max_chars:]
        physical.append(line)

    pages: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in physical:
        line_len = len(line)
        would_be_lines = len(current) + 1
        would_be_chars = current_len + line_len + (1 if current else 0)
        if current and (would_be_lines > max_lines or would_be_chars > max_chars):
            pages.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len = would_be_chars
    if current:
        pages.append("\n".join(current))

    if len(pages) == 1:
        return pages
    total = len(pages)
    return [f"[WorkBuddy {i}/{total}]\n{page}" for i, page in enumerate(pages, start=1)]
