"""Read-only CLI probe for the WorkBuddy Hub — no AstrBot, no QQ.

Reads the panel password from the WB_HUB_PASSWORD environment variable if set,
otherwise uses the shipped default "admin" (matching the plugin config default);
an optional preobtained token may come from WB_HUB_PANEL_TOKEN. Secrets are never
accepted as CLI arguments (they would land in shell history) and are never printed.

Usage:
  WB_HUB_PASSWORD=... python tools/probe_hub.py --base-url http://127.0.0.1:8788 \
      --timezone Asia/Shanghai
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

# Allow running both as a module-relative tool and as a plain script.
if __package__ in (None, ""):
    # The package import needs the directory that CONTAINS the plugin package,
    # while the plugin directory itself is needed for intra-plugin helpers.
    _PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
    _PACKAGE_PARENT = _PLUGIN_DIR.parent
    for _entry in (str(_PACKAGE_PARENT), str(_PLUGIN_DIR)):
        if _entry not in sys.path:
            sys.path.insert(0, _entry)

from astrbot_plugin_workbuddy_usage.client import (  # noqa: E402
    HubAuthError,
    HubError,
    HubProtocolError,
    HubRateLimitError,
    HubUnavailableError,
    WorkBuddyClient,
)
from astrbot_plugin_workbuddy_usage.reporting import (  # noqa: E402
    aggregate,
    day_bounds,
    day_bounds_from_hub,
    format_report,
)

EXIT_OK = 0
EXIT_TRANSPORT = 2
EXIT_AUTH = 3
EXIT_SCHEMA = 4


async def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only WorkBuddy Hub usage probe")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8788",
        help="Hub origin (default: http://127.0.0.1:8788, the Hub's default port)",
    )
    parser.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone for day bounds")
    parser.add_argument(
        "--proxy",
        default=os.environ.get("WB_HUB_PROXY", ""),
        help="optional HTTP/HTTPS proxy, e.g. http://127.0.0.1:8080 (default: direct; "
        "falls back to WB_HUB_PROXY env var)",
    )
    args = parser.parse_args()

    # Password: WB_HUB_PASSWORD if set, otherwise the shipped default "admin"
    # (matching the plugin config default). Never accepted as a CLI argument.
    password = os.environ.get("WB_HUB_PASSWORD") or "admin"
    token = os.environ.get("WB_HUB_PANEL_TOKEN", "")
    if not password and not token:
        print("ERROR: set WB_HUB_PASSWORD (or WB_HUB_PANEL_TOKEN) in the environment.",
              file=sys.stderr)
        return EXIT_AUTH

    now = datetime.now(tz=ZoneInfo(args.timezone))
    tz = ZoneInfo(args.timezone)

    async with aiohttp.ClientSession() as session:
        client = WorkBuddyClient(
            base_url=args.base_url,
            panel_password=password,
            panel_token=token,
            timeout_seconds=15.0,
            max_response_bytes=5 * 1024 * 1024,
            proxy=args.proxy,
            session=session,
        )
        # Window authority: the Hub's own clock (HTTP Date header) when
        # available — the Hub timestamps every usage row — else the local
        # clock. A login happens first so the Date header is captured.
        try:
            await client.login()
        except HubError:
            pass  # fetch_analytics will retry with credentials as usual
        hub_now = client.hub_now()
        if hub_now is not None:
            since, until = day_bounds_from_hub(hub_now, args.timezone)
        else:
            since, until = day_bounds(now, args.timezone)
        print(
            "查询窗口: "
            f"{datetime.fromtimestamp(since, tz).isoformat()} .. "
            f"{datetime.fromtimestamp(until, tz).isoformat()} "
            f"(epoch since={since:.0f} until={until:.0f}, "
            f"clock={'Hub Date header' if hub_now is not None else 'local'})"
        )
        try:
            payload = await client.fetch_analytics(since, until)
        except HubAuthError as exc:
            print(f"AUTH FAILURE: {exc}", file=sys.stderr)
            return EXIT_AUTH
        except HubRateLimitError:
            print("RATE LIMITED (HTTP 429).", file=sys.stderr)
            return EXIT_TRANSPORT
        except HubUnavailableError as exc:
            print(f"TRANSPORT FAILURE: {exc}", file=sys.stderr)
            return EXIT_TRANSPORT
        except HubProtocolError as exc:
            print(f"SCHEMA FAILURE: {exc}", file=sys.stderr)
            return EXIT_SCHEMA
        finally:
            await client.logout()

    # Per-account live state (GET /accounts?realm=all). Failures are
    # non-fatal: the report still renders without status info.
    accounts_payload = None
    accounts_hub_now = None
    async with aiohttp.ClientSession() as session:
        client2 = WorkBuddyClient(
            base_url=args.base_url,
            panel_password=password,
            panel_token=token,
            timeout_seconds=15.0,
            max_response_bytes=5 * 1024 * 1024,
            proxy=args.proxy,
            session=session,
        )
        try:
            await client2.login()
            accounts_payload = await client2.fetch_accounts()
            accounts_hub_now = client2.hub_now()
        except HubError as exc:
            print(f"WARNING: /accounts unavailable ({type(exc).__name__}: {exc}); "
                  "report will omit per-account status.", file=sys.stderr)
        finally:
            await client2.logout()

    if accounts_hub_now is not None:
        print(
            f"/accounts Hub clock: {accounts_hub_now.astimezone(tz).isoformat()}"
        )
        print(f"/accounts rows: {len(accounts_payload.get('accounts', []))}")
        for entry in accounts_payload.get("accounts", []):
            print(
                "  uid={uid} enabled={enabled} breakerFor={breakerFor} "
                "cooldownFor={cooldownFor} inCooldown={inCooldown} "
                "degradeFor={degradeFor} modelCooldowns={modelCooldowns} "
                "reserveBlocked={reserveBlocked} dailyLimitBlocked={dailyLimitBlocked} "
                "creditLimitReached={creditLimitReached}".format(
                    uid=entry.get("uid"),
                    enabled=entry.get("enabled"),
                    breakerFor=entry.get("breakerFor"),
                    cooldownFor=entry.get("cooldownFor"),
                    inCooldown=entry.get("inCooldown"),
                    degradeFor=entry.get("degradeFor"),
                    modelCooldowns=entry.get("modelCooldowns"),
                    reserveBlocked=entry.get("reserveBlocked"),
                    dailyLimitBlocked=entry.get("dailyLimitBlocked"),
                    creditLimitReached=entry.get("creditLimitReached"),
                )
            )
    else:
        print("/accounts: unavailable (no status info)")
    print()

    # The report's "day" comes from the same instant used for the bounds.
    effective = hub_now if hub_now is not None else now
    local_now = effective if effective.tzinfo is not None else effective.replace(tzinfo=tz)
    local_now = local_now.astimezone(tz)
    report = aggregate(
        payload,
        local_now.date(),
        args.timezone,
        local_now,
        accounts_payload=accounts_payload,
        hub_now=accounts_hub_now,
    )
    print(format_report(report))
    print()
    status = "schema validation: OK (payload matched the expected contract)"
    if report.warnings:
        status += f" | reconciliation warnings: {len(report.warnings)}"
    print(status)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
