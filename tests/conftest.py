"""Shared test fixtures: source-shaped payload builders and an aiohttp stub server.

Also installs a minimal offline stub of the ``astrbot`` API package so the
plugin modules (and test_access) can be imported without a real AstrBot
install. The stub only implements the surface the plugin actually uses.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import types
from typing import Any, Mapping

import pytest
import pytest_asyncio
import aiohttp
from aiohttp import web

# Put the directory that CONTAINS the plugin package on sys.path so
# `import astrbot_plugin_workbuddy_usage` resolves, and the plugin directory
# itself so the `from conftest import ...` helper imports resolve —
# regardless of the working directory pytest is invoked from.
# On a real AstrBot install the package parent is data/plugins/, where the
# plugin package lives.
_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_PARENT = _PLUGIN_DIR.parent
for _entry in (str(_PACKAGE_PARENT), str(_PLUGIN_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# ---------------------------------------------------------------------------
# Offline stub of astrbot.api / astrbot.api.star / astrbot.api.event
# ---------------------------------------------------------------------------


def _install_astrbot_stub() -> None:
    if "astrbot.api" in sys.modules:
        return

    def _module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    api = _module("astrbot")
    api_mod = _module("astrbot.api")
    star_mod = _module("astrbot.api.star")
    event_mod = _module("astrbot.api.event")

    import logging

    class AstrBotConfig(dict):
        """Config stand-in; AstrBot injects a dict-like schema-backed object."""

    class Context:  # noqa: D401 - placeholder type for typing only
        pass

    class Star:
        def __init__(self, context: Any = None) -> None:
            self.context = context

    class MessageEventResult:
        def __init__(self, chain: Any = None) -> None:
            self.chain = chain

    class AstrMessageEvent:
        """Event stand-in used by tests (see tests/test_access.py)."""

        def __init__(self) -> None:
            self.stopped = False

        def stop_event(self) -> None:
            self.stopped = True

    def filter_command(name: str):  # noqa: ANN201 - decorator stub
        def deco(fn):  # noqa: ANN001
            fn._command_name = name
            return fn

        return deco

    class _Filter:  # namespace mimicking astrbot.api.event.filter
        command = staticmethod(filter_command)

    api_mod.AstrBotConfig = AstrBotConfig
    api_mod.logger = logging.getLogger("astrbot.stub")
    star_mod.Context = Context
    star_mod.Star = Star
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageEventResult = MessageEventResult
    event_mod.filter = _Filter
    star_mod.filter = _Filter


_install_astrbot_stub()

# ---------------------------------------------------------------------------
# Source-shaped builders
# ---------------------------------------------------------------------------


def make_stat(**overrides: Any) -> dict[str, Any]:
    """Complete Stat record with all fields from wb_proxy.py 6176-6186."""
    stat: dict[str, Any] = {
        "requests": 0,
        "errors": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "credit": 0.0,
        "cost_cny": 0.0,
        "ttft_sum": 0.0,
        "ttft_n": 0,
        "speed_sum": 0.0,
        "speed_n": 0,
        "elapsed_sum": 0.0,
        "elapsed_n": 0,
        "cache_hit_pct": 0.0,
        "reasoning_ratio": 0.0,
        "ttft_ms_avg": 0.0,
        "speed_avg": 0.0,
        "elapsed_ms_avg": 0.0,
    }
    stat.update(overrides)
    return stat


def make_distribution(**overrides: Any) -> dict[str, Any]:
    """Complete Distribution record (requests/tokens/reasoning/cost_cny — no credit)."""
    dist: dict[str, Any] = {
        "requests": 0,
        "tokens": 0,
        "reasoning": 0,
        "cost_cny": 0.0,
    }
    dist.update(overrides)
    return dist


def make_account(
    uid: str,
    nickname: str,
    realm: str = "intl",
    window: Mapping[str, Any] | None = None,
    window_models: Mapping[str, Any] | None = None,
    with_credits: bool = True,
    remain: Any = 500.0,
) -> dict[str, Any]:
    acct: dict[str, Any] = {
        "uid": uid,
        "nickname": nickname,
        "realm": realm,
        "domain": "",
        "window": dict(window or make_stat()),
        "all_time": make_stat(),
        "window_models": dict(window_models or {}),
        "all_models": dict(window_models or {}),
    }
    if with_credits:
        # Mirrors the live Hub: `remain` is the account's remaining credit
        # balance; `used` is a cumulative package figure (never today's usage).
        credits: dict[str, Any] = {"balance": 100.0}
        if remain is not None:
            credits["remain"] = remain
        acct["credits"] = credits
    return acct


def make_model(model: str, window: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "model": model,
        "window": dict(window or make_stat()),
        "all_time": make_stat(),
    }


def make_analytics(
    accounts: list[dict[str, Any]] | None = None,
    models: list[dict[str, Any]] | None = None,
    summary_window: Mapping[str, Any] | None = None,
    since: float = 1700000000.0,
    until: float = 1700086399.0,
) -> dict[str, Any]:
    """Full analytics payload with complete summary.window / all_time Stats."""
    return {
        "window": {"since": since, "until": until},
        "realm": "all",
        "usd_cny": 7.2,
        "summary": {
            "window": dict(summary_window or make_stat()),
            "all_time": make_stat(),
        },
        "accounts": list(accounts or []),
        "models": list(models or []),
        "keys": [],
    }


@pytest.fixture()
def known_payload() -> dict[str, Any]:
    """Known fixture: two current accounts (one zero), one historical
    (no credits) account, two exact model names, a billed failed request
    contribution and a client-aborted record (excluded by the Hub itself)."""
    a1_win = make_stat(
        requests=10,
        errors=1,  # billed failed record included by the Hub
        prompt_tokens=700,
        completion_tokens=400,
        reasoning_tokens=50,
        cached_tokens=100,
        total_tokens=1200,
        credit=2.5,
        cost_cny=0.6,
        ttft_sum=900.0,
        ttft_n=9,
        speed_sum=90.0,
        speed_n=9,
        elapsed_sum=45000.0,
        elapsed_n=9,
        cache_hit_pct=8.33,
        reasoning_ratio=0.0417,
        ttft_ms_avg=100.0,
        speed_avg=10.0,
        elapsed_ms_avg=5000.0,
    )
    a2_win = make_stat()  # zero-traffic current account
    hist_win = make_stat(requests=3, total_tokens=300, credit=0.75)  # historical row

    accounts = [
        make_account("u1", "账号一", "intl", a1_win, {"model-a": make_distribution(requests=8, tokens=1000, reasoning=50, cost_cny=0.5), "model-b": make_distribution(requests=2, tokens=200, cost_cny=0.1)}),
        make_account("u2", "账号二", "cn", a2_win, {}),
        make_account("u3", "历史账号", "intl", hist_win, {}, with_credits=False),
    ]
    # client-aborted record is NOT present: the Hub excludes it (verified source).
    # Per-model windows exclude the billed error row (Hub fold skips errors),
    # so model totals (1500) are legitimately below the summary (1500 - billed
    # error tokens). Here the billed error consumed 200 tokens / 0.25 credit,
    # all attributed to model-a in the account fold but absent from models[].
    models = [
        make_model("model-a", make_stat(requests=9, total_tokens=900, credit=1.75)),
        make_model("model-b", make_stat(requests=4, total_tokens=400, credit=1.25)),
    ]
    # summary folds the SAME rows as accounts[] (wb_proxy.py _feed_analytics),
    # so the account axis must match EXACTLY. The model axis legitimately
    # totals LESS than the summary because the Hub's per-model fold skips
    # billed error rows (wb_proxy.py _bump_analytics_models: `if is_err: return`).
    summary = make_stat(
        requests=17,
        errors=1,
        total_tokens=1200 + 0 + 300,  # 1500 = sum of account windows
        credit=2.5 + 0.0 + 0.75,  # 3.25 = sum of account windows
        cost_cny=0.6 + 0.75,
    )
    return make_analytics(accounts, models, summary)


# ---------------------------------------------------------------------------
# aiohttp stub Hub server
# ---------------------------------------------------------------------------


class StubHub:
    """Minimal offline WorkBuddy Hub stub covering login/analytics/logout."""

    def __init__(self) -> None:
        self.password = "dummy-password"
        self.valid_tokens: set[str] = {"preexisting-token"}
        self.login_calls: list[Mapping[str, Any]] = []
        self.analytics_calls: list[Mapping[str, Any]] = []
        self.logout_calls: list[Mapping[str, Any]] = []
        self.payload: dict[str, Any] = make_analytics()
        # behaviour knobs
        self.analytics_status = 200
        self.analytics_body: Any = None  # override raw body (e.g. invalid JSON)
        self.body_bytes: bytes | None = None  # override fully (e.g. oversize)
        self.stream_body: bytes | None = None  # stream in chunks w/ yield points
        self.stream_chunks = 4  # number of pieces to write for stream_body
        self.fail_connect = False
        # /accounts endpoint knobs (v1.0.4)
        self.accounts_body: Any = None
        self.accounts_status = 200
        self.accounts_date: str | None = None
        self.accounts_calls: list[Mapping[str, Any]] = []

    async def start(self, aiohttp_client_factory=None) -> None:
        app = web.Application()
        app.router.add_post("/panel/login", self._login)
        app.router.add_get("/usage/analytics", self._analytics)
        app.router.add_get("/accounts", self._accounts)
        app.router.add_get("/usage/analytics_streamed", self._analytics_streamed)
        app.router.add_post("/panel/logout", self._logout)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        # retrieve the actually bound port
        assert self._runner.addresses
        host, port = self._runner.addresses[0][:2]
        self.base_url = f"http://{host}:{port}"

    async def stop(self) -> None:
        await self._runner.cleanup()

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.login_calls.append(body)
        if body.get("password") != self.password:
            return web.json_response({"ok": False}, status=401)
        token = f"tok-{len(self.valid_tokens) + 1}"
        self.valid_tokens.add(token)
        return web.json_response({"ok": True, "token": token, "using_default_password": False})

    async def _analytics(self, request: web.Request) -> web.Response:
        self.analytics_calls.append(
            {
                "token": request.headers.get("X-Panel-Token"),
                "realm": request.query.get("realm"),
                "range": request.query.get("range"),
                "since": request.query.get("since"),
                "until": request.query.get("until"),
            }
        )
        if request.headers.get("X-Panel-Token") not in self.valid_tokens:
            return web.json_response({"ok": False}, status=401)
        if self.fail_connect:
            raise web.HTTPServiceUnavailable()
        if self.body_bytes is not None:
            return web.Response(body=self.body_bytes, content_type="application/json")
        if self.analytics_body is not None:
            return web.json_response(self.analytics_body, status=self.analytics_status)
        return web.json_response(self.payload, status=self.analytics_status)

    async def _accounts(self, request: web.Request) -> web.Response:
        self.accounts_calls.append(
            {
                "token": request.headers.get("X-Panel-Token"),
                "realm": request.query.get("realm"),
            }
        )
        if request.headers.get("X-Panel-Token") not in self.valid_tokens:
            return web.json_response({"ok": False}, status=401)
        headers = {"Date": self.accounts_date} if self.accounts_date else {}
        body = self.accounts_body if self.accounts_body is not None else {"accounts": []}
        return web.json_response(body, status=self.accounts_status, headers=headers)

    async def _analytics_streamed(self, request: web.Request) -> web.StreamResponse:
        """Serve self.stream_body as N chunks with a yield point between
        writes, Content-Length set to the FULL length. Deterministically
        reproduces the aiohttp `read(n)` early-return truncation."""
        self.analytics_calls.append({"token": request.headers.get("X-Panel-Token")})
        if request.headers.get("X-Panel-Token") not in self.valid_tokens:
            return web.json_response({"ok": False}, status=401)
        assert self.stream_body is not None
        resp = web.StreamResponse()
        resp.content_length = len(self.stream_body)
        resp.content_type = "application/json"
        await resp.prepare(request)
        n = max(1, self.stream_chunks)
        size = max(1, len(self.stream_body) // n)
        for i in range(0, len(self.stream_body), size):
            await resp.write(self.stream_body[i : i + size])
            # yield to the event loop so the client's read() sees a partial
            # buffer — the condition that triggers the early return
            await asyncio.sleep(0)
        await resp.write_eof()
        return resp

    async def _logout(self, request: web.Request) -> web.Response:
        self.logout_calls.append({"token": request.headers.get("X-Panel-Token")})
        return web.json_response({"ok": True})


@pytest_asyncio.fixture()
async def stub_hub():
    hub = StubHub()
    await hub.start()
    yield hub
    await hub.stop()


@pytest_asyncio.fixture()
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def run(coro: Any) -> Any:
    """Helper for optional sync execution in non-asyncio tests."""
    return asyncio.run(coro)
