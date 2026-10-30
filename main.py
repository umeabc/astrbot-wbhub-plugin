"""AstrBot Star plugin: WorkBuddy Hub daily usage report via /wb command.

Lifecycle: creates the aiohttp ClientSession in initialize() and closes it in
terminate() (even if initialize partially failed). Authorization runs BEFORE
any network request. No LLM/provider calls are made by this plugin.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone as dt_timezone
from typing import AsyncIterator, Optional
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .client import (
    HubAuthError,
    HubError,
    HubProtocolError,
    HubRateLimitError,
    HubUnavailableError,
    WorkBuddyClient,
)
from .models import DailyReport
from .reporting import (
    aggregate,
    append_update_notice,
    day_bounds,
    derive_update_notice,
    format_report,
    paginate_report,
)

# Clock-skew threshold (seconds): when the Hub's clock differs from the local
# clock by more than this, log a warning — a silent mis-report becomes an
# obvious diagnostic.
CLOCK_SKEW_WARN_SECONDS = 120

# A captured Hub Date header is only evidence about the response that carried
# it. Beyond this age it is treated as absent when building a time window, so
# the previous /wb's instant can never become this report's `until`.
HUB_CLOCK_FRESH_SECONDS = 5.0

# Exact user-facing error strings (plan §4).
ERR_UNAUTHORIZED = "无权查询 WorkBuddy 用量。"
ERR_NOT_CONFIGURED = "WorkBuddy 查询未配置，请联系管理员。"
ERR_TRANSPORT = "WorkBuddy Hub 暂不可达，请稍后重试。"
ERR_AUTH = "WorkBuddy Hub 认证失败，请联系管理员检查配置。"
ERR_SCHEMA = "WorkBuddy Hub 响应格式不兼容，请联系管理员。"
ERR_RATE_LIMIT = "WorkBuddy Hub 请求受限，请稍后重试。"
ERR_COOLDOWN = "查询过于频繁，请稍后重试。"

# /wbdiag notice-state strings (offline; no Hub request is made for these).
DIAG_UPDATE_OFF = "网关更新: 已关闭"
DIAG_UPDATE_NONE = "网关更新: 无或未知（诊断不联网）"


def _id_set(value: object) -> set[str]:
    """Normalize an allowlist config value into a set of id strings.

    Tolerates a real list/tuple/set (of str or int), a single bare id, and a
    comma-separated string using either ',' or the full-width '，'. A bare
    non-empty string must NEVER be iterated character by character.
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in value:
            result |= _id_set(item)
        return result
    if isinstance(value, str):
        parts = value.replace("，", ",").split(",")
        return {p.strip() for p in parts if p.strip()}
    if isinstance(value, bool):
        return set()  # not a usable id
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return set()
        if value.is_integer():
            return {str(int(value))}
        return {str(value).strip()}
    if isinstance(value, int):
        return {str(value).strip()}
    # Other scalars: use str() only if it yields a non-empty, scalar-ish string.
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - never raise on odd input
        return set()
    return {text} if text else set()


def _scalar_id(value: object) -> str:
    """Best-effort single id extraction from an unknown value (never raises)."""
    try:
        return next(iter(_id_set(value)), "")
    except Exception:  # noqa: BLE001 - never raise on odd input
        return ""


class WorkBuddyUsagePlugin(Star):
    """Star plugin exposing the /wb command."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[WorkBuddyClient] = None
        # Serializes report construction so logins/token renewal never race.
        self._report_lock = asyncio.Lock()
        # Per-sender cooldown: sender id -> last successful/attempted ts.
        self._last_query: dict[str, float] = {}
        # Gateway update-notice state (in-memory only, never persisted):
        # the last rendered notice string ("" = none known), the monotonic
        # timestamp of the last background manual check we fired, and the
        # single in-flight background task handle.
        self._update_notice: str = ""
        self._last_update_check_ts: float | None = None
        self._update_check_task: Optional[asyncio.Task] = None

    # -- validation helpers ---------------------------------------------------

    def _cfg_timeout(self) -> float:
        try:
            value = float(self._config.get("timeout_seconds", 15.0))
        except (TypeError, ValueError):
            value = 15.0
        return min(120.0, max(1.0, value))

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(self._config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(hi, max(lo, value))

    def _validate_timezone(self) -> str:
        tz = str(self._config.get("default_timezone", "Asia/Shanghai")) or "Asia/Shanghai"
        try:
            ZoneInfo(tz)
        except Exception:
            logger.warning("invalid default_timezone %r, falling back to Asia/Shanghai", tz)
            tz = "Asia/Shanghai"
        return tz

    def _credentials_configured(self) -> bool:
        password = str(self._config.get("panel_password", "") or "")
        token = str(self._config.get("panel_token", "") or "")
        return bool(password or token)

    # -- lifecycle -------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the long-lived HTTP session and client."""
        base_url = str(self._config.get("base_url", "") or "").strip()
        if not base_url:
            logger.warning("workbuddy_usage: base_url empty; /wb will report unconfigured")
            return
        self._session = aiohttp.ClientSession()
        self._client = WorkBuddyClient(
            base_url=base_url,
            panel_password=str(self._config.get("panel_password", "") or ""),
            panel_token=str(self._config.get("panel_token", "") or ""),
            timeout_seconds=self._cfg_timeout(),
            max_response_bytes=self._cfg_int("max_response_bytes", 5242880, 65536, 52428800),
            proxy=str(self._config.get("proxy", "") or ""),
            session=self._session,
        )
        # Validate the timezone at initialization so bad config surfaces early.
        self._validate_timezone()
        logger.info("workbuddy_usage initialized")

    async def terminate(self) -> None:
        """Close the HTTP session even if initialization was partial."""
        # Cancel/await a pending background update check FIRST so it never
        # touches a closed session; safe when none was ever created.
        task = self._update_check_task
        self._update_check_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.logout()
            except Exception:  # noqa: BLE001 - logout must never block close
                pass
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    # -- authorization -----------------------------------------------------------

    def _event_ids(self, event: AstrMessageEvent) -> tuple[Optional[str], Optional[str]]:
        """Extract (sender_id, group_id) platform-agnostically.

        Never gates on the platform name: every adapter exposes the same
        accessors. Both reads are individually guarded; empty strings and
        unavailable ids normalize to None.
        """
        try:
            sender = event.get_sender_id()
        except Exception:  # noqa: BLE001 - some adapters may not expose a sender id
            sender = None
        try:
            group = event.get_group_id()
        except Exception:  # noqa: BLE001 - private chats may not expose groups
            group = None
        return str(sender) if sender else None, str(group) if group else None

    def _platform_gate_ok(self, adapter: str) -> bool:
        """True when allowed_platforms is empty (no gate) or contains adapter."""
        configured = _id_set(self._config.get("allowed_platforms"))
        if not configured:
            return True
        return bool(adapter) and adapter.strip().lower() in {
            name.strip().lower() for name in configured
        }

    def _deny_reason(self, event: AstrMessageEvent) -> str:
        """Return "" when allowed, else the exact user-facing denial text.

        The returned string is both the chat reply and the log reason, so log
        and chat always agree. Never echoes the caller's account/group id.
        """
        adapter = str(getattr(event, "get_platform_name", lambda: "")() or "").strip()
        if not self._platform_gate_ok(adapter):
            reason = (
                f"无权查询 WorkBuddy 用量。（当前消息平台不在 allowed_platforms 中："
                f"{adapter or '未知'}）"
            )
            logger.info("workbuddy_usage denied: platform gate (adapter=%r)", adapter)
            return reason

        sender, group = self._event_ids(event)
        if sender is None:
            raw_sender = None
            try:
                raw_sender = event.get_sender_id()
            except Exception:  # noqa: BLE001
                raw_sender = None
            reason = "无权查询 WorkBuddy 用量。（无法识别你的账号标识）"
            detail = f"sender id missing (adapter={adapter!r} raw_sender={raw_sender!r})"
            logger.info("workbuddy_usage denied: %s", detail)
            return reason

        is_admin = False
        try:
            is_admin = bool(event.is_admin())
        except Exception:  # noqa: BLE001
            is_admin = False

        # Effective allowlists: new keys unioned with the legacy qq-only keys.
        allowed_users = (
            _id_set(self._config.get("allowed_users"))
            | _id_set(self._config.get("allowed_qq_users"))
        )
        allowed_groups = (
            _id_set(self._config.get("allowed_groups"))
            | _id_set(self._config.get("allowed_qq_groups"))
        )
        allow_private = bool(self._config.get("allow_private", True))
        allow_admin = bool(self._config.get("allow_admin", True))
        ctx = (
            f"adapter={adapter!r} sender={sender!r} group={group!r} "
            f"is_admin={is_admin}"
        )

        private_chat = not group
        if private_chat:
            if not allow_private:
                reason = "无权查询 WorkBuddy 用量。（私聊查询已被关闭：allow_private=false）"
            elif is_admin:
                if allow_admin:
                    return ""
                reason = "无权查询 WorkBuddy 用量。（管理员豁免已被 allow_admin=false 关闭）"
            elif sender in allowed_users:
                return ""
            else:
                reason = "无权查询 WorkBuddy 用量。（你的账号不在 allowed_users 白名单中）"
        else:
            if is_admin:
                if allow_admin:
                    return ""
                reason = "无权查询 WorkBuddy 用量。（管理员豁免已被 allow_admin=false 关闭）"
            elif sender not in allowed_users:
                reason = (
                    "无权查询 WorkBuddy 用量。（你的账号不在 allowed_users 白名单中；"
                    "群聊要求 allowed_users 与 allowed_groups 同时命中）"
                )
            elif group not in allowed_groups:
                reason = "无权查询 WorkBuddy 用量。（本群不在 allowed_groups 白名单中）"
            else:
                return ""

        logger.info(
            "workbuddy_usage denied: %s | %s | users=%d groups=%d",
            reason,
            ctx,
            len(allowed_users),
            len(allowed_groups),
        )
        return reason

    def _allowed(self, event: AstrMessageEvent) -> bool:
        """Authorization gate. Denies by default; runs before any network I/O."""
        return not self._deny_reason(event)

    # -- report ------------------------------------------------------------------

    # -- gateway update notice ---------------------------------------------------

    def _cfg_update_notice_enabled(self) -> bool:
        return bool(self._config.get("update_notice", True))

    def _maybe_schedule_update_check(self) -> None:
        """Fire-and-forget background POST /updates/check.

        Time-throttled (NOT gated on "no data yet"): the Hub persists check
        results, so gating on missing data would fire once and then go
        permanently stale. Conditions, ALL required:
        - update_notice config is true;
        - our own monotonic timestamp is absent or older than the interval;
        - no check is currently in flight in this process;
        - the payload does not say checking: true (respect the Hub's state).
        The timestamp is updated the moment we fire, so a failing GitHub
        request can never cause a request storm. The task swallows ALL
        exceptions: a failed update check must never surface to the user.
        """
        if not self._cfg_update_notice_enabled():
            return
        if self._update_check_task is not None and not self._update_check_task.done():
            return
        interval_hours = self._cfg_int("update_check_interval_hours", 6, 1, 168)
        now_mono = time.monotonic()
        if (
            self._last_update_check_ts is not None
            and (now_mono - self._last_update_check_ts) < interval_hours * 3600
        ):
            return
        client = self._client
        if client is None:
            return
        self._last_update_check_ts = now_mono
        self._update_check_task = asyncio.get_running_loop().create_task(
            self._background_update_check(client)
        )

    async def _background_update_check(self, client: WorkBuddyClient) -> None:
        """Run one manual check; on success remember the derived notice for
        the NEXT /wb. Never raises — every failure is logged and dropped."""
        try:
            payload = await client.check_updates()
            self._update_notice = derive_update_notice(payload)
            logger.info(
                "workbuddy_usage: background gateway update check done "
                "(notice=%s)",
                bool(self._update_notice),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never fatal, never shown
            logger.info(
                "workbuddy_usage: background gateway update check failed "
                "(ignored): %s",
                type(exc).__name__,
            )

    def _fresh_hub_clock(self) -> Optional[datetime]:
        """The Hub clock captured by the response we JUST received, or None.

        ``client.hub_now()`` outlives the response that produced it, so using
        it unconditionally builds the day window from the PREVIOUS /wb's
        instant — minutes, or a whole day, old. That made the window end in
        the past and /wb repeat the earlier report. Only a capture younger
        than HUB_CLOCK_FRESH_SECONDS counts as evidence about "now".
        """
        client = self._client
        if client is None:
            return None
        age = client.hub_now_age()
        if age is None or age > HUB_CLOCK_FRESH_SECONDS:
            return None
        return client.hub_now()

    async def _get_report(self, now: datetime) -> DailyReport:
        """Fetch analytics for the local day containing "now" and aggregate.

        The window is built from a FRESH Hub clock: /accounts is fetched
        first, so its Date header (captured moments ago) is available before
        since/until are chosen. The Hub timestamps every usage row, so its
        clock — not the host process's — is the authority for "today". A
        capture is only valid for its own response, so it is never reused
        from an earlier /wb. If /accounts is unavailable, the local clock
        provides a provisional window that the analytics Date corrects once.
        """
        assert self._client is not None
        tz_name = self._validate_timezone()
        tz = ZoneInfo(tz_name)

        # First Hub call: per-account live state (cooldowns / breaker / etc).
        # It also donates the fresh Hub clock used for the window below.
        # MUST NEVER break /wb: any failure here degrades to no status info
        # (and no roster extension) with a logged warning.
        accounts_payload = None
        accounts_hub_now = None
        try:
            accounts_payload = await self._client.fetch_accounts()
            # This Date header is both the base for the relative-seconds
            # fields (see reporting.status_segments) and the window's clock.
            accounts_hub_now = self._fresh_hub_clock()
        except HubError as exc:
            logger.warning(
                "workbuddy_usage: /accounts unavailable, report will omit "
                "per-account status: %s",
                type(exc).__name__,
            )

        hub_now_utc = accounts_hub_now
        if hub_now_utc is not None:
            self._warn_clock_skew(hub_now_utc)
            since, until = day_bounds(hub_now_utc, tz_name)
        else:
            # /accounts failed or sent no Date header: provisional window from
            # the local clock, corrected below from the analytics response.
            since, until = day_bounds(now, tz_name)

        payload = await self._client.fetch_analytics(since, until)

        # Degraded-path self-correction. The analytics response carries the
        # Hub's Date too: if it disagrees about the calendar day (UTC-hosted
        # AstrBot reporting a +08:00 day) or puts "now" more than the skew
        # threshold past our provisional `until`, refetch ONCE with the Hub's
        # authoritative window. The normal path never reaches this: /accounts
        # already supplied a fresh clock.
        if hub_now_utc is None:
            fresh = self._fresh_hub_clock()
            if fresh is not None:
                self._warn_clock_skew(fresh)
                hub_since, hub_until = day_bounds(fresh, tz_name)
                if (
                    hub_since != since
                    or abs(hub_until - until) > CLOCK_SKEW_WARN_SECONDS
                ):
                    since, until = hub_since, hub_until
                    payload = await self._client.fetch_analytics(since, until)
                hub_now_utc = fresh
        # Gateway update notice: cheap GET /updates, derive the notice, and
        # (throttled) fire a background manual check. A failure here must
        # NEVER affect the report — degrade to no notice, log at info.
        notice = ""
        if self._cfg_update_notice_enabled():
            try:
                updates_payload = await self._client.fetch_updates()
                notice = derive_update_notice(updates_payload)
                if notice:
                    self._update_notice = notice
                if not updates_payload.get("checking"):
                    self._maybe_schedule_update_check()
            except HubError as exc:
                logger.info(
                    "workbuddy_usage: /updates unavailable, no update notice: %s",
                    type(exc).__name__,
                )
            except Exception as exc:  # noqa: BLE001 - never fatal
                logger.info(
                    "workbuddy_usage: /updates handling failed, no update "
                    "notice: %s",
                    type(exc).__name__,
                )
        # Capture the day from the SAME instant used for the query bounds, so
        # a request that completes after midnight still reports that day.
        effective = hub_now_utc if hub_now_utc is not None else now
        local_now = (
            effective.replace(tzinfo=tz) if effective.tzinfo is None else effective.astimezone(tz)
        )
        return aggregate(
            payload,
            local_now.date(),
            tz_name,
            local_now,
            accounts_payload=accounts_payload,
            hub_now=accounts_hub_now,
        )

    def report_notice(self) -> str:
        """The current in-memory gateway-update notice ("" = none).

        _get_report sets it for THIS reply; the background manual check may
        also set it between /wb calls (it appears on the next report, never
        retroactively). Kept as a method for clean test injection.
        """
        return self._update_notice

    def _warn_clock_skew(self, hub_now_utc: datetime) -> None:
        """Log a warning when the Hub's clock differs from the local clock."""
        local_utc = datetime.now(dt_timezone.utc)
        delta = abs((local_utc - hub_now_utc).total_seconds())
        if delta > CLOCK_SKEW_WARN_SECONDS:
            logger.warning(
                "workbuddy_usage: clock skew between Hub and local clock: "
                "hub=%s local=%s delta=%.0f seconds — 'today' may be computed "
                "against the wrong calendar day",
                hub_now_utc.isoformat(),
                local_utc.isoformat(),
                delta,
            )

    # -- command -------------------------------------------------------------------

    @filter.command("wb")
    async def wb(self, event: AstrMessageEvent) -> AsyncIterator:
        """/wb — WorkBuddy Hub 当日用量报表。"""
        if not self._allowed(event):
            yield event.plain_result(self._deny_reason(event))
            self._stop(event)
            return

        if not self._credentials_configured():
            yield event.plain_result(ERR_NOT_CONFIGURED)
            self._stop(event)
            return

        sender = str(event.get_sender_id() or "unknown")
        cooldown = self._cfg_int("cooldown_seconds", 10, 0, 3600)
        now_ts = time.monotonic()
        # Prune stale cooldown entries so the map cannot grow unboundedly.
        if cooldown > 0:
            self._last_query = {
                sid: ts for sid, ts in self._last_query.items()
                if (now_ts - ts) < cooldown
            }
        last = self._last_query.get(sender)
        if cooldown > 0 and last is not None and (now_ts - last) < cooldown:
            yield event.plain_result(ERR_COOLDOWN)
            self._stop(event)
            return
        self._last_query[sender] = now_ts

        if self._client is None:
            yield event.plain_result(ERR_NOT_CONFIGURED)
            self._stop(event)
            return

        max_chars = self._cfg_int("max_message_chars", 1800, 300, 4000)
        max_lines = self._cfg_int("max_lines_per_message", 35, 5, 100)

        async with self._report_lock:
            try:
                # datetime.now().astimezone(): an AWARE local instant. Never
                # pass a naive datetime — in a container whose local time is
                # UTC, a naive value stamped with the reporting timezone
                # mislabels the instant and truncates the day window (the
                # v1.0.2 under-count bug). The Hub-clock path in _get_report
                # supersedes this when a Date header was captured.
                report = await self._get_report(datetime.now().astimezone())
            except HubAuthError:
                yield event.plain_result(ERR_AUTH)
            except HubRateLimitError:
                yield event.plain_result(ERR_RATE_LIMIT)
            except HubUnavailableError:
                yield event.plain_result(ERR_TRANSPORT)
            except HubProtocolError:
                yield event.plain_result(ERR_SCHEMA)
            except HubError:
                yield event.plain_result(ERR_TRANSPORT)
            else:
                text = append_update_notice(format_report(report), self.report_notice())
                for page in paginate_report(text, max_chars, max_lines):
                    yield event.plain_result(page)
        self._stop(event)

    @filter.command("wbdiag")
    async def wbdiag(self, event: AstrMessageEvent) -> AsyncIterator:
        """/wbdiag — self-service authorization diagnostic for the caller."""
        adapter = getattr(event, "get_platform_name", lambda: "")() or ""
        try:
            raw_sender = event.get_sender_id()
        except Exception:  # noqa: BLE001
            raw_sender = None
        try:
            raw_group = event.get_group_id()
        except Exception:  # noqa: BLE001
            raw_group = None

        try:
            is_admin = bool(event.is_admin())
        except Exception:  # noqa: BLE001
            is_admin = False

        sender = str(raw_sender) if raw_sender else None
        group = str(raw_group) if raw_group else None
        private_chat = not group

        allowed_users = (
            _id_set(self._config.get("allowed_users"))
            | _id_set(self._config.get("allowed_qq_users"))
        )
        allowed_groups = (
            _id_set(self._config.get("allowed_groups"))
            | _id_set(self._config.get("allowed_qq_groups"))
        )
        allowed_platforms = _id_set(self._config.get("allowed_platforms"))

        # Reuse the same reason logic, but WITHOUT emitting a duplicate log line
        # (a diagnostic should not look like a denial in the log).
        reason = self._deny_reason(event)

        chat_kind = "私聊" if private_chat else "群聊"
        lines = [
            "[WorkBuddy 诊断]",
            f"平台/适配器: {adapter or '（未知）'}",
            f"发送者 ID: {sender or '（缺失）'}",
            f"群 ID: {group or '（私聊）'}",
            f"聊天类型: {chat_kind}",
            f"is_admin: {is_admin}",
            f"allowed_platforms: {'未限制' if not allowed_platforms else f'{len(allowed_platforms)} 项'}",
            f"allowed_users: {'空' if not allowed_users else f'{len(allowed_users)} 项'}",
            f"allowed_groups: {'空' if not allowed_groups else f'{len(allowed_groups)} 项'}",
            f"allow_private: {bool(self._config.get('allow_private', True))}",
            f"allow_admin: {bool(self._config.get('allow_admin', True))}",
        ]
        if reason:
            lines.append(f"判定: 拒绝 — {reason}")
        else:
            lines.append("判定: 允许")

        # Clock/window observability. /wbdiag stays OFFLINE: no Hub request is
        # made for the authorization verdict, so the Hub time is shown as
        # 未知（诊断不联网） unless a previous /wb already captured it.
        tz_name = self._validate_timezone()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - _validate_timezone already guards
            tz = ZoneInfo("Asia/Shanghai")
        local_now = datetime.now().astimezone()
        hub_now_utc = self._client.hub_now() if self._client is not None else None
        if hub_now_utc is not None:
            # Show how old the remembered capture is: /wbdiag is offline, so
            # the value is whatever the last /wb (or background check) saw.
            # Without the age a day-old clock reads like the current one.
            age = self._client.hub_now_age() if self._client is not None else None
            age_text = f"，采集于 {int(age)} 秒前" if age is not None else ""
            hub_text = hub_now_utc.astimezone(tz).isoformat() + age_text
        else:
            hub_text = "未知（诊断不联网）"
        since, until = day_bounds(local_now, tz_name)
        since_local = (
            datetime.fromtimestamp(since, tz) if hub_now_utc is None
            else hub_now_utc.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        until_local = (
            datetime.fromtimestamp(until, tz) if hub_now_utc is None
            else hub_now_utc.astimezone(tz)
        )
        minutes = int((until_local.timestamp() - since_local.timestamp()) // 60)
        lines.append(f"本机时间: {local_now.isoformat()}")
        lines.append(f"Hub 时间: {hub_text}")
        lines.append(
            f"查询窗口: {since_local.isoformat()} .. {until_local.isoformat()}"
            f"（{minutes} 分钟）"
        )

        # Gateway update notice state — OFFLINE (no Hub request is made here;
        # the value was captured by a previous /wb or background check).
        if not self._cfg_update_notice_enabled():
            lines.append(DIAG_UPDATE_OFF)
        elif self._update_notice:
            # The stored notice is the full report sentence
            # (网关有更新，最新版本号v{X.Y.Z}); the diag line shows only the
            # version, e.g. 网关更新: 有 v1.7.25.
            prefix = "网关有更新，最新版本号v"
            version = self._update_notice[len(prefix):] if self._update_notice.startswith(prefix) else ""
            lines.append(f"网关更新: 有 v{version}" if version else DIAG_UPDATE_NONE)
        else:
            lines.append(DIAG_UPDATE_NONE)

        yield event.plain_result("\n".join(lines))
        self._stop(event)

    @staticmethod
    def _stop(event: AstrMessageEvent) -> None:
        """Stop event propagation so the LLM pipeline does not also respond."""
        try:
            event.stop_event()
        except Exception:  # noqa: BLE001 - optional on some platforms
            pass
