"""Selenium を API process から隔離する single-worker runtime。"""

from __future__ import annotations

import json
import math
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from selenium.common.exceptions import TimeoutException


FRAME_MAX_BYTES = 1024 * 1024
STARTUP_SECONDS = 45.0
COOLDOWN_SECONDS = 60.0
RECOVERY_SECONDS = 2.0
CONTROL_TOTAL_SECONDS = 25.0
CONTROL_REPLY_RESERVE_SECONDS = 1.0


def parse_bool_env(name: str, default: str = "false") -> bool:
    value = os.environ.get(name, default).lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be 'true' or 'false'")


class FrameError(RuntimeError):
    """IPC frame が protocol として不正、または期限までに完結しなかった。"""


class RuntimeUnavailable(RuntimeError):
    """worker 未ready、busy、または worker 障害。"""


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _wait_for(sock: socket.socket, *, readable: bool, deadline: float | None) -> None:
    remaining = None if deadline is None else _remaining(deadline)
    if remaining is not None and remaining <= 0:
        raise FrameError("IPC deadline expired")
    read, write, _ = select.select([sock] if readable else [], [sock] if not readable else [], [], remaining)
    if not (read if readable else write):
        raise FrameError("IPC deadline expired")


def _read_exact(sock: socket.socket, size: int, deadline: float | None) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        _wait_for(sock, readable=True, deadline=deadline)
        try:
            chunk = sock.recv(remaining)
        except OSError as error:
            raise FrameError("IPC receive failed") from error
        if not chunk:
            raise FrameError("IPC peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket, deadline: float | None) -> dict[str, Any]:
    """4-byte length + JSON object を partial stream にも安全に読み出す。"""
    size = int.from_bytes(_read_exact(sock, 4, deadline), "big")
    if size <= 0 or size > FRAME_MAX_BYTES:
        raise FrameError("invalid IPC frame size")
    try:
        value = json.loads(_read_exact(sock, size, deadline))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FrameError("invalid IPC JSON") from error
    if not isinstance(value, dict):
        raise FrameError("IPC frame must be an object")
    return value


def send_frame(sock: socket.socket, value: dict[str, Any], deadline: float) -> None:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise FrameError("IPC payload is not JSON") from error
    if not payload or len(payload) > FRAME_MAX_BYTES:
        raise FrameError("invalid IPC frame size")
    data = len(payload).to_bytes(4, "big") + payload
    sent = 0
    while sent < len(data):
        _wait_for(sock, readable=False, deadline=deadline)
        try:
            count = sock.send(data[sent:])
        except OSError as error:
            raise FrameError("IPC send failed") from error
        if count <= 0:
            raise FrameError("IPC peer closed")
        sent += count


@dataclass
class _Worker:
    process: subprocess.Popen[bytes]
    sock: socket.socket
    pgid: int
    floors: tuple[int, ...]


@dataclass(frozen=True)
class _ProcessInfo:
    pid: int
    ppid: int
    pgrp: int
    comm: str
    cmdline: str
    exe: str = ""


class SeleniumRuntime:
    """API 親で worker process group の生成・admission・回収だけを行う。"""

    def __init__(self, *, fatal_exit=os._exit) -> None:
        self._command_lock = threading.Lock()
        self._admission = threading.Condition()
        self._active_kind: str | None = None
        self._control_waiting = False
        self._admission_open = True
        self._lifecycle_lock = threading.Lock()
        self._worker: _Worker | None = None
        self._floors: tuple[int, ...] | None = None
        self._cooldown_until = 0.0
        self._prewarming = False
        self._fatal_exit = fatal_exit

    def prewarm(self) -> bool:
        """request 外で一度だけ worker を ready にする。失敗時は cooldown に入る。"""
        with self._admission:
            if not self._admission_open:
                return False
        with self._lifecycle_lock:
            if self._worker is not None:
                return True
            if self._prewarming or time.monotonic() < self._cooldown_until:
                return False
            self._prewarming = True
        try:
            worker = self._spawn_worker()
        except (FrameError, OSError, subprocess.SubprocessError, RuntimeError) as error:
            print(f"HEMS worker prewarm failed: {error}")
            with self._lifecycle_lock:
                self._cooldown_until = time.monotonic() + COOLDOWN_SECONDS
            return False
        else:
            with self._admission:
                if not self._admission_open:
                    self._terminate_worker(worker)
                    return False
                with self._lifecycle_lock:
                    self._worker = worker
                    self._floors = worker.floors
            return True
        finally:
            with self._lifecycle_lock:
                self._prewarming = False

    def request_prewarm(self) -> None:
        """HTTP request を待たせず、必要なら一回だけ background prewarm を予約する。"""
        with self._admission:
            if not self._admission_open:
                return
        with self._lifecycle_lock:
            if self._worker is not None or self._prewarming or time.monotonic() < self._cooldown_until:
                return
        threading.Thread(target=self.prewarm, name="hems-worker-prewarm", daemon=True).start()

    def execute(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
        before_execute=None,
        after_send=None,
    ) -> tuple[int, dict[str, Any]]:
        """Compatibility non-blocking admission used by direct runtime callers."""
        return self._execute_nonblocking(
            operation,
            payload,
            deadline=time.monotonic() + timeout_seconds,
            before_execute=before_execute,
            after_send=after_send,
            reject_for_control=False,
        )

    @property
    def control_waiting(self) -> bool:
        with self._admission:
            return self._control_waiting

    @property
    def floors(self) -> tuple[int, ...] | None:
        with self._lifecycle_lock:
            return self._floors

    def execute_refresh(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        """Run a low-priority refresh only when no control is active or waiting."""
        return self._execute_nonblocking(
            operation,
            payload,
            deadline=time.monotonic() + timeout_seconds,
            before_execute=None,
            after_send=None,
            reject_for_control=True,
        )

    def execute_control(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        deadline: float,
        before_execute=None,
        after_send=None,
    ) -> tuple[int, dict[str, Any]]:
        """Allow one control to wait for the current command within its absolute deadline."""
        with self._admission:
            if not self._admission_open:
                raise RuntimeUnavailable("runtime shutting down")
            if self._control_waiting or self._active_kind == "control":
                raise RuntimeUnavailable("control already waiting")
            if self._active_kind == "refresh":
                self._control_waiting = True
                try:
                    while self._active_kind is not None and self._admission_open:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeUnavailable("control deadline expired before send")
                        self._admission.wait(remaining)
                    if not self._admission_open:
                        raise RuntimeUnavailable("runtime shutting down")
                    if deadline <= time.monotonic():
                        raise RuntimeUnavailable("control deadline expired before send")
                    self._active_kind = "control"
                finally:
                    self._control_waiting = False
            else:
                if self._active_kind is not None:
                    raise RuntimeUnavailable("control busy")
                self._active_kind = "control"
        try:
            return self._run_command(operation, payload, deadline, before_execute, after_send)
        finally:
            self._finish_admission()

    def _execute_nonblocking(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        deadline: float,
        before_execute,
        after_send,
        reject_for_control: bool,
    ) -> tuple[int, dict[str, Any]]:
        with self._admission:
            if not self._admission_open:
                raise RuntimeUnavailable("runtime shutting down")
            if reject_for_control and self._control_waiting:
                raise RuntimeUnavailable("control waiting")
            if self._active_kind is not None:
                raise RuntimeUnavailable("worker busy")
            self._active_kind = "refresh" if reject_for_control else "direct"
        try:
            return self._run_command(operation, payload, deadline, before_execute, after_send)
        finally:
            self._finish_admission()

    def _finish_admission(self) -> None:
        with self._admission:
            self._active_kind = None
            self._admission.notify_all()

    def _run_command(self, operation, payload, deadline, before_execute, after_send):
        """Send one already-admitted command to the ready worker."""
        if not self._command_lock.acquire(blocking=False):
            raise RuntimeUnavailable("worker busy")
        try:
            with self._lifecycle_lock:
                worker = self._worker
            if worker is None:
                self.request_prewarm()
                raise RuntimeUnavailable("worker not ready")
            if before_execute is not None and not before_execute():
                return 429, {"error": "Too many requests"}
            if deadline <= time.monotonic():
                raise RuntimeUnavailable("command deadline expired before send")
            try:
                send_frame(worker.sock, {
                    "type": "command", "operation": operation, "payload": payload, "deadline": deadline,
                }, deadline)
                if after_send is not None:
                    after_send()
                result = recv_frame(worker.sock, deadline)
                if result.get("type") != "result" or not isinstance(result.get("status"), int) or not isinstance(result.get("body"), dict):
                    raise FrameError("invalid worker result")
                return result["status"], result["body"]
            except FrameError as error:
                print(f"HEMS worker command failed: {error}")
                self._retire(worker)
                with self._lifecycle_lock:
                    self._cooldown_until = time.monotonic() + COOLDOWN_SECONDS
                self.request_prewarm()
                raise RuntimeUnavailable("worker unavailable") from error
        finally:
            self._command_lock.release()

    def stop_admission(self) -> None:
        with self._admission:
            self._admission_open = False
            self._admission.notify_all()

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self._admission:
            while self._active_kind is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._admission.wait(remaining)
            return True

    def close(self) -> None:
        with self._lifecycle_lock:
            worker, self._worker = self._worker, None
            self._floors = None
        if worker is not None:
            self._terminate_worker(worker)

    @staticmethod
    def _validated_floors(value) -> tuple[int, ...]:
        if (
            not isinstance(value, list)
            or not value
            or any(type(floor) is not int or floor <= 0 for floor in value)
            or value != sorted(set(value))
        ):
            raise RuntimeError("invalid worker ready handshake")
        return tuple(value)

    def _spawn_worker(self) -> _Worker:
        self._assert_container_is_clean()
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.monotonic() + STARTUP_SECONDS
        try:
            child.set_inheritable(True)
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--selenium-worker", "--fd", str(child.fileno())],
                pass_fds=(child.fileno(),),
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        try:
            pgid = os.getpgid(process.pid)
            if pgid != process.pid:
                raise RuntimeError("worker is not process-group leader")
            ready = recv_frame(parent, deadline)
            if ready.get("type") != "ready" or ready.get("pid") != process.pid or ready.get("pgid") != pgid:
                raise RuntimeError("invalid worker ready handshake")
            floors = self._validated_floors(ready.get("floors"))
            self._assert_descendants_in_group(process.pid, pgid)
            return _Worker(process=process, sock=parent, pgid=pgid, floors=floors)
        except Exception:
            parent.close()
            self._terminate_process(process, process.pid)
            raise

    def _retire(self, worker: _Worker) -> None:
        with self._lifecycle_lock:
            if self._worker is worker:
                self._worker = None
                self._floors = None
        self._terminate_worker(worker)

    def _terminate_worker(self, worker: _Worker) -> None:
        try:
            worker.sock.close()
        finally:
            self._terminate_process(worker.process, worker.pgid)

    def _terminate_process(self, process: subprocess.Popen[bytes], pgid: int) -> None:
        """共有 recovery deadline 内で group を消滅まで回収できなければ fail closed。"""
        recovery_deadline = time.monotonic() + RECOVERY_SECONDS
        if self._leader_is_running(process):
            try:
                self._assert_descendants_in_group(process.pid, pgid)
            except RuntimeError:
                self._fatal_exit(1)
                return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            self._fatal_exit(1)
            return

        term_deadline = min(recovery_deadline, time.monotonic() + RECOVERY_SECONDS / 2)
        leader_exited = self._wait_for_process_exit(process, term_deadline)
        try:
            group_gone = self._wait_for_group_gone(pgid, term_deadline)
        except RuntimeError:
            self._fatal_exit(1)
            return
        if not group_gone or not leader_exited:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                self._fatal_exit(1)
                return
            if not leader_exited:
                leader_exited = self._wait_for_process_exit(process, recovery_deadline)
            try:
                group_gone = self._wait_for_group_gone(pgid, recovery_deadline)
            except RuntimeError:
                self._fatal_exit(1)
                return
            if not group_gone:
                self._fatal_exit(1)
                return

        if not leader_exited:
            self._fatal_exit(1)
            return
        try:
            group_gone = self._process_group_gone(pgid)
        except RuntimeError:
            self._fatal_exit(1)
            return
        if not group_gone:
            self._fatal_exit(1)
            return
        if not self._wait_for_container_clean(recovery_deadline):
            self._fatal_exit(1)

    def _wait_for_container_clean(self, deadline: float) -> bool:
        """detached helperの正常終了を既存recovery deadline内だけ待つ。"""
        while True:
            try:
                self._assert_container_is_clean()
            except RuntimeError:
                remaining = _remaining(deadline)
                if remaining <= 0:
                    return False
                time.sleep(min(0.05, remaining))
            else:
                return True

    @staticmethod
    def _leader_is_running(process: subprocess.Popen[bytes]) -> bool:
        poll = getattr(process, "poll", None)
        return poll is None or poll() is None

    @staticmethod
    def _process_group_gone(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError as error:
            raise RuntimeError(f"cannot inspect process group {pgid}") from error
        return False

    def _wait_for_group_gone(self, pgid: int, deadline: float) -> bool:
        while not self._process_group_gone(pgid):
            remaining = _remaining(deadline)
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True

    @staticmethod
    def _wait_for_process_exit(process: subprocess.Popen[bytes], deadline: float) -> bool:
        remaining = _remaining(deadline)
        if remaining <= 0:
            poll = getattr(process, "poll", None)
            return poll is not None and poll() is not None
        try:
            process.wait(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    @staticmethod
    def _list_processes() -> list[_ProcessInfo]:
        """container namespace の /proc から process tree と browser identity を読む。"""
        try:
            entries = [entry for entry in os.scandir("/proc") if entry.name.isdigit()]
        except OSError as error:
            raise RuntimeError("cannot enumerate container processes") from error

        processes: list[_ProcessInfo] = []
        for entry in entries:
            process_path = Path(entry.path)
            try:
                stat = (process_path / "stat").read_text()
                tail = stat.rsplit(")", 1)[1].split()
                comm = (process_path / "comm").read_text().strip()
                raw_cmdline = (process_path / "cmdline").read_bytes()
                cmdline = b" ".join(part for part in raw_cmdline.split(b"\0") if part).decode("utf-8", "replace")
                try:
                    exe = os.readlink(process_path / "exe")
                except OSError:
                    # zombie、走査中に終了したprocess、または権限外processは
                    # exe実体を証明できないためCrashpad例外の対象にしない。
                    exe = ""
                processes.append(_ProcessInfo(
                    pid=int(entry.name),
                    ppid=int(tail[1]),
                    pgrp=int(tail[2]),
                    comm=comm,
                    cmdline=cmdline,
                    exe=exe,
                ))
            except FileNotFoundError:
                # process が走査中に終了しただけなら、残留を見逃す状態ではない。
                continue
            except (IndexError, ValueError) as error:
                raise RuntimeError(f"cannot parse process metadata for pid {entry.name}") from error
            except OSError as error:
                raise RuntimeError(f"cannot read process metadata for pid {entry.name}") from error
        return processes

    @staticmethod
    def _is_worker_process(process: _ProcessInfo) -> bool:
        return Path(__file__).name in process.cmdline and "--selenium-worker" in process.cmdline

    @staticmethod
    def _is_browser_process(process: _ProcessInfo) -> bool:
        identity = f"{process.comm}\n{process.cmdline}".casefold()
        return "chrome" in identity or "chromium" in identity

    @staticmethod
    def _is_detached_crashpad_handler(process: _ProcessInfo) -> bool:
        """ChromeがPID 1へreparentした正規Crashpad handlerだけを識別する。"""
        return (
            process.ppid == 1
            and process.comm == "chrome_crashpad"
            and Path(process.exe).name == "chrome_crashpad_handler"
        )

    @staticmethod
    def _descendants(processes: list[_ProcessInfo], worker_pid: int) -> set[int]:
        descendants = {worker_pid}
        changed = True
        while changed:
            changed = False
            for process in processes:
                if process.ppid in descendants and process.pid not in descendants:
                    descendants.add(process.pid)
                    changed = True
        return descendants

    @staticmethod
    def _describe_processes(processes: list[_ProcessInfo]) -> str:
        return ", ".join(
            f"pid={process.pid} ppid={process.ppid} pgrp={process.pgrp} comm={process.comm!r} "
            f"exe={process.exe!r} cmdline={process.cmdline!r}"
            for process in processes
        )

    def _assert_container_is_clean(self) -> None:
        """専用 container に前 worker/Chrome が残る間は次 worker を絶対に spawn しない。"""
        residual = [
            process
            for process in self._list_processes()
            if self._is_worker_process(process) or self._is_browser_process(process)
        ]
        if residual:
            raise RuntimeError(f"residual Selenium runtime process: {self._describe_processes(residual)}")

    @staticmethod
    def _assert_descendants_in_group(worker_pid: int, pgid: int) -> None:
        """ready worker と Chrome/ChromeDriver が同じ tree/PGID にあることを証明する。"""
        processes = SeleniumRuntime._list_processes()
        by_pid = {process.pid: process for process in processes}
        worker = by_pid.get(worker_pid)
        if worker is None:
            raise RuntimeError(f"worker pid {worker_pid} is absent from /proc")
        descendants = SeleniumRuntime._descendants(processes, worker_pid)
        runtime_processes = [
            process
            for process in processes
            if SeleniumRuntime._is_worker_process(process) or SeleniumRuntime._is_browser_process(process)
        ]
        escaped = [process for process in (by_pid[pid] for pid in descendants) if process.pgrp != pgid]
        foreign = [
            process
            for process in runtime_processes
            if process.pid != worker_pid and process.pid not in descendants
            and not SeleniumRuntime._is_detached_crashpad_handler(process)
        ]
        extra_workers = [
            process
            for process in runtime_processes
            if process.pid != worker_pid and SeleniumRuntime._is_worker_process(process)
        ]
        if worker.pgrp != pgid or escaped or foreign or extra_workers:
            details = [worker] + escaped + foreign + extra_workers
            raise RuntimeError(
                "worker runtime process escaped expected tree or process group: "
                f"{SeleniumRuntime._describe_processes(details)}"
            )


def _result(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"type": "result", "status": status, "body": body}


def _control_deadlines(deadline: float) -> tuple[float, float, float, float]:
    """Request T+25 deadline を累積 stage 境界へ展開する。

    期限切れでも stage を導出し、Selenium 接続を含む device action の前に
    structured 503 へ写像する。
    """
    start = deadline - CONTROL_TOTAL_SECONDS
    return (
        start + 10.0,
        start + 15.0,
        start + 18.0,
        start + CONTROL_TOTAL_SECONDS - CONTROL_REPLY_RESERVE_SECONDS,
    )


def _deadline_body(payload: dict[str, Any], *, actual: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": "Control deadline exhausted",
        "reason": "control_deadline_exhausted",
        "requested": {
            "mode": payload.get("mode"),
            "temp": payload.get("temp"),
            "power": payload.get("power"),
        },
    }
    if actual is not None:
        body["actual"] = actual
    return body


def _off_to_on_unverified_body(
    payload: dict[str, Any], actual: dict[str, Any],
) -> dict[str, Any]:
    return {
        "error": "Control not confirmed after OFF to ON transition",
        "reason": "off_to_on_unverified",
        "requested": {
            "mode": payload.get("mode"),
            "temp": payload.get("temp"),
            "power": payload.get("power"),
        },
        "actual": actual,
    }


def _run_operation_impl(controller: Any, operation: str, payload: dict[str, Any], deadline: float) -> tuple[int, dict[str, Any]]:
    """既存 HTTP 契約を worker 内の Selenium 操作結果へ写像する。"""
    control_stage_deadlines = _control_deadlines(deadline) if operation == "control" else None
    if control_stage_deadlines is not None and control_stage_deadlines[0] <= time.monotonic():
        return 503, _deadline_body(payload)
    connection_deadline = control_stage_deadlines[0] if control_stage_deadlines is not None else deadline
    if not controller.ensure_connection(relogin=False, deadline=connection_deadline):
        if control_stage_deadlines is not None and connection_deadline <= time.monotonic():
            return 503, _deadline_body(payload)
        raise RuntimeUnavailable("HEMS session is unavailable")
    if operation == "status":
        target_floor = payload.get("floor")
        status = controller.get_current_status(target_floor=target_floor, deadline=deadline)
        if not status or (target_floor is not None and status.get("floor") != target_floor):
            return 500, {"error": "Failed to retrieve status"}
        return 200, status
    if operation == "security_status":
        status = controller.get_security_status(deadline=deadline)
        return (200, status) if status else (500, {"error": "Failed to retrieve security status"})
    if operation == "lock":
        if not controller.control_lock(payload["action"], deadline=deadline):
            return 500, {"error": "Failed to control lock"}
        return 200, controller.get_security_status(deadline=deadline)
    if operation == "shutter":
        if not controller.control_shutter(payload["action"], deadline=deadline):
            return 500, {"error": "Failed to control shutter"}
        return 200, controller.get_security_status(deadline=deadline)
    if operation != "control":
        raise FrameError("unknown worker operation")

    requested = {"mode": payload.get("mode"), "temp": payload.get("temp"), "power": payload.get("power")}
    stage_deadlines = control_stage_deadlines
    initial_deadline, power_deadline, send_deadline, final_deadline = stage_deadlines

    def stage_available(stage_deadline: float) -> bool:
        return stage_deadline > time.monotonic()

    def exhausted() -> tuple[int, dict[str, Any]]:
        return 503, _deadline_body(payload)

    if not stage_available(initial_deadline):
        return exhausted()
    controller.navigate_to_smart_airs(force_reload=True, deadline=initial_deadline)
    if not stage_available(initial_deadline):
        return exhausted()
    floor, mode, temp, power = (payload.get(key) for key in ("floor", "mode", "temp", "power"))
    failed: list[str] = []
    if floor is not None:
        if not stage_available(initial_deadline):
            return exhausted()
        if not controller.select_floor(floor, deadline=initial_deadline):
            if not stage_available(initial_deadline):
                return exhausted()
            return 502, {"error": "Floor switch not confirmed", "requested": {"floor": floor}}
        if not stage_available(initial_deadline):
            return exhausted()

    transitioned_from_off = False
    if power == "OFF":
        if not stage_available(power_deadline):
            return exhausted()
        power_result = controller.toggle_power("OFF", deadline=power_deadline)
        if not power_result:
            if not stage_available(power_deadline):
                return exhausted()
            failed.append("power")
    else:
        if power == "ON" or mode or temp is not None:
            if not stage_available(power_deadline):
                return exhausted()
            power_result = controller.toggle_power(
                "ON",
                deadline=power_deadline,
                mode_ready_deadline=power_deadline,
            )
            transition_value = getattr(power_result, "value", power_result)
            transitioned_from_off = (
                transition_value == "off_to_on"
                or getattr(controller, "off_to_on_transition", False) is True
            )
            if not power_result and not transitioned_from_off:
                if not stage_available(power_deadline):
                    return exhausted()
                return 502, {"error": "Control not confirmed", "requested": requested, "failed": ["power"]}
            if not stage_available(power_deadline):
                return exhausted()
        if mode:
            if not stage_available(send_deadline):
                return exhausted()
            if not controller.set_mode(mode, deadline=send_deadline):
                if not stage_available(send_deadline):
                    return exhausted()
                if not transitioned_from_off:
                    failed.append("mode")
        if temp is not None:
            if not stage_available(send_deadline):
                return exhausted()
            if not controller.set_temperature(temp, deadline=send_deadline):
                if not stage_available(send_deadline):
                    return exhausted()
                if not transitioned_from_off:
                    failed.append("temp")
    if not stage_available(final_deadline):
        return exhausted()
    status = controller.get_current_status(target_floor=floor, deadline=final_deadline)
    if not stage_available(final_deadline):
        return exhausted(actual=status)
    if status is None:
        return 500, {"error": "internal error"}
    if mode is not None and power != "OFF" and status.get("mode") != mode and "mode" not in failed:
        failed.append("mode")
    if temp is not None and power != "OFF":
        try:
            matches = math.isfinite(float(status.get("temperature"))) and round(float(status["temperature"])) == round(float(temp))
        except (KeyError, TypeError, ValueError, OverflowError):
            matches = False
        if not matches and "temp" not in failed:
            failed.append("temp")
    if power is not None and status.get("power") != power and "power" not in failed:
        failed.append("power")
    if floor is not None and status.get("floor") != floor and "floor" not in failed:
        failed.append("floor")
    if failed:
        return 502, {"error": "Control not confirmed", "requested": requested, "actual": status, "failed": failed}
    if transitioned_from_off and (mode or temp is not None):
        return 503, _off_to_on_unverified_body(payload, status)
    return 200, status


def _run_operation(controller: Any, operation: str, payload: dict[str, Any], deadline: float) -> tuple[int, dict[str, Any]]:
    """Translate expected Selenium time exhaustion into a worker result."""
    try:
        return _run_operation_impl(controller, operation, payload, deadline)
    except TimeoutException:
        return 503, _deadline_body(payload)


def _required_hems_url() -> str:
    """Return the configured device URL, refusing an unsafe implicit target."""
    url = os.environ.get("HEMS_URL", "").strip()
    if not url:
        raise ValueError("HEMS_URL environment variable must be set.")
    return url


def worker_main(fd: int) -> int:
    """subprocess entrypoint。HEMSController import/Chrome 起動はここだけで行う。"""
    from hems_control import HEMSController

    hems_url = _required_hems_url()
    sock = socket.socket(fileno=fd)
    startup_deadline = time.monotonic() + STARTUP_SECONDS
    controller = HEMSController(
        hems_url,
        os.environ["HEMS_USER"], os.environ["HEMS_PASSWORD"], headless=True,
    )
    try:
        controller.login(deadline=startup_deadline)
        controller.navigate_to_smart_airs(deadline=startup_deadline)
        send_frame(
            sock,
            {
                "type": "ready",
                "pid": os.getpid(),
                "pgid": os.getpgrp(),
                "floors": controller.detected_floors(),
            },
            startup_deadline,
        )
        while True:
            command = recv_frame(sock, None)
            if command.get("type") != "command" or not isinstance(command.get("operation"), str) or not isinstance(command.get("payload"), dict) or not isinstance(command.get("deadline"), (int, float)):
                raise FrameError("invalid worker command")
            try:
                status, body = _run_operation(controller, command["operation"], command["payload"], float(command["deadline"]))
            except TimeoutException:
                # Controller wait exhaustion is an expected result-unknown state;
                # it must not destroy the Selenium worker or cause an implicit retry.
                status, body = 503, _deadline_body(command["payload"])
            send_frame(sock, _result(status, body), float(command["deadline"]))
    finally:
        try:
            controller.close()
        except Exception:
            pass
        sock.close()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--selenium-worker" and sys.argv[2] == "--fd":
        raise SystemExit(worker_main(int(sys.argv[3])))
    raise SystemExit("hems_runtime.py is an internal worker entrypoint")
