"""Asynchronous WorkBuddy Hub panel client (aiohttp only, never requests).

Responsibilities:
- login via POST /panel/login and keep the session token in memory only;
- fetch GET /usage/analytics with X-Panel-Token, validating the schema;
- optional logout (lifecycle cleanup; failures never block session close).

Error taxonomy: HubError (base), HubAuthError (401), HubRateLimitError (429),
HubUnavailableError (transport / 5xx), HubProtocolError (schema / oversize).
Secrets (password/token) are never included in exception messages or logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone as dt_timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

import aiohttp

from .models import (
    AccountAnalytics,
    AccountsPayload,
    AnalyticsPayload,
    Distribution,
    ModelAnalytics,
    Stat,
)

logger = logging.getLogger("astrbot.workbuddy_usage")

_MAX_LOGIN_ATTEMPTS_ON_401 = 1  # static token 401 gets exactly one login/replay
# Chunk size for reading response bodies. A single large read(n) on
# aiohttp's StreamReader returns early with whatever is currently buffered
# (an exact multiple of the internal buffer), truncating multi-KB JSON
# bodies; reading to EOF in fixed chunks is deterministic.
_READ_CHUNK = 65536


class HubError(Exception):
    """Base error for all WorkBuddy Hub failures."""


class HubAuthError(HubError):
    """Authentication failed (HTTP 401 or login rejection)."""


class HubRateLimitError(HubError):
    """Hub returned HTTP 429 (login rate limit or request throttling)."""


class HubUnavailableError(HubError):
    """Network/transport failure or server 5xx."""


class HubProtocolError(HubError):
    """Response could not be parsed / failed schema validation / oversized."""


def _to_decimal(value: Any, where: str) -> Decimal:
    """Convert a JSON number to Decimal exactly; reject bool/NaN/inf/None."""
    if isinstance(value, bool) or value is None:
        raise HubProtocolError(f"invalid numeric field: {where}")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HubProtocolError(f"invalid numeric field: {where}") from exc
    if not d.is_finite():
        raise HubProtocolError(f"non-finite numeric field: {where}")
    return d


def _to_int(value: Any, where: str) -> int:
    """Convert a JSON number to int; reject bools and nonintegral values."""
    if isinstance(value, bool) or value is None:
        raise HubProtocolError(f"invalid integer field: {where}")
    if isinstance(value, int):
        return value
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HubProtocolError(f"invalid integer field: {where}") from exc
    if not d.is_finite() or d != d.to_integral_value():
        raise HubProtocolError(f"nonintegral integer field: {where}")
    return int(d)


def _to_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or value is None:
        raise HubProtocolError(f"invalid float field: {where}")
    d = _to_decimal(value, where)
    return float(d)


def _validate_stat(raw: Any, where: str) -> Stat:
    """Validate a full Stat record; all mandatory fields must be present."""
    if not isinstance(raw, Mapping):
        raise HubProtocolError(f"stat is not an object: {where}")
    stat: Stat = {}
    # Mandatory numeric fields, validated strictly.
    stat["requests"] = _to_int(raw.get("requests"), f"{where}.requests")
    stat["errors"] = _to_int(raw.get("errors"), f"{where}.errors")
    stat["prompt_tokens"] = _to_int(raw.get("prompt_tokens"), f"{where}.prompt_tokens")
    stat["completion_tokens"] = _to_int(
        raw.get("completion_tokens"), f"{where}.completion_tokens"
    )
    stat["reasoning_tokens"] = _to_int(
        raw.get("reasoning_tokens"), f"{where}.reasoning_tokens"
    )
    stat["cached_tokens"] = _to_int(raw.get("cached_tokens"), f"{where}.cached_tokens")
    stat["total_tokens"] = _to_int(raw.get("total_tokens"), f"{where}.total_tokens")
    # credit is mandatory and must never be null/missing: it is never defaulted
    # to zero because that would silently rewrite missing data.
    if "credit" not in raw or raw.get("credit") is None:
        raise HubProtocolError(f"missing credit: {where}")
    stat["credit"] = _to_decimal(raw["credit"], f"{where}.credit")
    # Optional / derived fields when present.
    if raw.get("cost_cny") is not None:
        stat["cost_cny"] = _to_decimal(raw["cost_cny"], f"{where}.cost_cny")
    if raw.get("ttft_sum") is not None:
        stat["ttft_sum"] = _to_float(raw["ttft_sum"], f"{where}.ttft_sum")
    if raw.get("ttft_n") is not None:
        stat["ttft_n"] = _to_int(raw["ttft_n"], f"{where}.ttft_n")
    if raw.get("speed_sum") is not None:
        stat["speed_sum"] = _to_float(raw["speed_sum"], f"{where}.speed_sum")
    if raw.get("speed_n") is not None:
        stat["speed_n"] = _to_int(raw["speed_n"], f"{where}.speed_n")
    if raw.get("elapsed_sum") is not None:
        stat["elapsed_sum"] = _to_float(raw["elapsed_sum"], f"{where}.elapsed_sum")
    if raw.get("elapsed_n") is not None:
        stat["elapsed_n"] = _to_int(raw["elapsed_n"], f"{where}.elapsed_n")
    for key in ("cache_hit_pct", "reasoning_ratio", "ttft_ms_avg", "speed_avg", "elapsed_ms_avg"):
        if raw.get(key) is not None:
            stat[key] = _to_float(raw[key], f"{where}.{key}")  # type: ignore[literal-required]
    return stat


def _validate_distribution(raw: Any, where: str) -> Distribution:
    """Validate a Distribution record. Note: Distribution has NO credit field."""
    if not isinstance(raw, Mapping):
        raise HubProtocolError(f"distribution is not an object: {where}")
    dist: Distribution = {}
    dist["requests"] = _to_int(raw.get("requests"), f"{where}.requests")
    dist["tokens"] = _to_int(raw.get("tokens"), f"{where}.tokens")
    if raw.get("reasoning") is not None:
        dist["reasoning"] = _to_int(raw["reasoning"], f"{where}.reasoning")
    if raw.get("cost_cny") is not None:
        dist["cost_cny"] = _to_decimal(raw["cost_cny"], f"{where}.cost_cny")
    return dist


def _validate_account(raw: Any, where: str) -> AccountAnalytics:
    if not isinstance(raw, Mapping):
        raise HubProtocolError(f"account is not an object: {where}")
    uid = raw.get("uid")
    if not isinstance(uid, str) or not uid:
        raise HubProtocolError(f"invalid uid: {where}")
    acct: AccountAnalytics = {"uid": uid}
    nickname = raw.get("nickname")
    acct["nickname"] = nickname if isinstance(nickname, str) else ""
    realm = raw.get("realm")
    acct["realm"] = realm if isinstance(realm, str) else ""
    domain = raw.get("domain")
    acct["domain"] = domain if isinstance(domain, str) else ""
    if raw.get("window") is not None:
        acct["window"] = _validate_stat(raw["window"], f"{where}.window")
    if raw.get("all_time") is not None:
        acct["all_time"] = _validate_stat(raw["all_time"], f"{where}.all_time")
    for key in ("window_models", "all_models"):
        block = raw.get(key)
        if block is None:
            continue
        if not isinstance(block, Mapping):
            raise HubProtocolError(f"{key} is not an object: {where}")
        parsed: dict[str, Distribution] = {}
        for model, dist in block.items():
            parsed[str(model)] = _validate_distribution(dist, f"{where}.{key}.{model}")
        acct[key] = parsed  # type: ignore[typeddict-item]
    # Optional credits object: presence marks a current pool account.
    if raw.get("credits") is not None:
        acct["credits"] = raw["credits"]
    return acct


def _validate_model(raw: Any, where: str) -> ModelAnalytics:
    if not isinstance(raw, Mapping):
        raise HubProtocolError(f"model analytics is not an object: {where}")
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise HubProtocolError(f"invalid model id: {where}")
    parsed: ModelAnalytics = {"model": model}
    if raw.get("window") is not None:
        parsed["window"] = _validate_stat(raw["window"], f"{where}.window")
    if raw.get("all_time") is not None:
        parsed["all_time"] = _validate_stat(raw["all_time"], f"{where}.all_time")
    return parsed


def validate_analytics(raw: Any) -> AnalyticsPayload:
    """Validate the complete analytics payload structure."""
    if not isinstance(raw, Mapping):
        raise HubProtocolError("analytics payload is not an object")
    summary = raw.get("summary")
    if not isinstance(summary, Mapping):
        raise HubProtocolError("missing summary")
    window_stat = _validate_stat(summary.get("window"), "summary.window")
    payload: AnalyticsPayload = {
        "summary": {"window": window_stat},  # type: ignore[typeddict-unknown-key]
        "accounts": [],
        "models": [],
    }
    all_time = summary.get("all_time")
    if all_time is not None:
        payload["summary"]["all_time"] = _validate_stat(all_time, "summary.all_time")
    accounts = raw.get("accounts")
    if not isinstance(accounts, list):
        raise HubProtocolError("accounts is not a list")
    payload["accounts"] = [
        _validate_account(a, f"accounts[{i}]") for i, a in enumerate(accounts)
    ]
    models = raw.get("models")
    if not isinstance(models, list):
        raise HubProtocolError("models is not a list")
    payload["models"] = [
        _validate_model(m, f"models[{i}]") for i, m in enumerate(models)
    ]
    if raw.get("realm") is not None:
        payload["realm"] = str(raw["realm"])
    return payload


def validate_accounts(raw: Any) -> AccountsPayload:
    """Validate the GET /accounts payload: top level object with a list.

    Deliberately LOOSE: per-account fields (breakerFor, modelCooldowns, ...)
    are validated defensively at render time (see reporting.status_segments),
    so an odd individual account must never reject the whole payload — its
    status is simply skipped. Only the structural minimum is enforced here.
    """
    if not isinstance(raw, Mapping):
        raise HubProtocolError("accounts payload is not an object")
    accounts = raw.get("accounts")
    if not isinstance(accounts, list):
        raise HubProtocolError("accounts is not a list")
    payload: AccountsPayload = {"accounts": []}
    kept: list[dict[str, Any]] = []
    for entry in accounts:
        # Tolerate any shape; only rows whose uid is a non-empty string are
        # usable (status rendering keys on uid). Others are skipped silently.
        if isinstance(entry, Mapping) and isinstance(entry.get("uid"), str) and entry["uid"]:
            kept.append(dict(entry))
    payload["accounts"] = kept
    if isinstance(raw.get("storage"), str):
        payload["storage"] = raw["storage"]
    usable = raw.get("usable")
    if isinstance(usable, int) and not isinstance(usable, bool):
        payload["usable"] = usable
    return payload


# Clock-skew threshold (seconds) above which the Hub-vs-local clock delta
# is logged as a warning (see _maybe_warn_clock_skew).
CLOCK_SKEW_WARN_SECONDS = 120


def parse_http_date_header(value: object) -> datetime | None:
    """Parse an RFC 7231 HTTP ``Date`` header into a tz-aware UTC datetime.

    Returns None when the value is missing or unparseable — never raises.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt_timezone.utc)


class WorkBuddyClient:
    """Panel client. One instance owns one long-lived aiohttp session."""

    def __init__(
        self,
        base_url: str,
        panel_password: str,
        panel_token: str,
        timeout_seconds: float,
        max_response_bytes: int,
        session: aiohttp.ClientSession,
        proxy: str = "",
    ) -> None:
        # Strip a single trailing slash so joins never produce '//' paths.
        self._base_url = base_url.rstrip("/")
        self._password = panel_password
        self._token = panel_token
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_bytes = max_response_bytes
        self._session = session
        # Empty proxy means direct connection: aiohttp rejects "", so map to None.
        self._proxy = proxy if proxy else None
        # Serializes login so concurrent commands do not race token renewal.
        self._login_lock = asyncio.Lock()
        # The Hub's own clock, captured from the RFC 7231 Date header of the
        # last response that carried one (login or analytics). The Hub
        # timestamps every usage row, so its clock — not the host process's —
        # is the authority for "today". None until first captured.
        self._hub_now: datetime | None = None

    # -- low level helpers ---------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _read_json(self, resp: aiohttp.ClientResponse) -> Any:
        """Read the whole body to EOF in bounded chunks, respecting
        max_response_bytes; parse as JSON.

        Why not a single `read(n)` with a large n: aiohttp's
        ``StreamReader.read(n)`` returns as soon as *any* data is buffered
        (up to n), it does NOT block until n bytes or EOF. Against a
        multi-KB JSON response this intermittently yields only the first
        internal chunk (an exact multiple of the buffer size), truncating
        the body so json.loads fails. Reading in fixed-size chunks until
        an empty chunk (EOF) is deterministic, and the running total keeps
        the max_response_bytes cap applied incrementally (never buffering
        an unbounded body first).
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await resp.content.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_bytes:
                raise HubProtocolError("response exceeds max_response_bytes")
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HubProtocolError("response is not valid JSON") from exc

    @staticmethod
    def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
        # Never include the HTTP body in errors: it could echo secrets/HTML.
        if resp.status == 401:
            raise HubAuthError("unauthorized")
        if resp.status == 429:
            raise HubRateLimitError("rate limited")
        if resp.status == 304:
            # v1 never sends conditional headers, so a 304 means the Hub
            # answered conditionally without our cache — a protocol error.
            raise HubProtocolError("unexpected 304")
        if resp.status >= 500:
            raise HubUnavailableError(f"server error {resp.status}")
        if resp.status >= 400:
            raise HubProtocolError(f"unexpected status {resp.status}")

    # -- public API ----------------------------------------------------------

    def hub_now(self) -> datetime | None:
        """The Hub's clock captured from an HTTP Date header, tz-aware UTC.

        None until a response with a parseable Date header has been seen
        (never raises; callers must fall back to the local clock).
        """
        return self._hub_now

    def _capture_hub_date(self, resp: aiohttp.ClientResponse) -> None:
        """Capture the Hub's Date header if present and parseable."""
        parsed = parse_http_date_header(resp.headers.get("Date"))
        if parsed is not None:
            self._hub_now = parsed

    async def login(self) -> str:
        """POST /panel/login; returns the session token (memory only)."""
        async with self._login_lock:
            return await self._login_once()

    async def _login_once(self) -> str:
        try:
            async with self._session.post(
                self._url("/panel/login"),
                json={"password": self._password},
                timeout=self._timeout,
                proxy=self._proxy,
            ) as resp:
                self._raise_for_status(resp)
                self._capture_hub_date(resp)
                data = await self._read_json(resp)
        except aiohttp.ClientError as exc:
            raise HubUnavailableError("login transport failure") from exc
        except asyncio.TimeoutError as exc:
            raise HubUnavailableError("login timeout") from exc
        if not isinstance(data, Mapping) or data.get("ok") is not True:
            raise HubProtocolError("login response missing ok=true")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise HubProtocolError("login response missing token")
        self._token = token
        return token

    async def fetch_analytics(self, since: float, until: float) -> AnalyticsPayload:
        """GET /usage/analytics with realm=all and a custom epoch window.

        If a statically configured token is rejected (401) and a password is
        available, exactly one login+replay is attempted. A wrong password is
        never retried.
        """
        for attempt in range(_MAX_LOGIN_ATTEMPTS_ON_401 + 1):
            if not self._token:
                if not self._password:
                    raise HubAuthError("no credentials configured")
                await self._login_once()
            params = {
                "realm": "all",
                "range": "custom",
                "since": str(int(since)),
                "until": str(int(until)),
            }
            headers = {"X-Panel-Token": self._token}
            try:
                async with self._session.get(
                    self._url("/usage/analytics"),
                    params=params,
                    headers=headers,
                    timeout=self._timeout,
                    proxy=self._proxy,
                ) as resp:
                    if resp.status == 401 and attempt == 0 and self._password:
                        # Token likely expired: renew once and replay.
                        self._token = ""
                        await self._login_once()
                        continue
                    self._raise_for_status(resp)
                    self._capture_hub_date(resp)
                    raw = await self._read_json(resp)
            except aiohttp.ClientError as exc:
                raise HubUnavailableError("analytics transport failure") from exc
            except asyncio.TimeoutError as exc:
                raise HubUnavailableError("analytics timeout") from exc
            return validate_analytics(raw)
        raise HubAuthError("authentication failed after token renewal")

    async def fetch_accounts(self) -> AccountsPayload:
        """GET /accounts?realm=all — per-account live state (cooldowns etc.).

        The response's RFC 7231 Date header updates ``self._hub_now``: the
        relative-seconds fields (cooldownFor / breakerFor / degradeFor) must
        be converted to absolute recovery times against the Hub's clock at
        the moment THIS response was generated.

        Same error taxonomy / chunked reader / caps / proxy handling as
        fetch_analytics, including the one-shot 401 login+replay.
        """
        for attempt in range(_MAX_LOGIN_ATTEMPTS_ON_401 + 1):
            if not self._token:
                if not self._password:
                    raise HubAuthError("no credentials configured")
                await self._login_once()
            headers = {"X-Panel-Token": self._token}
            try:
                async with self._session.get(
                    self._url("/accounts"),
                    params={"realm": "all"},
                    headers=headers,
                    timeout=self._timeout,
                    proxy=self._proxy,
                ) as resp:
                    if resp.status == 401 and attempt == 0 and self._password:
                        # Token likely expired: renew once and replay.
                        self._token = ""
                        await self._login_once()
                        continue
                    self._raise_for_status(resp)
                    self._capture_hub_date(resp)
                    raw = await self._read_json(resp)
            except aiohttp.ClientError as exc:
                raise HubUnavailableError("accounts transport failure") from exc
            except asyncio.TimeoutError as exc:
                raise HubUnavailableError("accounts timeout") from exc
            return validate_accounts(raw)
        raise HubAuthError("authentication failed after token renewal")

    async def logout(self) -> None:
        """Best-effort logout; failures are swallowed and never raise."""
        if not self._token or self._session.closed:
            return
        try:
            async with self._session.post(
                self._url("/panel/logout"),
                json={},
                headers={"X-Panel-Token": self._token},
                timeout=self._timeout,
                proxy=self._proxy,
            ) as resp:
                # 401/429/etc on logout are irrelevant to correctness.
                await resp.content.read(0)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.debug("workbuddy logout failed (ignored)")
