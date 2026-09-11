"""Flask API が全 Selenium 操作を single worker RPC に委譲することを検証する。"""

from __future__ import annotations

from collections import deque
import importlib
import sys

import pytest

import hems_api
from hems_runtime import RuntimeUnavailable
from hems_snapshot import SnapshotCoordinator, SnapshotStore


READ_KEY = "test-read-key"
CONTROL_KEY = "test-control-key"


def _status(**overrides):
    payload = {"floor": 1, "mode": "暖房", "temperature": 25.0, "power": "ON"}
    payload.update(overrides)
    return payload


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.floors = (1, 2)
        self.responses = deque()
        self.fail_after_admission = False
        self.after_send_exception = None

    def execute_control(self, operation, payload, *, deadline, before_execute=None, after_send=None):
        self.calls.append((operation, payload, hems_api.CONTROL_TIMEOUT_SECONDS))
        if self.responses and isinstance(self.responses[0], Exception):
            raise self.responses.popleft()
        if before_execute is not None and not before_execute():
            return 429, {"error": "Too many requests"}
        if self.fail_after_admission:
            self.fail_after_admission = False
            raise RuntimeUnavailable("worker socket closed")
        if after_send is not None:
            after_send()
        if self.after_send_exception is not None:
            exception = self.after_send_exception
            self.after_send_exception = None
            raise exception
        if self.responses:
            response = self.responses.popleft()
            return response
        if operation == "status":
            return 200, _status(floor=payload.get("floor") or 1)
        if operation == "security_status":
            return 200, {
                "lock": "LOCKED",
                "shutter": "CLOSED",
                "capabilities": {"lock": "available", "shutter": "available"},
            }
        if operation == "control":
            return 200, _status(
                floor=payload.get("floor") or 1,
                mode=payload.get("mode") or "暖房",
                temperature=payload.get("temp") or 25.0,
                power=payload.get("power") or "ON",
            )
        return 200, {
            "lock": "LOCKED",
            "shutter": "CLOSED",
            "capabilities": {"lock": "available", "shutter": "available"},
        }


class FakeSnapshots:
    def __init__(self):
        self.responses = {
            "aircon:1": (200, {**_status(floor=1), "snapshot": {"stale": False}}),
            "aircon:2": (200, {**_status(floor=2), "snapshot": {"stale": False}}),
            "security": (
                200,
                {
                    "lock": "LOCKED",
                    "shutter": "CLOSED",
                    "capabilities": {"lock": "available", "shutter": "available"},
                    "snapshot": {"stale": False},
                },
            ),
        }

    def response(self, key):
        return self.responses[key]

    valid_body = staticmethod(SnapshotStore.valid_body)

    def record_success(self, key, body):
        self.responses[key] = (200, {**body, "snapshot": {"stale": False}})


class FakeCoordinator:
    def __init__(self):
        self.targets = []

    def request_targeted(self, keys):
        self.targets.append(set(keys))


@pytest.fixture
def runtime(monkeypatch):
    fake = FakeRuntime()
    monkeypatch.setattr(hems_api, "runtime", fake)
    monkeypatch.setattr(hems_api, "snapshots", FakeSnapshots())
    monkeypatch.setattr(hems_api, "coordinator", FakeCoordinator())
    with hems_api._rate_limit_lock:
        hems_api._last_request_time.clear()
    return fake


@pytest.fixture
def api_client(runtime):
    with hems_api.app.test_client() as client:
        yield client


def _headers(key=READ_KEY):
    return {"X-API-Key": key}


@pytest.mark.parametrize("missing", ["HEMS_URL", "HEMS_USER", "HEMS_PASSWORD"])
def test_api_parent_fails_fast_when_worker_credential_is_missing(monkeypatch, missing):
    original = sys.modules.pop("hems_api")
    monkeypatch.delenv(missing, raising=False)
    try:
        expected = "HEMS_URL" if missing == "HEMS_URL" else "HEMS_USER and HEMS_PASSWORD"
        with pytest.raises(ValueError, match=expected):
            importlib.import_module("hems_api")
    finally:
        sys.modules["hems_api"] = original


def test_api_parent_fails_fast_for_invalid_disable_lock(monkeypatch):
    original = sys.modules.pop("hems_api")
    monkeypatch.setenv("HEMS_DISABLE_LOCK", "enabled")
    try:
        with pytest.raises(ValueError, match="HEMS_DISABLE_LOCK"):
            importlib.import_module("hems_api")
    finally:
        sys.modules["hems_api"] = original


def test_auth_and_json_errors_stay_local(api_client, runtime):
    assert api_client.get("/status").status_code == 401
    assert api_client.post("/control", json={}, headers=_headers()).status_code == 403
    assert api_client.post("/control", data=b"{bad", content_type="application/json", headers=_headers(CONTROL_KEY)).status_code == 400
    assert runtime.calls == []


def test_control_endpoint_is_not_rate_limited(api_client, runtime):
    first = api_client.post("/control", json={"power": "ON"}, headers=_headers(CONTROL_KEY))
    second = api_client.post("/control", json={"power": "ON"}, headers=_headers(CONTROL_KEY))

    assert [first.status_code, second.status_code] == [200, 200]
    assert [call[0] for call in runtime.calls] == ["control", "control"]


def test_not_found_and_method_not_allowed_use_json_error_contract(api_client, runtime):
    responses = [
        api_client.get("/not-found", headers=_headers(CONTROL_KEY)),
        api_client.get("/control", headers=_headers(CONTROL_KEY)),
    ]

    assert [response.status_code for response in responses] == [404, 405]
    assert all(isinstance(response.get_json().get("error"), str) for response in responses)
    assert runtime.calls == []


def test_main_starts_coordinator_without_synchronous_prewarm(monkeypatch):
    calls = []

    class Coordinator:
        def start(self):
            calls.append("coordinator-start")

        def stop(self, **_kwargs):
            calls.append("coordinator-stop")

    class Runtime:
        def stop_admission(self):
            calls.append("stop-admission")

        def wait_for_idle(self, _deadline):
            calls.append("wait-idle")

        def close(self):
            calls.append("runtime-close")

    monkeypatch.setattr(hems_api, "runtime", Runtime())
    monkeypatch.setattr(hems_api, "coordinator", Coordinator())
    monkeypatch.setattr(hems_api, "_shutdown_complete", False)
    monkeypatch.setattr(hems_api.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(hems_api.app, "run", lambda **kwargs: calls.append(kwargs))

    hems_api.main()

    assert calls == [
        "coordinator-start",
        {"host": "0.0.0.0", "port": 5000, "threaded": True},
        "stop-admission",
        "coordinator-stop",
        "wait-idle",
        "runtime-close",
    ]


def test_status_rejects_non_integer_floor_before_worker(api_client, runtime):
    for floor in ("abc", "1.5", "0", "3"):
        response = api_client.get(f"/status?floor={floor}", headers=_headers())
        assert response.status_code == 400
    assert runtime.calls == []


def test_authenticated_status_requires_floor_and_gets_are_runtime_free(api_client, runtime):
    missing = api_client.get("/status", headers=_headers())
    floor = api_client.get("/status?floor=2", headers=_headers())
    security = api_client.get("/security/status", headers=_headers())

    assert missing.status_code == 400
    assert missing.get_json() == {"error": "Invalid floor"}
    assert floor.status_code == 200
    assert floor.get_json()["floor"] == 2
    assert floor.get_json()["floors"] == [1, 2]
    assert security.status_code == 200
    assert runtime.calls == []


def test_status_and_control_follow_detected_one_and_three_floor_configuration(
    api_client, runtime
):
    runtime.floors = (1, 3)
    hems_api.snapshots.responses["aircon:3"] = (
        200,
        {**_status(floor=3), "snapshot": {"stale": False}},
    )

    status = api_client.get("/status?floor=3", headers=_headers())
    invalid = api_client.get("/status?floor=2", headers=_headers())
    control = api_client.post(
        "/control", json={"power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert status.status_code == 200
    assert status.get_json()["floors"] == [1, 3]
    assert invalid.status_code == 400
    assert control.status_code == 200
    assert hems_api.coordinator.targets == [{"aircon:1", "aircon:3"}]


def test_floor_endpoints_fail_closed_until_worker_floors_are_known(api_client, runtime):
    runtime.floors = None

    status = api_client.get("/status?floor=1", headers=_headers())
    control = api_client.post(
        "/control", json={"floor": 1, "power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert status.status_code == 503
    assert control.status_code == 503
    assert status.get_json()["reason"] == "floors_unknown"
    assert control.get_json()["reason"] == "floors_unknown"
    assert runtime.calls == []


def test_stale_snapshot_returns_metadata_503_without_runtime_rpc(api_client, runtime):
    hems_api.snapshots.responses["aircon:1"] = (
        503,
        {
            "error": "HEMS snapshot unavailable",
            "snapshot": {
                "observed_at": "2026-08-27T00:00:00Z",
                "age_seconds": 90.0,
                "stale": True,
                "last_error": "stale",
            },
        },
    )
    response = api_client.get("/status?floor=1", headers=_headers())
    assert response.status_code == 503
    assert response.get_json()["snapshot"]["last_error"] == "stale"
    assert runtime.calls == []


@pytest.mark.parametrize("key", [None, "bogus"])
def test_all_endpoints_reject_missing_or_wrong_key_before_runtime(api_client, runtime, key):
    headers = {} if key is None else _headers(key)
    responses = [
        api_client.get("/status", headers=headers),
        api_client.get("/security/status", headers=headers),
        api_client.post("/control", json={}, headers=headers),
        api_client.post("/security/lock", json={"action": "lock"}, headers=headers),
        api_client.post("/security/shutter", json={"action": "open"}, headers=headers),
    ]
    assert [response.status_code for response in responses] == [401] * 5
    assert runtime.calls == []


def test_control_key_is_accepted_for_read_endpoints(api_client, runtime):
    assert api_client.get("/status?floor=1", headers=_headers(CONTROL_KEY)).status_code == 200
    assert api_client.get("/security/status", headers=_headers(CONTROL_KEY)).status_code == 200
    assert runtime.calls == []


@pytest.mark.parametrize("path", ["/control", "/security/lock", "/security/shutter"])
def test_post_endpoints_require_json_content_type(api_client, runtime, path):
    response = api_client.post(path, data=b'{"action":"lock"}', headers=_headers(CONTROL_KEY))
    assert response.status_code == 400
    assert response.get_json() == {"error": "Content-Type must be application/json"}
    assert runtime.calls == []


@pytest.mark.parametrize("body", [b"", b"{bad", b"[]", b'"text"', b"1", b"null"])
def test_post_endpoints_require_json_object(api_client, runtime, body):
    for path in ("/control", "/security/lock", "/security/shutter"):
        response = api_client.post(path, data=body, content_type="application/json", headers=_headers(CONTROL_KEY))
        assert response.status_code == 400
        assert response.get_json() == {"error": "Request body must be a JSON object"}
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"floor": 1.5}, "Invalid floor"),
        ({"floor": 3}, "Invalid floor. Use one of: 1, 2"),
        ({"mode": "暖房冷房"}, "Invalid mode"),
        ({"temp": "warm"}, "Invalid temp"),
        ({"temp": 16}, "Invalid temp. Must be between 17 and 30"),
        ({"temp": 31}, "Invalid temp. Must be between 17 and 30"),
        ({"temp": 17.5}, "Invalid temp. Must be a whole degree"),
        ({"temp": float("nan")}, "Invalid temp. Must be a whole degree"),
        ({"temp": float("inf")}, "Invalid temp. Must be a whole degree"),
        ({"power": "MAYBE"}, "Invalid power. Use 'ON' or 'OFF'"),
    ],
)
def test_control_validation_boundaries_do_not_reach_runtime(api_client, runtime, body, error):
    response = api_client.post("/control", json=body, headers=_headers(CONTROL_KEY))
    assert response.status_code == 400
    assert response.get_json() == {"error": error}
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("path", "body", "error"),
    [
        ("/security/lock", {"action": "explode"}, "Invalid action. Use 'lock' or 'unlock'"),
        ("/security/shutter", {"action": "explode"}, "Invalid action. Use 'open' or 'close'"),
    ],
)
def test_security_action_validation_does_not_reach_runtime(api_client, runtime, path, body, error):
    response = api_client.post(path, json=body, headers=_headers(CONTROL_KEY))
    assert response.status_code == 400
    assert response.get_json() == {"error": error}
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("path", "capabilities", "status", "reason"),
    [
        (
            "/security/lock",
            {"lock": "disabled", "shutter": "available"},
            403,
            "lock_disabled",
        ),
        (
            "/security/lock",
            {"lock": "not_installed", "shutter": "available"},
            501,
            "lock_not_installed",
        ),
        (
            "/security/shutter",
            {"lock": "available", "shutter": "not_installed"},
            501,
            "shutter_not_installed",
        ),
    ],
)
def test_security_capability_rejection_precedes_rpc_and_rate_window(
    api_client, runtime, path, capabilities, status, reason
):
    hems_api.snapshots.responses["security"] = (
        200,
        {
            "lock": "DISABLED",
            "shutter": "NOT_INSTALLED",
            "capabilities": capabilities,
            "snapshot": {"stale": False},
        },
    )
    action = "lock" if path.endswith("lock") else "open"

    first = api_client.post(path, json={"action": action}, headers=_headers(CONTROL_KEY))
    second = api_client.post(path, json={"action": action}, headers=_headers(CONTROL_KEY))

    assert [first.status_code, second.status_code] == [status, status]
    assert first.get_json()["reason"] == reason
    assert runtime.calls == []
    assert hems_api.coordinator.targets == []


def test_security_control_fails_closed_when_capabilities_are_unknown(api_client, runtime):
    hems_api.snapshots.responses["security"] = (
        503,
        {"error": "HEMS snapshot unavailable", "snapshot": {"stale": True}},
    )

    response = api_client.post(
        "/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY)
    )

    assert response.status_code == 503
    assert response.get_json()["reason"] == "capabilities_unknown"
    assert runtime.calls == []
    assert hems_api.coordinator.targets == []


def test_control_ignores_unknown_fields_and_normalizes_whole_temperature(api_client, runtime):
    response = api_client.post("/control", json={"temp": 26, "future_option": True}, headers=_headers(CONTROL_KEY))
    assert response.status_code == 200
    assert runtime.calls == [("control", {"floor": None, "mode": None, "temp": 26.0, "power": None}, 25.0)]


@pytest.mark.parametrize(
    ("path", "method", "body", "operation", "budget"),
    [
        ("/control", "post", {"floor": 2, "mode": "冷房", "temp": 26, "power": "ON"}, "control", 25.0),
        ("/security/lock", "post", {"action": "lock"}, "lock", 25.0),
        ("/security/shutter", "post", {"action": "open"}, "shutter", 25.0),
    ],
)
def test_all_endpoints_use_one_runtime_rpc(api_client, runtime, path, method, body, operation, budget):
    response = getattr(api_client, method)(path, json=body, headers=_headers(CONTROL_KEY))

    assert response.status_code == 200
    assert len(runtime.calls) == 1
    assert runtime.calls[0][0] == operation
    assert runtime.calls[0][2] == budget


@pytest.mark.parametrize("path, method, body", [
    ("/control", "post", {"power": "ON"}),
    ("/security/lock", "post", {"action": "lock"}),
    ("/security/shutter", "post", {"action": "open"}),
])
def test_unready_busy_or_timeout_is_uniform_503(api_client, runtime, path, method, body):
    runtime.responses.append(RuntimeUnavailable("unavailable"))
    response = getattr(api_client, method)(path, json=body, headers=_headers(CONTROL_KEY))

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "HEMS backend unavailable",
        "reason": "backend_unavailable",
    }


@pytest.mark.parametrize(
    ("path", "body", "expected"),
    [
        ("/control", {"floor": 2, "power": "ON"}, {"aircon:2"}),
        ("/control", {"power": "ON"}, {"aircon:1", "aircon:2"}),
        ("/security/lock", {"action": "lock"}, {"security"}),
        ("/security/shutter", {"action": "open"}, {"security"}),
    ],
)
def test_sent_control_schedules_targeted_refresh_even_for_failure(api_client, runtime, path, body, expected):
    runtime.responses.append((502, {"error": "not confirmed"}))
    response = api_client.post(path, json=body, headers=_headers(CONTROL_KEY))
    assert response.status_code == 502
    assert hems_api.coordinator.targets == [expected]


def test_finite_worker_failure_keeps_existing_502_control_contract(api_client, runtime):
    runtime.responses.append((502, {"error": "Control not confirmed", "failed": ["power"]}))
    response = api_client.post("/control", json={"power": "ON"}, headers=_headers(CONTROL_KEY))

    assert response.status_code == 502
    assert response.get_json()["failed"] == ["power"]


def test_control_502_preserves_requested_actual_and_multiple_failed_fields(api_client, runtime):
    actual = _status(mode="送風", temperature=18.0)
    expected = {"error": "Control not confirmed", "requested": {"mode": "冷房", "temp": 26, "power": "ON"}, "actual": actual, "failed": ["mode", "temp"]}
    runtime.responses.append((502, expected))
    response = api_client.post("/control", json={"mode": "冷房", "temp": 26, "power": "ON"}, headers=_headers(CONTROL_KEY))
    assert response.status_code == 502
    assert response.get_json() == expected


@pytest.mark.parametrize("path, body, expected", [
    ("/security/lock", {"action": "lock"}, (500, {"error": "Failed to control lock"})),
    ("/security/shutter", {"action": "close"}, (500, {"error": "Failed to control shutter"})),
])
def test_existing_non_control_failure_body_is_preserved(api_client, runtime, path, body, expected):
    runtime.responses.append(expected)
    response = api_client.get(path, headers=_headers()) if body is None else api_client.post(path, json=body, headers=_headers(CONTROL_KEY))

    assert response.status_code == 500
    assert response.get_json() == expected[1]


def test_rate_limit_runs_after_input_validation_and_after_admission(api_client, runtime):
    invalid = api_client.post("/security/lock", data=b"{bad", content_type="application/json", headers=_headers(CONTROL_KEY))
    runtime.responses.append(RuntimeUnavailable("busy"))
    unavailable = api_client.post("/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY))
    accepted = api_client.post("/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY))
    limited = api_client.post("/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY))

    assert [invalid.status_code, unavailable.status_code, accepted.status_code, limited.status_code] == [400, 503, 200, 429]
    assert hems_api.coordinator.targets == []


def test_security_rate_limit_does_not_commit_when_worker_frame_send_fails(api_client, runtime):
    runtime.fail_after_admission = True

    unavailable = api_client.post("/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY))
    accepted = api_client.post("/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY))
    limited = api_client.post("/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY))

    assert [unavailable.status_code, accepted.status_code, limited.status_code] == [503, 200, 429]
    assert hems_api.coordinator.targets == []


def test_power_on_failure_is_worker_atomic_and_skips_followups():
    class Controller:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def method(*args, **kwargs):
                self.calls.append(name)
                return False if name == "toggle_power" else _status()
            return method

    from hems_runtime import _run_operation

    controller = Controller()
    status, body = _run_operation(controller, "control", {"mode": "冷房", "temp": 26, "power": None}, 999999.0)
    assert status == 502
    assert body["failed"] == ["power"]
    assert "set_mode" not in controller.calls
    assert "set_temperature" not in controller.calls
    assert "get_current_status" not in controller.calls


@pytest.fixture
def real_snapshots(monkeypatch):
    """seed 検証は本物の SnapshotStore を使い、valid_body の判定まで通す。"""
    store = SnapshotStore()
    monkeypatch.setattr(hems_api, "snapshots", store)
    return store


def _seeded_status(api_client, floor):
    return api_client.get(f"/status?floor={floor}", headers=_headers()).get_json()


def test_confirmed_control_seeds_snapshot_so_next_read_sees_post_control_state(
    api_client, runtime, real_snapshots
):
    """制御応答は refresh と同一の get_current_status。捨てずに cache へ入れる。"""
    response = api_client.post(
        "/control", json={"floor": 1, "mode": "冷房", "power": "ON"}, headers=_headers(CONTROL_KEY)
    )
    assert response.status_code == 200

    seeded = _seeded_status(api_client, 1)
    assert (seeded["mode"], seeded["power"], seeded["floor"]) == ("冷房", "ON", 1)
    assert seeded["snapshot"]["stale"] is False
    assert hems_api.coordinator.targets == []


def test_confirmed_security_control_seeds_security_snapshot(api_client, runtime, real_snapshots):
    real_snapshots.record_success(
        "security",
        {
            "lock": "UNLOCKED",
            "shutter": "CLOSED",
            "capabilities": {"lock": "available", "shutter": "available"},
        },
    )
    assert api_client.post(
        "/security/lock", json={"action": "lock"}, headers=_headers(CONTROL_KEY)
    ).status_code == 200

    status, body = real_snapshots.response("security")
    assert status == 200
    assert (body["lock"], body["shutter"]) == ("LOCKED", "CLOSED")
    assert hems_api.coordinator.targets == []


def test_unconfirmed_control_seeds_observed_actual_for_502(
    api_client, runtime, real_snapshots
):
    actual = _status(mode="送風", temperature=18.0, power="ON")
    runtime.responses.append(
        (502, {"error": "Control not confirmed", "actual": actual, "failed": ["mode"]})
    )
    response = api_client.post(
        "/control", json={"floor": 1, "mode": "冷房", "power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert response.status_code == 502
    seeded = _seeded_status(api_client, 1)
    assert seeded["mode"] == "送風"
    assert seeded["temperature"] == 18.0


def test_off_to_on_unverified_seeds_observed_actual_for_503(
    api_client, runtime, real_snapshots
):
    actual = _status(mode="暖房", temperature=22.0, power="OFF")
    runtime.responses.append(
        (503, {
            "error": "Control not confirmed after OFF to ON transition",
            "reason": "off_to_on_unverified",
            "requested": {"mode": "冷房", "temp": 26, "power": "ON"},
            "actual": actual,
        })
    )
    response = api_client.post(
        "/control", json={"floor": 1, "mode": "冷房", "power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert response.status_code == 503
    seeded = _seeded_status(api_client, 1)
    assert seeded["mode"] == "暖房"
    assert seeded["temperature"] == 22.0


def test_off_to_on_observation_is_replaced_by_later_targeted_refresh(
    api_client, runtime, real_snapshots, monkeypatch
):
    class RefreshRuntime:
        floors = (1, 2)

        def execute_refresh(self, operation, payload, *, timeout_seconds):
            assert (operation, payload, timeout_seconds) == ("status", {"floor": 1}, 6.0)
            return 200, _status(mode="除湿", temperature=None, power="ON")

    coordinator = SnapshotCoordinator(RefreshRuntime(), real_snapshots)
    monkeypatch.setattr(hems_api, "coordinator", coordinator)
    runtime.responses.append(
        (503, {
            "error": "Control not confirmed after OFF to ON transition",
            "reason": "off_to_on_unverified",
            "requested": {"mode": "送風", "temp": None, "power": "ON"},
            "actual": _status(mode="送風", temperature=None, power="ON"),
        })
    )

    response = api_client.post(
        "/control", json={"floor": 1, "mode": "送風", "power": "ON"},
        headers=_headers(CONTROL_KEY),
    )

    assert response.status_code == 503
    assert _seeded_status(api_client, 1)["mode"] == "送風"
    assert coordinator.pending_keys == {"aircon:1"}
    assert coordinator.refresh_one_targeted() is True
    refreshed = _seeded_status(api_client, 1)
    assert (refreshed["mode"], refreshed["temperature"]) == ("除湿", None)


@pytest.mark.parametrize(
    "prepared",
    [
        (502, {"error": "Control not confirmed", "failed": ["mode"]}),
        (503, {"error": "Control deadline exhausted", "reason": "control_deadline_exhausted", "actual": _status()}),
        (503, {"error": "HEMS backend unavailable", "reason": "backend_unavailable", "actual": _status()}),
        RuntimeUnavailable("worker socket closed"),
    ],
    ids=["actual-less-502", "unsupported-503", "backend-unavailable-503", "runtime-unavailable"],
)
def test_unsupported_control_results_never_seed_snapshot(api_client, runtime, real_snapshots, prepared):
    runtime.responses.append(prepared)
    response = api_client.post(
        "/control", json={"floor": 1, "mode": "冷房", "power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert response.status_code == (503 if isinstance(prepared, RuntimeUnavailable) else prepared[0])
    assert real_snapshots.response("aircon:1")[0] == 503


def test_control_without_floor_seeds_nothing_because_target_is_ambiguous(
    api_client, runtime, real_snapshots
):
    """floor 省略は refresh_keys が2件になり、どちらの階か決められない。"""
    assert api_client.post(
        "/control", json={"power": "ON"}, headers=_headers(CONTROL_KEY)
    ).status_code == 200

    assert real_snapshots.response("aircon:1")[0] == 503
    assert real_snapshots.response("aircon:2")[0] == 503
    assert hems_api.coordinator.targets == [{"aircon:1", "aircon:2"}]


def test_control_body_for_other_floor_is_rejected_by_valid_body(
    api_client, runtime, real_snapshots
):
    """worker が別階の body を返しても、その key の snapshot を汚さない。"""
    runtime.responses.append((200, _status(floor=2)))
    assert api_client.post(
        "/control", json={"floor": 1, "power": "ON"}, headers=_headers(CONTROL_KEY)
    ).status_code == 200

    assert real_snapshots.response("aircon:1")[0] == 503
    assert hems_api.coordinator.targets == [{"aircon:1"}]


def test_invalid_confirmed_body_keeps_targeted_refresh(
    api_client, runtime, real_snapshots
):
    runtime.responses.append((200, {"floor": "1", "power": "ON"}))
    response = api_client.post(
        "/control", json={"floor": 1, "power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert response.status_code == 200
    assert real_snapshots.response("aircon:1")[0] == 503
    assert hems_api.coordinator.targets == [{"aircon:1"}]


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [(RuntimeUnavailable("worker socket closed after send"), 503), (RuntimeError("response lost after send"), 500)],
    ids=["runtime-unavailable", "unexpected-error"],
)
def test_exception_after_send_keeps_targeted_refresh(
    api_client, runtime, real_snapshots, exception, expected_status
):
    runtime.after_send_exception = exception
    response = api_client.post(
        "/control", json={"floor": 1, "power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert response.status_code == expected_status
    assert real_snapshots.response("aircon:1")[0] == 503
    assert hems_api.coordinator.targets == [{"aircon:1"}]


@pytest.mark.parametrize(
    "status, reason",
    [(502, None), (503, "off_to_on_unverified")],
)
def test_control_actual_for_other_floor_is_rejected_by_valid_body(
    api_client, runtime, real_snapshots, status, reason
):
    actual = _status(floor=2, mode="冷房")
    body = {"error": "Control not confirmed", "actual": actual}
    if reason:
        body["reason"] = reason
    runtime.responses.append((status, body))
    response = api_client.post(
        "/control", json={"floor": 1, "power": "ON"}, headers=_headers(CONTROL_KEY)
    )

    assert response.status_code == status
    assert real_snapshots.response("aircon:1")[0] == 503
