"""Tests for the WorkBuddy panel client against a local aiohttp stub server.

All tests run fully offline. No live hub is contacted.
"""
from __future__ import annotations

import json
import time

import pytest

from astrbot_plugin_workbuddy_usage.client import (
    HubAuthError,
    HubProtocolError,
    HubRateLimitError,
    HubUnavailableError,
    WorkBuddyClient,
)
from conftest import make_analytics, make_stat

pytestmark = pytest.mark.asyncio


def make_client(hub, session, **kw) -> WorkBuddyClient:
    return WorkBuddyClient(
        base_url=hub.base_url,
        panel_password=kw.get("password", hub.password),
        panel_token=kw.get("token", ""),
        timeout_seconds=kw.get("timeout", 5.0),
        max_response_bytes=kw.get("max_bytes", 5 * 1024 * 1024),
        session=session,
        proxy=kw.get("proxy", ""),
    )


class TestLogin:
    async def test_login_success_sends_password_json(self, stub_hub, session):
        client = make_client(stub_hub, session, token="")
        token = await client.login()
        assert token.startswith("tok-")
        assert stub_hub.login_calls == [{"password": stub_hub.password}]

    async def test_login_invalid_password_401(self, stub_hub, session):
        client = make_client(stub_hub, session, password="wrong-password")
        with pytest.raises(HubAuthError):
            await client.login()
        # wrong password must NOT be retried
        assert len(stub_hub.login_calls) == 1

    async def test_login_connect_failure(self, session):
        client = WorkBuddyClient(
            base_url="http://127.0.0.1:1",  # nothing listens here
            panel_password="x",
            panel_token="",
            timeout_seconds=1.0,
            max_response_bytes=65536,
            session=session,
        )
        with pytest.raises(HubUnavailableError):
            await client.login()


class TestAnalytics:
    async def test_fetch_uses_panel_token_and_custom_window(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        since, until = 1700000000, 1700086399
        payload = await client.fetch_analytics(since, until)
        call = stub_hub.analytics_calls[-1]
        assert call["token"] == "preexisting-token"
        assert call["realm"] == "all"
        assert call["range"] == "custom"
        assert call["since"] == str(since)
        assert call["until"] == str(until)
        assert payload["summary"]["window"]["total_tokens"] == 0
        # no conditional headers in v1
        # (stub records none; absence of If-None-Match is implied by aiohttp defaults)

    async def test_expired_static_token_renews_once(self, stub_hub, session):
        # give a stale token that the stub does not know
        client = make_client(stub_hub, session, token="stale-token")
        payload = await client.fetch_analytics(1.0, 2.0)
        # one renewal login happened, then replay succeeded
        assert len(stub_hub.login_calls) == 1
        assert stub_hub.analytics_calls[-1]["token"].startswith("tok-")
        assert payload["realm"] == "all"

    async def test_bearer_only_refused(self, stub_hub, session):
        # no password and no token: must fail without any HTTP call
        client = make_client(stub_hub, session, token="", password="")
        with pytest.raises(HubAuthError):
            await client.fetch_analytics(1.0, 2.0)
        assert stub_hub.analytics_calls == []
        assert stub_hub.login_calls == []

    async def test_429_rate_limit(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.analytics_status = 429
        with pytest.raises(HubRateLimitError):
            await client.fetch_analytics(1.0, 2.0)

    async def test_500_unavailable(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.analytics_status = 500
        with pytest.raises(HubUnavailableError):
            await client.fetch_analytics(1.0, 2.0)

    async def test_invalid_json(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.body_bytes = b"not-json{"
        with pytest.raises(HubProtocolError):
            await client.fetch_analytics(1.0, 2.0)

    async def test_missing_summary(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        stub_hub.analytics_body = {"accounts": [], "models": []}
        with pytest.raises(HubProtocolError):
            await client.fetch_analytics(1.0, 2.0)

    async def test_missing_credit_rejected_not_zeroed(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        bad = make_analytics(summary_window=make_stat(total_tokens=5))
        del bad["summary"]["window"]["credit"]
        stub_hub.analytics_body = bad
        with pytest.raises(HubProtocolError):
            await client.fetch_analytics(1.0, 2.0)

    async def test_oversized_response_rejected(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token", max_bytes=100)
        stub_hub.body_bytes = json.dumps(make_analytics()).encode() * 100
        with pytest.raises(HubProtocolError):
            await client.fetch_analytics(1.0, 2.0)

    async def test_nonintegral_tokens_rejected(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        bad = make_analytics(summary_window=make_stat(total_tokens=1.5, credit=1))
        stub_hub.analytics_body = bad
        with pytest.raises(HubProtocolError):
            await client.fetch_analytics(1.0, 2.0)

    async def test_bool_as_int_rejected(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        bad = make_analytics()
        bad["summary"]["window"]["requests"] = True
        stub_hub.analytics_body = bad
        with pytest.raises(HubProtocolError):
            await client.fetch_analytics(1.0, 2.0)


class TestChunkedStreaming:
    """Regression: aiohttp's StreamReader.read(n) with a large n returns
    early with only what is currently buffered (an exact multiple of the
    internal buffer), truncating multi-KB JSON bodies. _read_json must
    read to EOF in bounded chunks instead."""

    @staticmethod
    def _large_payload() -> bytes:
        # Build a ~200 KB analytics payload: 600 accounts + 600 models.
        accounts = [
            {"uid": f"u{i}", "nickname": f"账号{i}", "realm": "intl",
             "window": make_stat(total_tokens=i, credit=1.5),
             "all_time": make_stat(total_tokens=2 * i, credit=3.0),
             "credits": {"balance": 1.0}}
            for i in range(600)
        ]
        models = [
            {"model": f"model-{i}", "window": make_stat(total_tokens=i, credit=0.5),
             "all_time": make_stat(total_tokens=i, credit=0.5)}
            for i in range(600)
        ]
        payload = make_analytics(accounts, models)
        return json.dumps(payload).encode("utf-8")

    async def test_streamed_body_read_to_eof(self, stub_hub, session):
        body = self._large_payload()
        assert len(body) > 3 * 65536  # spans several read chunks
        stub_hub.stream_body = body
        stub_hub.stream_chunks = 8
        client = make_client(stub_hub, session, token="preexisting-token")
        url = f"{stub_hub.base_url}/usage/analytics_streamed"
        async with session.get(url, headers={"X-Panel-Token": "preexisting-token"}) as resp:
            resp.raise_for_status = lambda: None  # not used; status checked below
            assert resp.status == 200
            assert resp.headers["Content-Length"] == str(len(body))
            payload = await client._read_json(resp)
        # COMPLETE payload parsed: all accounts/models present
        assert len(payload["accounts"]) == 600
        assert len(payload["models"]) == 600
        assert payload["accounts"][599]["uid"] == "u599"
        assert payload["summary"]["window"]["total_tokens"] == 0

    async def test_streamed_body_over_cap_rejected(self, stub_hub, session):
        # streamed body must still be capped by max_response_bytes
        body = self._large_payload()
        stub_hub.stream_body = body
        stub_hub.stream_chunks = 8
        client = make_client(stub_hub, session, token="preexisting-token",
                             max_bytes=len(body) // 2)
        url = f"{stub_hub.base_url}/usage/analytics_streamed"
        async with session.get(url, headers={"X-Panel-Token": "preexisting-token"}) as resp:
            assert resp.status == 200
            with pytest.raises(HubProtocolError, match="max_response_bytes"):
                await client._read_json(resp)


class TestLogoutAndLifecycle:
    async def test_logout_best_effort_after_session_close(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        await session.close()  # already closed: logout must not raise
        await client.logout()
        assert stub_hub.logout_calls == []

    async def test_logout_sends_token(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        await client.logout()
        assert stub_hub.logout_calls == [{"token": "preexisting-token"}]

    async def test_logout_401_swallowed(self, stub_hub, session):
        client = make_client(stub_hub, session, token="invalid-token")
        await client.logout()  # must not raise
        assert len(stub_hub.logout_calls) == 1


class TestSchemaStrictness:
    async def test_full_stat_fields_accepted(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        payload = await client.fetch_analytics(1.0, 2.0)
        win = payload["summary"]["window"]
        for field in (
            "requests", "errors", "prompt_tokens", "completion_tokens",
            "reasoning_tokens", "cached_tokens", "total_tokens", "credit",
            "cost_cny", "ttft_sum", "ttft_n", "speed_sum", "speed_n",
            "elapsed_sum", "elapsed_n", "cache_hit_pct", "reasoning_ratio",
            "ttft_ms_avg", "speed_avg", "elapsed_ms_avg",
        ):
            assert field in win

    async def test_extra_fields_tolerated(self, stub_hub, session):
        client = make_client(stub_hub, session, token="preexisting-token")
        body = make_analytics()
        body["some_future_field"] = {"x": 1}
        body["accounts"] = [{"uid": "u9", "nickname": "n", "brand_new_field": True,
                             "window": make_stat(total_tokens=1, credit=0.5)}]
        stub_hub.analytics_body = body
        payload = await client.fetch_analytics(1.0, 2.0)
        assert payload["accounts"][0]["uid"] == "u9"


class TestProxy:
    """Proxy plumbing: stored on the client, threaded into aiohttp requests,
    normalized so an empty string means 'no proxy' (None)."""

    async def test_proxy_stored_and_used_in_request(self, stub_hub, session, monkeypatch):
        seen: list = []
        original_post = session.post

        def spy_post(url, **kw):
            seen.append(kw.get("proxy"))
            kw.pop("proxy", None)  # prove it is passed, do not actually route through it
            return original_post(url, **kw)

        monkeypatch.setattr(session, "post", spy_post)
        # The placeholder proxy address is documentation only and is never dialled.
        client = make_client(stub_hub, session, proxy="http://127.0.0.1:8080")
        assert client._proxy == "http://127.0.0.1:8080"
        token = await client.login()
        assert token.startswith("tok-")
        assert seen == ["http://127.0.0.1:8080"]

    async def test_proxy_threads_into_get_and_logout(self, stub_hub, session, monkeypatch):
        seen: list = []
        for method in ("get", "post"):
            original = getattr(session, method)

            def make_spy(orig, m):
                def spy(url, **kw):
                    seen.append((m, kw.get("proxy")))
                    kw.pop("proxy", None)  # assert it was passed; do NOT dial it (keeps the test hermetic)
                    return orig(url, **kw)
                return spy

            monkeypatch.setattr(session, method, make_spy(original, method))
        # The proxy address below must NOT be reachable; the test only proves
        # the client threads it into the aiohttp call.
        client = make_client(stub_hub, session, token="preexisting-token",
                             proxy="http://127.0.0.1:8080")
        await client.fetch_analytics(1.0, 2.0)
        await client.logout()
        assert ("get", "http://127.0.0.1:8080") in seen
        assert ("post", "http://127.0.0.1:8080") in seen

    async def test_empty_proxy_becomes_none_not_empty_string(self, stub_hub, session):
        client = make_client(stub_hub, session, proxy="")
        assert client._proxy is None
        # works end-to-end with no proxy set
        token = await client.login()
        assert token.startswith("tok-")

    async def test_default_no_proxy_works(self, stub_hub, session):
        client = make_client(stub_hub, session)  # proxy kwarg omitted entirely
        assert client._proxy is None
        payload = await client.fetch_analytics(1.0, 2.0)
        assert payload["summary"]["window"]["total_tokens"] == 0
