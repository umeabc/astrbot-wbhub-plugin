"""Typed wire records and immutable report dataclasses.

Wire records mirror the WorkBuddy Hub analytics response shape exactly.
All numeric parsing is done with Decimal to avoid float drift for credits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, TypedDict

# ---------------------------------------------------------------------------
# Wire TypedDict records (source-shaped; unknown extra fields are tolerated)
# ---------------------------------------------------------------------------


class Stat(TypedDict, total=False):
    """Exact Stat record from wb_proxy.py (lines 6176-6186, 8222-8232)."""

    requests: int
    errors: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    total_tokens: int
    credit: Decimal
    cost_cny: Decimal
    ttft_sum: float
    ttft_n: int
    speed_sum: float
    speed_n: int
    elapsed_sum: float
    elapsed_n: int
    cache_hit_pct: float
    reasoning_ratio: float
    ttft_ms_avg: float
    speed_avg: float
    elapsed_ms_avg: float


class Distribution(TypedDict, total=False):
    """Per (account, model) distribution; NOTE: no credit field exists here."""

    requests: int
    tokens: int
    reasoning: int
    cost_cny: Decimal


class AccountAnalytics(TypedDict, total=False):
    uid: str
    nickname: str
    realm: str
    domain: str
    window: Stat
    all_time: Stat
    window_models: Mapping[str, Distribution]
    all_models: Mapping[str, Distribution]
    credits: Mapping[str, Any]  # optional; presence suggests current pool account


class AccountsPayload(TypedDict, total=False):
    """GET /accounts?realm=all payload (live-state roster).

    ``accounts`` entries are kept as loose dicts: the status fields
    (breakerFor / cooldownFor / degradeFor / modelCooldowns / the three
    boolean guards) are validated defensively at render time, not here.
    """

    accounts: list[dict[str, Any]]
    storage: str
    usable: int


class ModelAnalytics(TypedDict, total=False):
    model: str
    window: Stat
    all_time: Stat


class AnalyticsPayload(TypedDict, total=False):
    window: Mapping[str, float]
    realm: str
    usd_cny: float
    summary: Mapping[str, Stat]
    accounts: list[AccountAnalytics]
    models: list[ModelAnalytics]
    keys: list[Any]


# ---------------------------------------------------------------------------
# Immutable report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountUsage:
    """One account row of the daily report.

    ``has_credits`` records whether the Hub included the optional ``credits``
    object for this analytics row (a source-derived hint that the account is a
    current pool account; historical/unattributed rows omit it).  It is never
    used to silently drop rows.
    """

    uid: str
    name: str
    realm: str
    credits: Decimal
    tokens: int
    current: bool
    has_credits: bool = False
    # Remaining credit balance from credits.remain (NOT window.credit, which is
    # today's consumption). None when credits/remain is absent or unusable.
    remaining: Decimal | None = None
    # Pre-rendered per-account status text, e.g.
    # "熔断，10-17 15:23 恢复；deepseek-v4.1-flash被429，10-17 14:50 恢复".
    # Empty string for normal accounts → nothing is printed (no parentheses).
    status: str = ""
    # True for a row whose uid is no longer in the live pool
    # (/accounts?realm=all) but which DID consume something in the queried
    # window. Such rows are listed (marked 已删除) instead of being pruned, so
    # a multi-day total can be explained account by account. Their balance is
    # unknown → no 剩余 column, and they are not "current" accounts.
    deleted: bool = False


@dataclass(frozen=True)
class ModelUsage:
    model: str
    credits: Decimal
    tokens: int


@dataclass(frozen=True)
class DailyReport:
    day: date
    timezone: str
    as_of: datetime
    total_tokens: int
    total_credits: Decimal
    accounts: tuple[AccountUsage, ...] = field(default=())
    models: tuple[ModelUsage, ...] = field(default=())
    warnings: tuple[str, ...] = field(default=())
    # Usage carried by excluded placeholder rows (uid (unattributed)/(unknown)).
    # Normal case: both zero → nothing extra is printed.
    excluded_credits: Decimal = field(default_factory=lambda: Decimal("0"))
    excluded_tokens: int = 0
    # Cumulative (all-time) token consumption from summary.all_time.total_tokens.
    # None when the Hub payload has no all_time block — rendered as "—" never 0.
    total_tokens_all_time: int | None = None
    # Non-default window (/wb 1|3|7|30|week|month|all). "" keeps the legacy
    # single-day template byte-identical; otherwise the label prefixes the
    # header and the two consumption lines. range_start is None for the
    # all-time window ("有记录以来").
    range_label: str = ""
    range_start: datetime | None = None
    range_end: datetime | None = None
    # True for the three "current state" views (/wb, /wb 0, /wb 1): those also
    # print the cumulative token total, each account's remaining balance and
    # the 429 / risk-control status. Wider windows print consumption only —
    # a month of usage next to a momentary cooldown reads as one snapshot.
    detailed: bool = True
