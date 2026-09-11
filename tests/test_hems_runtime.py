"""hems_runtime.py の parent/worker 境界を実 Chrome なしで検証する。"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types

import pytest
from selenium.common.exceptions import TimeoutException

from hems_runtime import (
    FRAME_MAX_BYTES,
    FrameError,
    RuntimeUnavailable,
    SeleniumRuntime,
    recv_frame,
    send_frame,
)


def test_worker_fails_closed_when_hems_url_is_missing(monkeypatch):
    monkeypatch.delenv("HEMS_URL", raising=False)
    from hems_runtime import _required_hems_url

    with pytest.raises(ValueError, match="HEMS_URL"):
        _required_hems_url()


def _status(**overrides):
    value = {"floor": 1, "mode": "暖房", "temperature": 25.0, "power": "ON"}
    value.update(overrides)
    return value


class _OperationController:
    def __init__(self, *, status=_status(), security_status=None, outcomes=None):
        self.status = status
        self.security_status = security_status or {"lock": "LOCKED", "shutter": "CLOSED"}
        self.outcomes = outcomes or {}
        self.calls = []

    def _outcome(self, name):
        return self.outcomes.get(name, True)

    def ensure_connection(self, **kwargs):
        self.calls.append("ensure_connection")
        return self._outcome("ensure_connection")

    def navigate_to_smart_airs(self, **kwargs):
        self.calls.append("navigate_to_smart_airs")
        return self._outcome("navigate_to_smart_airs")

    def select_floor(self, *args, **kwargs):
        self.calls.append("select_floor")
        return self._outcome("select_floor")

    def toggle_power(self, *args, **kwargs):
        self.calls.append("toggle_power")
        return self._outcome("toggle_power")

    def set_mode(self, *args, **kwargs):
        self.calls.append("set_mode")
        return self._outcome("set_mode")

    def set_temperature(self, *args, **kwargs):
        self.calls.append("set_temperature")
        return self._outcome("set_temperature")

    def get_current_status(self, **kwargs):
        self.calls.append("get_current_status")
        return self.status

    def control_lock(self, *args, **kwargs):
        self.calls.append("control_lock")
        return self._outcome("control_lock")

    def control_shutter(self, *args, **kwargs):
        self.calls.append("control_shutter")
        return self._outcome("control_shutter")

    def get_security_status(self, **kwargs):
        self.calls.append("get_security_status")
        return self.security_status


def test_recv_frame_handles_partial_stream_writes():
    sender, receiver = socket.socketpair()
    try:
        raw = b'{"type":"result","status":200,"body":{"power":"ON"}}'
        header = len(raw).to_bytes(4, "big")
        sender.sendall(header[:2])
        sender.sendall(header[2:] + raw[:5])
        sender.sendall(raw[5:])

        assert recv_frame(receiver, time.monotonic() + 1) == {
            "type": "result",
            "status": 200,
            "body": {"power": "ON"},
        }
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize(
    "raw",
    [
        (FRAME_MAX_BYTES + 1).to_bytes(4, "big"),
        (3).to_bytes(4, "big") + b"bad",
    ],
)
def test_recv_frame_rejects_oversize_and_invalid_json(raw):
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(raw)
        with pytest.raises(FrameError):
            recv_frame(receiver, time.monotonic() + 1)
    finally:
        sender.close()
        receiver.close()


def test_send_frame_enforces_size_limit():
    sender, receiver = socket.socketpair()
    try:
        with pytest.raises(FrameError):
            send_frame(sender, {"payload": "x" * FRAME_MAX_BYTES}, time.monotonic() + 1)
    finally:
        sender.close()
        receiver.close()


def test_runtime_returns_unavailable_when_not_ready_without_starting_worker(monkeypatch):
    runtime = SeleniumRuntime()
    monkeypatch.setattr(runtime, "request_prewarm", lambda: None)

    with pytest.raises(RuntimeUnavailable):
        runtime.execute("status", {"floor": 1}, timeout_seconds=0.01)


def test_runtime_rejects_second_command_while_first_is_admitted():
    runtime = SeleniumRuntime()
    runtime._command_lock.acquire()
    try:
        with pytest.raises(RuntimeUnavailable):
            runtime.execute("status", {}, timeout_seconds=0.01)
    finally:
        runtime._command_lock.release()


def test_control_waits_for_current_refresh_then_blocks_following_refresh(monkeypatch):
    runtime = SeleniumRuntime()
    active = threading.Event()
    release = threading.Event()
    calls = []

    def command(operation, payload, deadline, before_execute, after_send):
        calls.append(operation)
        if operation == "status":
            active.set()
            assert release.wait(1)
        return 200, {"ok": True}

    monkeypatch.setattr(runtime, "_run_command", command)

    refresh = threading.Thread(
        target=lambda: runtime.execute_refresh("status", {"floor": 1}, timeout_seconds=1)
    )
    refresh.start()
    assert active.wait(1)

    control_result = []
    control = threading.Thread(
        target=lambda: control_result.append(
            runtime.execute_control("control", {}, deadline=time.monotonic() + 1)
        )
    )
    control.start()
    for _ in range(100):
        if runtime.control_waiting:
            break
        time.sleep(0.001)
    assert runtime.control_waiting is True
    with pytest.raises(RuntimeUnavailable, match="control waiting"):
        runtime.execute_refresh("status", {"floor": 2}, timeout_seconds=1)

    release.set()
    refresh.join(1)
    control.join(1)
    assert calls == ["status", "control"]
    assert control_result == [(200, {"ok": True})]


def test_only_one_control_may_wait_and_expired_control_sends_no_frame(monkeypatch):
    runtime = SeleniumRuntime()
    active = threading.Event()
    release = threading.Event()
    calls = []

    def command(operation, payload, deadline, before_execute, after_send):
        calls.append(operation)
        active.set()
        assert release.wait(1)
        return 200, {}

    monkeypatch.setattr(runtime, "_run_command", command)
    refresh = threading.Thread(
        target=lambda: runtime.execute_refresh("status", {"floor": 1}, timeout_seconds=1)
    )
    refresh.start()
    assert active.wait(1)

    first_error = []

    def wait_control():
        try:
            runtime.execute_control("control", {}, deadline=time.monotonic() + 0.05)
        except RuntimeUnavailable as error:
            first_error.append(str(error))

    first = threading.Thread(target=wait_control)
    first.start()
    for _ in range(100):
        if runtime.control_waiting:
            break
        time.sleep(0.001)
    with pytest.raises(RuntimeUnavailable, match="control already waiting"):
        runtime.execute_control("control", {}, deadline=time.monotonic() + 1)
    first.join(1)
    release.set()
    refresh.join(1)

    assert first_error == ["control deadline expired before send"]
    assert calls == ["status"]


def test_failed_prewarm_enters_cooldown_without_spawn_loop(monkeypatch):
    runtime = SeleniumRuntime()
    attempts = []

    def fail():
        attempts.append(True)
        raise FrameError("startup failed")

    monkeypatch.setattr(runtime, "_spawn_worker", fail)
    assert runtime.prewarm() is False
    assert runtime.prewarm() is False
    assert attempts == [True]
    assert runtime._cooldown_until > time.monotonic()


def test_command_frame_failure_retires_worker_and_enters_cooldown(monkeypatch):
    runtime = SeleniumRuntime()
    worker = types.SimpleNamespace(sock=object())
    runtime._worker = worker
    retired = []
    after_send = []

    monkeypatch.setattr("hems_runtime.send_frame", lambda *_args: None)
    monkeypatch.setattr(
        "hems_runtime.recv_frame", lambda *_args: (_ for _ in ()).throw(FrameError("timeout"))
    )
    monkeypatch.setattr(runtime, "_retire", lambda value: retired.append(value))

    with pytest.raises(RuntimeUnavailable, match="worker unavailable"):
        runtime.execute_control(
            "control",
            {"floor": 1},
            deadline=time.monotonic() + 1,
            after_send=lambda: after_send.append(True),
        )

    assert retired == [worker]
    assert after_send == [True]
    assert runtime._cooldown_until > time.monotonic()


def test_stop_admission_wakes_waiting_control_and_rejects_new_commands(monkeypatch):
    runtime = SeleniumRuntime()
    active = threading.Event()
    release = threading.Event()

    def command(operation, payload, deadline, before_execute, after_send):
        active.set()
        assert release.wait(1)
        return 200, {}

    monkeypatch.setattr(runtime, "_run_command", command)
    refresh = threading.Thread(
        target=lambda: runtime.execute_refresh("status", {"floor": 1}, timeout_seconds=1)
    )
    refresh.start()
    assert active.wait(1)
    errors = []

    def control():
        try:
            runtime.execute_control("control", {}, deadline=time.monotonic() + 1)
        except RuntimeUnavailable as error:
            errors.append(str(error))

    waiting = threading.Thread(target=control)
    waiting.start()
    deadline = time.monotonic() + 1
    while not runtime.control_waiting and time.monotonic() < deadline:
        time.sleep(0.001)
    runtime.stop_admission()
    waiting.join(1)
    with pytest.raises(RuntimeUnavailable, match="shutting down"):
        runtime.execute_refresh("status", {"floor": 2}, timeout_seconds=1)
    release.set()
    refresh.join(1)
    assert errors == ["runtime shutting down"]


def test_terminate_process_polls_group_kills_and_proves_group_gone(monkeypatch):
    class Process:
        pid = 999999

        def __init__(self):
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            return 0

    calls = []
    killed = False

    def killpg(pgid, sig):
        nonlocal killed
        calls.append((pgid, sig))
        if sig == signal.SIGKILL:
            killed = True
        if sig == 0 and killed:
            raise ProcessLookupError

    runtime = SeleniumRuntime(fatal_exit=lambda code: pytest.fail(f"unexpected fatal exit {code}"))
    monkeypatch.setattr(runtime, "_assert_descendants_in_group", lambda *_: None)
    monkeypatch.setattr("hems_runtime.os.killpg", killpg)
    monkeypatch.setattr("hems_runtime.RECOVERY_SECONDS", 0.02)
    process = Process()

    runtime._terminate_process(process, 999999)

    assert calls[0] == (999999, signal.SIGTERM)
    assert (999999, signal.SIGKILL) in calls
    assert calls[-1] == (999999, 0)
    assert process.waits == 1


def test_terminate_process_reaps_leader_before_proving_group_gone(monkeypatch):
    import hems_runtime

    class Process:
        pid = 999994

        def __init__(self):
            self.reaped = False

        def wait(self, timeout):
            self.reaped = True
            return 0

    process = Process()
    calls = []

    def killpg(pgid, sig):
        calls.append((pgid, sig))
        if sig == 0 and process.reaped:
            raise ProcessLookupError

    exits = []
    runtime = SeleniumRuntime(fatal_exit=exits.append)
    monkeypatch.setattr(runtime, "_assert_descendants_in_group", lambda *_: None)
    monkeypatch.setattr(runtime, "_assert_container_is_clean", lambda: None)
    monkeypatch.setattr(hems_runtime.os, "killpg", killpg)
    monkeypatch.setattr(hems_runtime, "RECOVERY_SECONDS", 0.02)

    runtime._terminate_process(process, 999994)

    assert exits == []
    assert (999994, signal.SIGKILL) not in calls


def test_unproven_group_recovery_fails_closed(monkeypatch):
    class Process:
        pid = 999998

        def wait(self, timeout):
            return 0

    exits = []
    runtime = SeleniumRuntime(fatal_exit=exits.append)
    monkeypatch.setattr(runtime, "_assert_descendants_in_group", lambda *_: None)
    monkeypatch.setattr("hems_runtime.os.killpg", lambda *_: None)

    runtime._terminate_process(Process(), 999998)

    assert exits == [1]


def test_unreadable_process_group_fails_closed(monkeypatch):
    class Process:
        pid = 999997

        def wait(self, timeout):
            return 0

    exits = []
    runtime = SeleniumRuntime(fatal_exit=exits.append)
    monkeypatch.setattr(runtime, "_assert_descendants_in_group", lambda *_: None)

    def killpg(pgid, sig):
        if sig == signal.SIGTERM:
            return
        raise PermissionError("cannot inspect process group")

    monkeypatch.setattr("hems_runtime.os.killpg", killpg)

    runtime._terminate_process(Process(), 999997)

    assert exits == [1]


def test_terminate_process_waits_for_detached_browser_cleanup(monkeypatch):
    import hems_runtime

    class Process:
        pid = 999996

        def wait(self, timeout):
            return 0

    clean_checks = []

    def assert_clean():
        clean_checks.append(True)
        if len(clean_checks) == 1:
            raise RuntimeError("detached crashpad still exiting")

    exits = []
    runtime = SeleniumRuntime(fatal_exit=exits.append)
    monkeypatch.setattr(runtime, "_assert_descendants_in_group", lambda *_: None)
    monkeypatch.setattr(runtime, "_assert_container_is_clean", assert_clean)
    monkeypatch.setattr(runtime, "_process_group_gone", lambda *_: True)
    monkeypatch.setattr(hems_runtime.os, "killpg", lambda *_: None)
    monkeypatch.setattr(hems_runtime, "RECOVERY_SECONDS", 0.1)

    runtime._terminate_process(Process(), 999996)

    assert exits == []
    assert len(clean_checks) == 2


def test_terminate_process_fails_closed_when_detached_browser_remains(monkeypatch):
    import hems_runtime

    class Process:
        pid = 999995

        def wait(self, timeout):
            return 0

    exits = []
    runtime = SeleniumRuntime(fatal_exit=exits.append)
    monkeypatch.setattr(runtime, "_assert_descendants_in_group", lambda *_: None)
    monkeypatch.setattr(
        runtime,
        "_assert_container_is_clean",
        lambda: (_ for _ in ()).throw(RuntimeError("detached crashpad remains")),
    )
    monkeypatch.setattr(runtime, "_process_group_gone", lambda *_: True)
    monkeypatch.setattr(hems_runtime.os, "killpg", lambda *_: None)
    monkeypatch.setattr(hems_runtime, "RECOVERY_SECONDS", 0.01)

    runtime._terminate_process(Process(), 999995)

    assert exits == [1]


def test_terminate_process_recovers_group_after_leader_exits_first(monkeypatch):
    import hems_runtime

    child_code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, lambda signum, frame: None); "
        "time.sleep(30)"
    )
    leader_code = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print(child.pid, flush=True)"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    child_pid = int(leader.stdout.readline())
    pgid = leader.pid
    exits = []
    runtime = SeleniumRuntime(fatal_exit=exits.append)
    monkeypatch.setattr(hems_runtime, "RECOVERY_SECONDS", 0.1)

    try:
        assert leader.wait(timeout=1) == 0
        assert os.getpgid(child_pid) == pgid

        runtime._terminate_process(leader, pgid)

        assert exits == []
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _write_proc_entry(proc_root, pid, *, ppid, pgrp, comm, cmdline):
    process_dir = proc_root / str(pid)
    process_dir.mkdir()
    (process_dir / "stat").write_text(f"{pid} ({comm}) S {ppid} {pgrp} 0 0 0\n")
    (process_dir / "comm").write_text(f"{comm}\n")
    (process_dir / "cmdline").write_bytes(b"\0".join(part.encode() for part in cmdline) + b"\0")
    return process_dir


def test_prewarm_fails_closed_before_popen_for_reparented_browser_residue(monkeypatch, tmp_path):
    import hems_runtime

    paths = [
        _write_proc_entry(
            tmp_path,
            4100,
            ppid=1,
            pgrp=4100,
            comm="python3",
            cmdline=[sys.executable, "/app/hems_runtime.py", "--selenium-worker", "--fd", "8"],
        ),
        _write_proc_entry(
            tmp_path,
            4101,
            ppid=1,
            pgrp=4101,
            comm="chrome",
            cmdline=["/opt/google/chrome/chrome", "--headless=new"],
        ),
        _write_proc_entry(
            tmp_path,
            4102,
            ppid=1,
            pgrp=4101,
            comm="chromedriver",
            cmdline=["/usr/local/bin/chromedriver", "--port=9515"],
        ),
    ]

    class Entry:
        def __init__(self, path):
            self.name = path.name
            self.path = str(path)

    class Process:
        pid = 4100

        def wait(self, timeout):
            return 0

    started = []
    monkeypatch.setattr(hems_runtime.os, "scandir", lambda _: [Entry(path) for path in paths])
    monkeypatch.setattr(hems_runtime.subprocess, "Popen", lambda *args, **kwargs: started.append(True) or Process())

    assert SeleniumRuntime().prewarm() is False
    assert started == []


def test_ready_worker_rejects_a_second_worker_in_its_process_group(monkeypatch, tmp_path):
    import hems_runtime

    paths = [
        _write_proc_entry(
            tmp_path,
            4200,
            ppid=1,
            pgrp=4200,
            comm="python3",
            cmdline=[sys.executable, "/app/hems_runtime.py", "--selenium-worker", "--fd", "8"],
        ),
        _write_proc_entry(
            tmp_path,
            4201,
            ppid=4200,
            pgrp=4200,
            comm="python3",
            cmdline=[sys.executable, "/app/hems_runtime.py", "--selenium-worker", "--fd", "9"],
        ),
    ]

    class Entry:
        def __init__(self, path):
            self.name = path.name
            self.path = str(path)

    monkeypatch.setattr(hems_runtime.os, "scandir", lambda _: [Entry(path) for path in paths])

    with pytest.raises(RuntimeError, match="worker runtime process escaped"):
        SeleniumRuntime._assert_descendants_in_group(4200, 4200)


def test_ready_worker_allows_only_reparented_chrome_crashpad_handlers(monkeypatch):
    import hems_runtime

    processes = [
        hems_runtime._ProcessInfo(4300, 1, 4300, "python3", f"{sys.executable} /app/hems_runtime.py --selenium-worker --fd 8"),
        hems_runtime._ProcessInfo(4301, 4300, 4300, "chromedriver", "/usr/local/bin/chromedriver --port=9515"),
        hems_runtime._ProcessInfo(4302, 4301, 4300, "chrome", "/opt/google/chrome/chrome --headless=new"),
        hems_runtime._ProcessInfo(
            4303,
            1,
            4303,
            "chrome_crashpad",
            "/opt/google/chrome/chrome_crashpad_handler --monitor-self --database=/tmp/crashpad",
            exe="/opt/google/chrome/chrome_crashpad_handler",
        ),
        hems_runtime._ProcessInfo(
            4304,
            1,
            4304,
            "chrome_crashpad",
            "/opt/google/chrome/chrome_crashpad_handler --no-periodic-tasks --database=/tmp/crashpad",
            exe="/opt/google/chrome/chrome_crashpad_handler",
        ),
    ]
    monkeypatch.setattr(hems_runtime.SeleniumRuntime, "_list_processes", lambda: processes)

    SeleniumRuntime._assert_descendants_in_group(4300, 4300)


@pytest.mark.parametrize(
    ("ppid", "comm", "exe", "cmdline"),
    [
        (1, "chrome", "/opt/google/chrome/chrome", "/opt/google/chrome/chrome --headless=new"),
        (
            1,
            "chrome_crashpad",
            "/tmp/not_chrome_crashpad_handler",
            "/opt/google/chrome/chrome_crashpad_handler --monitor-self",
        ),
        (
            1,
            "not_crashpad",
            "/opt/google/chrome/chrome_crashpad_handler",
            "/opt/google/chrome/chrome_crashpad_handler --monitor-self",
        ),
        (
            2,
            "chrome_crashpad",
            "/opt/google/chrome/chrome_crashpad_handler",
            "/opt/google/chrome/chrome_crashpad_handler --monitor-self",
        ),
    ],
)
def test_ready_worker_rejects_other_foreign_chrome_processes(monkeypatch, ppid, comm, exe, cmdline):
    import hems_runtime

    processes = [
        hems_runtime._ProcessInfo(4400, 1, 4400, "python3", f"{sys.executable} /app/hems_runtime.py --selenium-worker --fd 8"),
        hems_runtime._ProcessInfo(4401, 4400, 4400, "chromedriver", "/usr/local/bin/chromedriver --port=9515"),
        hems_runtime._ProcessInfo(4402, 4401, 4400, "chrome", "/opt/google/chrome/chrome --headless=new"),
        hems_runtime._ProcessInfo(4403, ppid, 4403, comm, cmdline, exe=exe),
    ]
    monkeypatch.setattr(hems_runtime.SeleniumRuntime, "_list_processes", lambda: processes)

    with pytest.raises(RuntimeError, match="worker runtime process escaped"):
        SeleniumRuntime._assert_descendants_in_group(4400, 4400)


def test_ready_worker_rejects_normal_descendant_in_another_process_group(monkeypatch):
    import hems_runtime

    processes = [
        hems_runtime._ProcessInfo(4500, 1, 4500, "python3", f"{sys.executable} /app/hems_runtime.py --selenium-worker --fd 8"),
        hems_runtime._ProcessInfo(4501, 4500, 4501, "chrome", "/opt/google/chrome/chrome --headless=new"),
    ]
    monkeypatch.setattr(hems_runtime.SeleniumRuntime, "_list_processes", lambda: processes)

    with pytest.raises(RuntimeError, match="worker runtime process escaped"):
        SeleniumRuntime._assert_descendants_in_group(4500, 4500)


def test_container_clean_check_still_rejects_detached_crashpad(monkeypatch):
    import hems_runtime

    processes = [
        hems_runtime._ProcessInfo(
            4600,
            1,
            4600,
            "chrome_crashpad",
            "/opt/google/chrome/chrome_crashpad_handler --monitor-self --database=/tmp/crashpad",
        )
    ]
    monkeypatch.setattr(
        hems_runtime.SeleniumRuntime,
        "_list_processes",
        staticmethod(lambda: processes),
    )

    with pytest.raises(RuntimeError, match="residual Selenium runtime process"):
        SeleniumRuntime()._assert_container_is_clean()


def test_spawn_worker_closes_both_socketpair_ends_when_popen_fails(monkeypatch):
    import hems_runtime

    class Socket:
        def __init__(self, fd):
            self.fd = fd
            self.closed = False

        def set_inheritable(self, value):
            assert value is True

        def fileno(self):
            return self.fd

        def close(self):
            self.closed = True

    parent = Socket(31)
    child = Socket(32)
    monkeypatch.setattr(hems_runtime.socket, "socketpair", lambda *args: (parent, child))
    monkeypatch.setattr(hems_runtime.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")))

    with pytest.raises(OSError, match="spawn failed"):
        SeleniumRuntime()._spawn_worker()

    assert parent.closed is True
    assert child.closed is True


@pytest.mark.parametrize("floors", [None, [], [1, 1], [2, 1], [0], [True], ["1"]])
def test_ready_handshake_rejects_invalid_floor_lists(floors):
    with pytest.raises(RuntimeError, match="invalid worker ready handshake"):
        SeleniumRuntime._validated_floors(floors)


def test_ready_handshake_accepts_positive_sorted_unique_floor_list():
    assert SeleniumRuntime._validated_floors([1, 3]) == (1, 3)


def test_runtime_floors_follow_ready_worker_and_clear_on_retire(monkeypatch):
    runtime = SeleniumRuntime()
    worker = types.SimpleNamespace(floors=(1, 3))
    monkeypatch.setattr(runtime, "_spawn_worker", lambda: worker)
    monkeypatch.setattr(runtime, "_terminate_worker", lambda value: None)

    assert runtime.prewarm() is True
    assert runtime.floors == (1, 3)

    runtime._retire(worker)
    assert runtime.floors is None


def test_worker_operation_never_relogs_in_request():
    class Controller:
        def __init__(self):
            self.ensure_kwargs = None

        def ensure_connection(self, **kwargs):
            self.ensure_kwargs = kwargs
            return True

        def get_current_status(self, **kwargs):
            return {"floor": 1, "power": "ON"}

    from hems_runtime import _run_operation

    controller = Controller()
    status, _ = _run_operation(controller, "status", {"floor": 1}, 123.0)

    assert status == 200
    assert controller.ensure_kwargs == {"relogin": False, "deadline": 123.0}


def test_failed_session_check_stops_operation_before_any_device_call():
    class Controller:
        def __init__(self):
            self.status_requested = False

        def ensure_connection(self, **kwargs):
            return False

        def get_current_status(self, **kwargs):
            self.status_requested = True
            return {"floor": 1, "power": "ON"}

    from hems_runtime import _run_operation

    controller = Controller()
    with pytest.raises(RuntimeUnavailable):
        _run_operation(controller, "status", {"floor": 1}, 123.0)
    assert controller.status_requested is False


def test_status_target_floor_failed_switch_returns_500_instead_of_another_floor():
    class Controller:
        def __init__(self):
            self.floor_switches = []

        def ensure_connection(self, **kwargs):
            return True

        def select_floor(self, floor, **kwargs):
            self.floor_switches.append(floor)
            return False

        def get_current_status(self, *, target_floor, deadline):
            # HEMSController currently reads the visible floor even when this
            # selection attempt fails; the runtime must not publish it as the
            # requested floor's successful status.
            assert self.select_floor(target_floor, deadline=deadline) is False
            return {"floor": 1, "power": "ON"}

    from hems_runtime import _run_operation

    controller = Controller()
    status, body = _run_operation(controller, "status", {"floor": 2}, 123.0)

    assert status == 500
    assert body == {"error": "Failed to retrieve status"}
    assert controller.floor_switches == [2]


def test_failed_floor_switch_stops_power_mode_temperature_and_status_calls():
    class Controller:
        def __init__(self):
            self.calls = []

        def ensure_connection(self, **kwargs):
            self.calls.append("ensure_connection")
            return True

        def navigate_to_smart_airs(self, **kwargs):
            self.calls.append("navigate_to_smart_airs")

        def select_floor(self, *args, **kwargs):
            self.calls.append("select_floor")
            return False

        def __getattr__(self, name):
            def method(*args, **kwargs):
                self.calls.append(name)
                return True
            return method

    from hems_runtime import _run_operation

    controller = Controller()
    status, body = _run_operation(
        controller, "control", {"floor": 2, "mode": "冷房", "temp": 26, "power": "ON"}, time.monotonic() + 25
    )
    assert status == 502
    assert body == {"error": "Floor switch not confirmed", "requested": {"floor": 2}}
    assert controller.calls == ["ensure_connection", "navigate_to_smart_airs", "select_floor"]


@pytest.mark.parametrize(
    ("payload", "failed_operation", "failed_field"),
    [
        ({"mode": "冷房"}, "set_mode", "mode"),
        ({"temp": 26}, "set_temperature", "temp"),
    ],
)
def test_control_mode_or_temperature_operation_failure_returns_502(payload, failed_operation, failed_field):
    from hems_runtime import _run_operation

    controller = _OperationController(outcomes={failed_operation: False})
    status, body = _run_operation(controller, "control", payload, time.monotonic() + 25)

    assert status == 502
    assert body["failed"] == [failed_field]
    assert failed_operation in controller.calls
    assert "get_current_status" in controller.calls


def test_control_post_verification_mismatch_returns_502_for_mode_and_temperature():
    from hems_runtime import _run_operation

    controller = _OperationController(status=_status(mode="送風", temperature=18.0))
    status, body = _run_operation(
        controller,
        "control",
        {"mode": "冷房", "temp": 26, "power": "ON"},
        time.monotonic() + 25,
    )

    assert status == 502
    assert body["failed"] == ["mode", "temp"]


@pytest.mark.parametrize("actual_temperature", [None, float("nan"), float("inf"), float("-inf")])
def test_control_post_verification_rejects_missing_or_nonfinite_temperature(actual_temperature):
    from hems_runtime import _run_operation

    controller = _OperationController(status=_status(temperature=actual_temperature))
    status, body = _run_operation(controller, "control", {"temp": 22}, time.monotonic() + 25)

    assert status == 502
    assert body["failed"] == ["temp"]


def test_control_power_off_skips_mode_and_temperature_operations():
    from hems_runtime import _run_operation

    controller = _OperationController(status=_status(mode=None, temperature=None, power="OFF"))
    status, body = _run_operation(
        controller,
        "control",
        {"mode": "冷房", "temp": 26, "power": "OFF"},
        time.monotonic() + 25,
    )

    assert status == 200
    assert body["power"] == "OFF"
    assert "toggle_power" in controller.calls
    assert "set_mode" not in controller.calls
    assert "set_temperature" not in controller.calls


def test_control_status_none_returns_500():
    from hems_runtime import _run_operation

    controller = _OperationController(status=None)
    status, body = _run_operation(controller, "control", {"power": "ON"}, time.monotonic() + 25)

    assert status == 500
    assert body == {"error": "internal error"}


def test_off_to_on_matching_mode_is_unverified_and_uses_cumulative_stage_deadlines():
    from hems_control import PowerTransition
    from hems_runtime import _run_operation

    class TransitionController(_OperationController):
        def __init__(self):
            super().__init__(status=_status(mode="冷房", temperature=26.0))
            self.deadlines = {}

        def ensure_connection(self, **kwargs):
            self.calls.append("ensure_connection")
            self.deadlines["initial"] = kwargs["deadline"]
            return True

        def toggle_power(self, *args, **kwargs):
            self.calls.append("toggle_power")
            self.deadlines["power"] = kwargs["deadline"]
            self.deadlines["mode_ready"] = kwargs["mode_ready_deadline"]
            return PowerTransition.OFF_TO_ON

        def set_mode(self, *args, **kwargs):
            self.calls.append("set_mode")
            self.deadlines["mode"] = kwargs["deadline"]
            return True

        def get_current_status(self, **kwargs):
            self.calls.append("get_current_status")
            self.deadlines["status"] = kwargs["deadline"]
            return self.status

    controller = TransitionController()
    deadline = time.monotonic() + 25
    status, body = _run_operation(
        controller,
        "control",
        {"mode": "冷房", "power": "ON"},
        deadline,
    )

    assert status == 503
    assert body["reason"] == "off_to_on_unverified"
    assert body["actual"] == controller.status
    assert controller.calls.count("toggle_power") == 1
    assert controller.calls.count("set_mode") == 1
    assert controller.deadlines["initial"] == pytest.approx(deadline - 15, abs=0.01)
    assert controller.deadlines["power"] == pytest.approx(deadline - 10, abs=0.01)
    assert controller.deadlines["mode_ready"] == pytest.approx(deadline - 10, abs=0.01)
    assert controller.deadlines["mode"] == pytest.approx(deadline - 7, abs=0.01)
    assert controller.deadlines["status"] == pytest.approx(deadline - 1, abs=0.01)


def test_deadline_shortage_before_power_click_returns_structured_503_without_click(monkeypatch):
    from hems_runtime import _run_operation

    clock = [0.0]

    class DeadlineController(_OperationController):
        def navigate_to_smart_airs(self, **kwargs):
            self.calls.append("navigate_to_smart_airs")
            clock[0] = 11.0

    monkeypatch.setattr("hems_runtime.time.monotonic", lambda: clock[0])
    controller = DeadlineController()

    status, body = _run_operation(
        controller,
        "control",
        {"mode": "冷房", "power": "ON"},
        25.0,
    )

    assert status == 503
    assert body["reason"] == "control_deadline_exhausted"
    assert "toggle_power" not in controller.calls


def test_expired_absolute_control_deadline_returns_503_before_connection_or_device_action(monkeypatch):
    from hems_runtime import _run_operation

    calls = []

    class Controller:
        def __getattr__(self, name):
            def method(*_args, **_kwargs):
                calls.append(name)
                return True
            return method

    monkeypatch.setattr("hems_runtime.time.monotonic", lambda: 25.0)
    status, body = _run_operation(Controller(), "control", {"power": "ON"}, 25.0)

    assert status == 503
    assert body["reason"] == "control_deadline_exhausted"
    assert calls == []


@pytest.mark.parametrize(
    ("boundary", "advance_after", "expected_calls"),
    [
        (10.0, "navigate_to_smart_airs", ["ensure_connection", "navigate_to_smart_airs"]),
        (15.0, "select_floor", ["ensure_connection", "navigate_to_smart_airs", "select_floor"]),
        (18.0, "toggle_power", ["ensure_connection", "navigate_to_smart_airs", "select_floor", "toggle_power"]),
        (24.0, "set_mode", ["ensure_connection", "navigate_to_smart_airs", "select_floor", "toggle_power", "set_mode"]),
    ],
)
def test_control_stage_boundaries_stop_the_next_device_action(monkeypatch, boundary, advance_after, expected_calls):
    from hems_runtime import _run_operation

    clock = [0.0]

    class Controller:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def method(*_args, **_kwargs):
                self.calls.append(name)
                if name == advance_after:
                    clock[0] = boundary
                return True
            return method

    monkeypatch.setattr("hems_runtime.time.monotonic", lambda: clock[0])
    controller = Controller()
    status, body = _run_operation(
        controller,
        "control",
        {"floor": 1, "mode": "冷房", "temp": 26, "power": "ON"},
        25.0,
    )

    assert status == 503
    assert body["reason"] == "control_deadline_exhausted"
    assert controller.calls == expected_calls


@pytest.mark.parametrize(
    ("failed_operation", "payload", "boundary", "expected_calls"),
    [
        ("select_floor", {"floor": 1}, 10.0, ["ensure_connection", "navigate_to_smart_airs", "select_floor"]),
        ("toggle_power", {"power": "ON"}, 15.0, ["ensure_connection", "navigate_to_smart_airs", "toggle_power"]),
        ("set_mode", {"mode": "冷房"}, 18.0, ["ensure_connection", "navigate_to_smart_airs", "toggle_power", "set_mode"]),
        ("set_temperature", {"temp": 26}, 18.0, ["ensure_connection", "navigate_to_smart_airs", "toggle_power", "set_temperature"]),
    ],
)
def test_false_controller_result_at_its_stage_deadline_is_503_not_502(
    monkeypatch, failed_operation, payload, boundary, expected_calls,
):
    from hems_runtime import _run_operation

    clock = [0.0]

    class Controller:
        def __init__(self):
            self.calls = []

        def _result(self, name):
            self.calls.append(name)
            if name == failed_operation:
                clock[0] = boundary
                return False
            return True

        def ensure_connection(self, **_kwargs):
            return self._result("ensure_connection")

        def navigate_to_smart_airs(self, **_kwargs):
            return self._result("navigate_to_smart_airs")

        def select_floor(self, *_args, **_kwargs):
            return self._result("select_floor")

        def toggle_power(self, *_args, **_kwargs):
            return self._result("toggle_power")

        def set_mode(self, *_args, **_kwargs):
            return self._result("set_mode")

        def set_temperature(self, *_args, **_kwargs):
            return self._result("set_temperature")

        def get_current_status(self, **_kwargs):
            return _status()

    monkeypatch.setattr("hems_runtime.time.monotonic", lambda: clock[0])
    controller = Controller()
    status, body = _run_operation(controller, "control", payload, 25.0)

    assert status == 503
    assert body["reason"] == "control_deadline_exhausted"
    assert controller.calls == expected_calls


@pytest.mark.parametrize(
    ("actual", "expected_status", "expected_reason"),
    [
        (_status(mode="冷房", temperature=26.0, power="ON"), 503, "off_to_on_unverified"),
        (_status(mode="送風", temperature=26.0, power="ON"), 502, None),
    ],
)
def test_off_to_on_intermediate_false_is_classified_from_final_actual(actual, expected_status, expected_reason):
    from hems_control import PowerTransition
    from hems_runtime import _run_operation

    class Controller(_OperationController):
        off_to_on_transition = True

        def toggle_power(self, *args, **kwargs):
            self.calls.append("toggle_power")
            return PowerTransition.UNCONFIRMED

    controller = Controller(status=actual)
    status, body = _run_operation(
        controller,
        "control",
        {"mode": "冷房", "temp": 26, "power": "ON"},
        time.monotonic() + 25,
    )

    assert status == expected_status
    assert body.get("reason") == expected_reason
    assert body.get("actual") == actual


@pytest.mark.parametrize("failed_operation", ["set_mode", "set_temperature"])
def test_off_to_on_matching_final_actual_overrides_mode_or_temperature_dom_false(failed_operation):
    from hems_control import PowerTransition
    from hems_runtime import _run_operation

    class Controller(_OperationController):
        off_to_on_transition = True

        def toggle_power(self, *args, **kwargs):
            self.calls.append("toggle_power")
            return PowerTransition.OFF_TO_ON

    actual = _status(mode="冷房", temperature=26.0, power="ON")
    controller = Controller(status=actual, outcomes={failed_operation: False})
    status, body = _run_operation(
        controller,
        "control",
        {"mode": "冷房", "temp": 26, "power": "ON"},
        time.monotonic() + 25,
    )

    assert status == 503
    assert body["reason"] == "off_to_on_unverified"
    assert body["actual"] == actual


def test_worker_converts_expected_timeout_to_result_and_remains_alive(monkeypatch):
    import hems_runtime

    class Controller:
        def __init__(self):
            self.control_calls = 0

        def login(self, **kwargs):
            pass

        def navigate_to_smart_airs(self, **kwargs):
            pass

        def detected_floors(self):
            return [1, 2]

        def ensure_connection(self, **kwargs):
            return True

        def toggle_power(self, *args, **kwargs):
            return True

        def set_mode(self, *args, **kwargs):
            self.control_calls += 1
            if self.control_calls == 1:
                raise TimeoutException("expected timeout")
            return True

        def get_current_status(self, **kwargs):
            return _status()

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "hems_control",
        types.SimpleNamespace(HEMSController=lambda *args, **kwargs: Controller()),
    )
    parent, child = socket.socketpair()
    failures = []

    def run_worker():
        try:
            hems_runtime.worker_main(child.detach())
        except FrameError as error:
            failures.append(error)

    thread = threading.Thread(target=run_worker)
    thread.start()
    try:
        ready = recv_frame(parent, time.monotonic() + 1)
        assert ready["type"] == "ready"
        first_deadline = time.monotonic() + 25
        send_frame(
            parent,
            {
                "type": "command",
                "operation": "control",
                "payload": {"mode": "冷房", "power": "ON"},
                "deadline": first_deadline,
            },
            first_deadline,
        )
        first = recv_frame(parent, first_deadline)
        assert first["status"] == 503
        assert first["body"]["reason"] == "control_deadline_exhausted"

        second_deadline = time.monotonic() + 1
        send_frame(
            parent,
            {
                "type": "command",
                "operation": "status",
                "payload": {"floor": 1},
                "deadline": second_deadline,
            },
            second_deadline,
        )
        assert recv_frame(parent, second_deadline)["status"] == 200
    finally:
        parent.close()
        thread.join(timeout=1)

    assert failures


def test_session_death_closes_worker_and_parent_retires_then_cools_down(monkeypatch):
    import hems_runtime

    class Controller:
        def login(self, **_kwargs):
            pass

        def navigate_to_smart_airs(self, **_kwargs):
            pass

        def detected_floors(self):
            return [1, 2]

        def ensure_connection(self, **_kwargs):
            return True

        def get_current_status(self, **_kwargs):
            raise RuntimeUnavailable("session died")

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "hems_control",
        types.SimpleNamespace(HEMSController=lambda *args, **kwargs: Controller()),
    )
    parent, child = socket.socketpair()
    worker_errors = []

    def run_worker():
        try:
            hems_runtime.worker_main(child.detach())
        except RuntimeUnavailable as error:
            worker_errors.append(error)

    thread = threading.Thread(target=run_worker)
    thread.start()
    assert recv_frame(parent, time.monotonic() + 1)["type"] == "ready"
    worker = types.SimpleNamespace(sock=parent)
    runtime = SeleniumRuntime()
    runtime._worker = worker
    retired = []
    monkeypatch.setattr(runtime, "_retire", lambda value: retired.append(value))
    monkeypatch.setattr(runtime, "request_prewarm", lambda: None)
    try:
        with pytest.raises(RuntimeUnavailable, match="worker unavailable"):
            runtime.execute_control(
                "status", {"floor": 1}, deadline=time.monotonic() + 1
            )
    finally:
        parent.close()
        thread.join(timeout=1)

    assert worker_errors and "session died" in str(worker_errors[0])
    assert retired == [worker]
    assert runtime._cooldown_until > time.monotonic()


@pytest.mark.parametrize(
    ("operation", "payload", "method", "security_status"),
    [
        ("lock", {"action": "lock"}, "control_lock", {"lock": "LOCKED", "shutter": "CLOSED"}),
        ("shutter", {"action": "open"}, "control_shutter", {"lock": "LOCKED", "shutter": "OPEN"}),
    ],
)
def test_security_operation_success_returns_refreshed_security_status(operation, payload, method, security_status):
    from hems_runtime import _run_operation

    controller = _OperationController(security_status=security_status)
    status, body = _run_operation(controller, operation, payload, 123.0)

    assert status == 200
    assert body == security_status
    assert controller.calls == ["ensure_connection", method, "get_security_status"]


def test_ready_worker_survives_idle_longer_than_startup_window(monkeypatch):
    class Controller:
        def login(self, **kwargs):
            pass

        def navigate_to_smart_airs(self, **kwargs):
            pass

        def detected_floors(self):
            return [1, 2]

        def ensure_connection(self, **kwargs):
            return True

        def get_current_status(self, **kwargs):
            return {"floor": 1, "power": "ON"}

        def close(self):
            pass

    import hems_runtime

    monkeypatch.setitem(sys.modules, "hems_control", types.SimpleNamespace(HEMSController=lambda *args, **kwargs: Controller()))
    monkeypatch.setattr(hems_runtime, "STARTUP_SECONDS", 0.01)
    parent, child = socket.socketpair()
    failures = []

    def run_worker():
        try:
            hems_runtime.worker_main(child.detach())
        except FrameError as error:
            failures.append(error)

    import threading

    thread = threading.Thread(target=run_worker)
    thread.start()
    ready = recv_frame(parent, time.monotonic() + 1)
    assert ready["type"] == "ready"
    time.sleep(0.03)
    assert thread.is_alive()

    deadline = time.monotonic() + 1
    send_frame(parent, {"type": "command", "operation": "status", "payload": {"floor": 1}, "deadline": deadline}, deadline)
    assert recv_frame(parent, deadline)["status"] == 200
    parent.close()
    thread.join(timeout=1)
    assert failures
