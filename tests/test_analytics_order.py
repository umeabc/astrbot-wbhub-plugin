"""Analytics authentication, conversion, and replay ordering contracts."""
import pytest

from astrbot_plugin_workbuddy_usage.client import HubAuthError, WorkBuddyClient


class Epoch:
    def __init__(self, convert):
        self.convert = convert

    def __int__(self):
        return self.convert()


def make_client(hub, session, token="", password=None):
    return WorkBuddyClient(
        hub.base_url, hub.password if password is None else password,
        token, 5.0, 65536, session,
    )


@pytest.mark.parametrize(("token", "expected"), [
    ("", ["login", "since", "until"]),
    ("preexisting-token", ["since", "until"]),
    ("stale-token", ["since", "until", "login", "since", "until"]),
])
async def test_window_rebuilt_after_authentication(stub_hub, session, monkeypatch, token, expected):
    client = make_client(stub_hub, session, token)
    trace = []
    login = client._login_once

    async def tracked_login():
        result = await login()
        trace.append("login")
        return result

    def convert(name):
        trace.append(name)
        return trace.count(name)

    monkeypatch.setattr(client, "_login_once", tracked_login)
    await client.fetch_analytics(Epoch(lambda: convert("since")), Epoch(lambda: convert("until")))
    assert trace == expected
    for attempt, call in enumerate(stub_hub.analytics_calls, 1):
        assert (call["since"], call["until"]) == (str(attempt), str(attempt))


@pytest.mark.parametrize("password", ["", "wrong-password"])
async def test_auth_failure_does_not_evaluate_window(stub_hub, session, password):
    client = make_client(stub_hub, session, password=password)
    value = Epoch(lambda: pytest.fail("window evaluated before authentication"))
    with pytest.raises(HubAuthError):
        await client.fetch_analytics(value, value)
    assert len(stub_hub.login_calls) == bool(password)
    assert stub_hub.analytics_calls == []
