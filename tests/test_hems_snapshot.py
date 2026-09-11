from __future__ import annotations

from datetime import datetime, timezone
import pytest
from hems_runtime import RuntimeUnavailable
from hems_snapshot import SnapshotCoordinator, SnapshotStore


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def test_snapshot_freshness_partial_failure_and_floor_separation():
    clock = FakeClock()
    store = SnapshotStore(
        monotonic=clock,
        utcnow=lambda: datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc),
    )
    store.record_success("aircon:1", {"floor": 1, "power": "ON"})
    store.record_success("aircon:2", {"floor": 2, "power": "OFF"})

    clock.value = 189.9
    status, body = store.response("aircon:1")
    assert status == 200
    assert body["floor"] == 1
    assert body["snapshot"] == {
        "observed_at": "2026-08-27T00:00:00Z",
        "age_seconds": 89.9,
        "stale": False,
        "last_error": None,
    }

    store.record_failure("aircon:2", "refresh_failed")
    failed_status, failed_body = store.response("aircon:2")
    assert failed_status == 503
    assert failed_body["error"] == "HEMS snapshot unavailable"
    assert failed_body["snapshot"]["last_error"] == "refresh_failed"
    assert "floor" not in failed_body

    clock.value = 190.0
    stale_status, stale_body = store.response("aircon:1")
    assert stale_status == 503
    assert stale_body["snapshot"]["age_seconds"] == 90.0
    assert stale_body["snapshot"]["stale"] is True


def test_age_rounding_does_not_make_just_under_90_seconds_stale():
    clock = FakeClock()
    store = SnapshotStore(monotonic=clock)
    store.record_success("aircon:1", {"floor": 1, "power": "ON"})
    clock.value = 189.9996
    status, body = store.response("aircon:1")
    assert status == 200
    assert body["snapshot"]["age_seconds"] == 89.999


def test_unobserved_snapshot_is_503_without_age():
    store = SnapshotStore()
    status, body = store.response("security")
    assert status == 503
    assert body["snapshot"] == {
        "observed_at": None,
        "age_seconds": None,
        "stale": True,
        "last_error": "not_observed",
    }


def test_dynamic_floor_configuration_removes_old_snapshot_and_accepts_third_floor():
    store = SnapshotStore()
    store.configure_floors((1, 3))
    store.record_success("aircon:3", {"floor": 3, "power": "ON"})
    assert store.response("aircon:3")[0] == 200

    store.configure_floors((1,))
    assert store.response("aircon:3")[0] == 503
    store.record_success("aircon:3", {"floor": 3, "power": "OFF"})
    assert store.response("aircon:3")[0] == 503


@pytest.mark.parametrize(
    "body",
    [
        {"lock": "LOCKED", "shutter": "CLOSED"},
        {
            "lock": "MISSING",
            "shutter": "CLOSED",
            "capabilities": {"lock": "available", "shutter": "available"},
        },
        {
            "lock": "DISABLED",
            "shutter": "CLOSED",
            "capabilities": {"lock": "available", "shutter": "available"},
        },
    ],
)
def test_security_snapshot_rejects_inconsistent_capability_contract(body):
    assert SnapshotStore.valid_body("security", body) is False


def test_security_snapshot_accepts_disabled_and_not_installed_states():
    assert SnapshotStore.valid_body(
        "security",
        {
            "lock": "DISABLED",
            "shutter": "NOT_INSTALLED",
            "capabilities": {"lock": "disabled", "shutter": "not_installed"},
        },
    ) is True


def test_floor_removed_during_refresh_is_not_resurrected():
    store = SnapshotStore()

    class Runtime:
        floors = (1, 3)

        def execute_refresh(self, operation, payload, *, timeout_seconds):
            assert (operation, payload) == ("status", {"floor": 3})
            self.floors = (1,)
            return 200, {"floor": 3, "power": "ON"}

    runtime = Runtime()
    coordinator = SnapshotCoordinator(runtime, store)
    coordinator.request_targeted({"aircon:3"})

    assert coordinator.refresh_one_targeted() is True
    assert coordinator.pending_keys == set()
    assert store.response("aircon:3")[0] == 503


def test_unknown_target_is_ignored_instead_of_raising():
    class Runtime:
        floors = (1,)

    coordinator = SnapshotCoordinator(Runtime(), SnapshotStore())
    coordinator.request_targeted({"aircon:9", "not-a-target"})
    assert coordinator.pending_keys == set()


def test_targeted_refresh_remains_pending_until_success():
    class Runtime:
        def __init__(self):
            self.floors = (1, 2)
            self.results = [RuntimeUnavailable("cooldown"), (200, {"floor": 2, "power": "ON"})]

        def prewarm(self):
            return True

        def execute_refresh(self, operation, payload, *, timeout_seconds):
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    coordinator = SnapshotCoordinator(Runtime(), SnapshotStore(), retry_seconds=0.01)
    coordinator.request_targeted({"aircon:2"})
    assert coordinator.refresh_one_targeted() is False
    assert coordinator.pending_keys == {"aircon:2"}
    assert coordinator.refresh_one_targeted() is True
    assert coordinator.pending_keys == set()


def test_control_admission_deferral_keeps_last_success_fresh():
    class Runtime:
        def execute_refresh(self, operation, payload, *, timeout_seconds):
            raise RuntimeUnavailable("worker busy")

    store = SnapshotStore()
    store.record_success("security", {"lock": "LOCKED", "shutter": "CLOSED"})
    coordinator = SnapshotCoordinator(Runtime(), store)
    coordinator.request_targeted({"security"})

    assert coordinator.refresh_one_targeted() is False
    assert coordinator.pending_keys == {"security"}
    status, body = store.response("security")
    assert status == 200
    assert body["snapshot"]["last_error"] is None
