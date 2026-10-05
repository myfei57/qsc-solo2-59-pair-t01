"""排程控制台接口：收单、出方案、回放与重算都走 HTTP API。"""

from __future__ import annotations

import calendar
import json
import time

import support
from batterystore.console.pages import PageLibrary
from batterystore.console.api import Api
from batterystore.console.server import ConsoleServer


def _date_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def test_schedule_api_direct_handle(platform):
    api = Api(platform, PageLibrary("web"))
    platform.start()
    date = _date_of(platform.clock.now())

    code, bid = api.handle("POST", "/api/schedule/bid", {}, {
        "bid_id": "c1", "direction": "charge", "energy_kwh": 80.0, "owner": "team-a",
    })
    assert code == 200 and bid["bid_id"] == "c1"

    code, body = api.handle("POST", "/api/schedule/bid", {}, {"direction": "charge", "energy_kwh": 1.0})
    assert code == 409 and body["error"] == "StoreError"

    code, gen = api.handle("POST", "/api/schedule/generate", {}, {"plan_date": date})
    assert code == 200 and gen["reconciliation"]["ok"] is True

    code, current = api.handle("GET", "/api/schedule", {}, {})
    assert code == 200 and current["version"] == 1

    code, ver = api.handle("GET", "/api/schedule/version", {"plan_date": date, "version": "1"}, {})
    assert code == 200 and ver["version"] == 1

    code, replay = api.handle("GET", "/api/schedule/replay", {"plan_date": date}, {})
    assert code == 200 and len(replay["replay"]) == 1

    code, recompute = api.handle("GET", "/api/schedule/recompute", {"plan_date": date, "version": "1"}, {})
    assert code == 200 and recompute["consistent"] is True

    code, policy = api.handle("GET", "/api/schedule/policy", {"plan_date": date}, {})
    assert code == 200 and policy["default_policy"] == "submit"
    assert any(window["crosses_midnight"] is False for window in policy["windows"])


def test_schedule_routes_listed_and_served_over_http(platform):
    support.bring_online(platform)
    date = _date_of(platform.clock.now())
    platform.submit_schedule_bid("d1", "discharge", 40.0)
    platform.generate_schedule(date)

    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        with __import__("urllib.request", fromlist=["urlopen"]).urlopen(
            f"{base_url}/api/schedule", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["version"] == 1
        with __import__("urllib.request", fromlist=["urlopen"]).urlopen(
            f"{base_url}/api/schedule/versions", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["versions"][0]["plan_date"] == date
    finally:
        server.stop()
