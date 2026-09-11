"""HEMS HTTP API。Chrome/Selenium は ``hems_runtime`` worker だけが所有する。"""

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException
import math
import os
import signal
import threading
import time

from hems_runtime import RuntimeUnavailable, SeleniumRuntime, parse_bool_env
from hems_snapshot import SnapshotCoordinator, SnapshotStore


app = Flask(__name__)

API_KEY_READ = os.environ.get("HEMS_API_KEY_READ")
API_KEY_CONTROL = os.environ.get("HEMS_API_KEY_CONTROL")
if not os.environ.get("HEMS_URL", "").strip():
    raise ValueError("HEMS_URL environment variable must be set.")
if not os.environ.get("HEMS_USER") or not os.environ.get("HEMS_PASSWORD"):
    raise ValueError("HEMS_USER and HEMS_PASSWORD environment variables must be set.")
if not API_KEY_READ:
    raise ValueError("HEMS_API_KEY_READ environment variable must be set.")
if not API_KEY_CONTROL:
    raise ValueError("HEMS_API_KEY_CONTROL environment variable must be set.")
parse_bool_env("HEMS_DISABLE_LOCK")

# import は process/Chrome を一切起動しない。互換用 controller は Selenium を持たない。
controller = None
runtime = SeleniumRuntime()
snapshots = SnapshotStore()

READ_TIMEOUT_SECONDS = 6.0
CONTROL_TIMEOUT_SECONDS = 25.0
READ_ONLY_PATHS = {"/status", "/security/status"}
CONTROL_PATHS = {"/control", "/security/lock", "/security/shutter"}
RATE_LIMIT_SECONDS = 3
RATE_LIMITED_PATHS = {"/security/lock", "/security/shutter"}
_last_request_time: dict[str, float] = {}
_rate_limit_lock = threading.Lock()
coordinator = SnapshotCoordinator(runtime, snapshots, read_timeout_seconds=READ_TIMEOUT_SECONDS)
_shutdown_lock = threading.Lock()
_shutdown_complete = False


@app.before_request
def check_api_key():
    if request.path in CONTROL_PATHS:
        g.hems_control_deadline = time.monotonic() + CONTROL_TIMEOUT_SECONDS
    api_key = request.headers.get("X-API-Key")
    if api_key not in (API_KEY_READ, API_KEY_CONTROL):
        return jsonify({"error": "Unauthorized"}), 401
    if request.path in CONTROL_PATHS and api_key != API_KEY_CONTROL:
        return jsonify({"error": "Forbidden"}), 403
    return None


def _parse_json_object():
    if not request.is_json:
        return None, (jsonify({"error": "Content-Type must be application/json"}), 400)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Request body must be a JSON object"}), 400)
    return data, None


def _known_floors():
    floors = runtime.floors
    if floors is None:
        return None, (
            jsonify({
                "error": "HEMS floor capabilities unavailable",
                "reason": "floors_unknown",
            }),
            503,
        )
    return tuple(floors), None


def _invalid_floor_response(floors=None):
    if floors:
        choices = ", ".join(str(floor) for floor in floors)
        return jsonify({"error": f"Invalid floor. Use one of: {choices}"}), 400
    return jsonify({"error": "Invalid floor"}), 400


def _security_capability_response(device):
    status, body = snapshots.response("security")
    capabilities = body.get("capabilities") if status == 200 else None
    capability = capabilities.get(device) if isinstance(capabilities, dict) else None
    if capability == "available":
        return None
    if device == "lock" and capability == "disabled":
        return jsonify({"reason": "lock_disabled"}), 403
    if capability == "not_installed":
        return jsonify({"reason": f"{device}_not_installed"}), 501
    return jsonify({
        "error": "HEMS security capabilities unavailable",
        "reason": "capabilities_unknown",
    }), 503


def _rate_limit_allows(path: str) -> bool:
    """admission 後、frame を送る直前に rate window を確認する。"""
    if path not in RATE_LIMITED_PATHS:
        return True
    now = time.monotonic()
    with _rate_limit_lock:
        last = _last_request_time.get(path)
        if last is not None and now - last < RATE_LIMIT_SECONDS:
            return False
    return True


def _record_rate_limited_command(path: str) -> None:
    """worker socket が command frame を受理した後だけ rate window を開始する。"""
    if path not in RATE_LIMITED_PATHS:
        return
    with _rate_limit_lock:
        _last_request_time[path] = time.monotonic()


def _rpc(
    operation: str,
    payload: dict,
    timeout_seconds: float,
    *,
    rate_path: str | None = None,
    refresh_keys: set[str],
):
    """runtime unavailable は全 endpoint 共通の 503 に正規化する。"""
    frame_sent = False
    confirmed_seed = False

    try:
        # Runtime の command admission は non-blocking。rate-limit は busy を消費せず、
        # worker へ frame を送れた時点でだけ window を確定する。
        def after_send() -> None:
            nonlocal frame_sent
            frame_sent = True
            if rate_path:
                _record_rate_limited_command(rate_path)

        status, body = runtime.execute_control(
            operation,
            payload,
            deadline=getattr(g, "hems_control_deadline", time.monotonic() + timeout_seconds),
            before_execute=(lambda: _rate_limit_allows(rate_path)) if rate_path else None,
            after_send=after_send,
        )
        if rate_path and status == 429:
            return jsonify({"error": "Too many requests"}), 429
        # refresh は同じ worker の解放待ちで必ずHTTP応答より後になる。確認済みの
        # 200 bodyだけは refresh と同一の実機観測値として直ちにseedする（#343, #344）。
        if status == 200 and len(refresh_keys) == 1:
            key = next(iter(refresh_keys))
            if snapshots.valid_body(key, body):
                snapshots.record_success(key, body)
                confirmed_seed = True
        elif status in (502, 503) and len(refresh_keys) == 1 and isinstance(body, dict):
            # 失敗応答で要求値を現在値として扱わず、worker が観測した actual だけをseedする。
            # 503はOFF→ON遷移の未確認結果だけが観測値を含む。
            if status == 502 or body.get("reason") == "off_to_on_unverified":
                key = next(iter(refresh_keys))
                actual = body.get("actual")
                if snapshots.valid_body(key, actual):
                    snapshots.record_success(key, actual)
        return jsonify(body), status
    except RuntimeUnavailable:
        return jsonify({
            "error": "HEMS backend unavailable",
            "reason": "backend_unavailable",
        }), 503
    finally:
        # 送信済みの不確実な結果だけを targeted refresh で再確認する。
        if frame_sent and not confirmed_seed:
            coordinator.request_targeted(refresh_keys)


@app.errorhandler(HTTPException)
def handle_http_exception(error):
    return jsonify({"error": error.description}), error.code


@app.errorhandler(Exception)
def handle_unexpected_exception(error):
    print(f"Unhandled error: {error}")
    return jsonify({"error": "internal error"}), 500


@app.route("/status", methods=["GET"])
def get_status():
    floor_param = request.args.get("floor")
    if floor_param is None:
        return _invalid_floor_response()
    try:
        floor = int(floor_param)
    except (TypeError, ValueError):
        return _invalid_floor_response()
    if str(floor) != floor_param:
        return _invalid_floor_response()
    floors, error_response = _known_floors()
    if error_response:
        return error_response
    if floor not in floors:
        return _invalid_floor_response(floors)
    status, body = snapshots.response(f"aircon:{floor}")
    body["floors"] = list(floors)
    return jsonify(body), status


@app.route("/security/status", methods=["GET"])
def get_security_status():
    status, body = snapshots.response("security")
    return jsonify(body), status


@app.route("/control", methods=["POST"])
def control_ac():
    data, error_response = _parse_json_object()
    if error_response:
        return error_response
    floor, mode, temp, power = (data.get(key) for key in ("floor", "mode", "temp", "power"))
    if floor is not None:
        if type(floor) is not int:
            return jsonify({"error": "Invalid floor"}), 400
    if mode is not None and mode not in {"暖房", "冷房", "除湿", "自動", "送風"}:
        return jsonify({"error": "Invalid mode"}), 400
    if temp is not None:
        if type(temp) not in (int, float):
            return jsonify({"error": "Invalid temp"}), 400
        temp = float(temp)
        if not math.isfinite(temp) or not temp.is_integer():
            return jsonify({"error": "Invalid temp. Must be a whole degree"}), 400
        if not 17 <= temp <= 30:
            return jsonify({"error": "Invalid temp. Must be between 17 and 30"}), 400
    if power is not None and power not in ("ON", "OFF"):
        return jsonify({"error": "Invalid power. Use 'ON' or 'OFF'"}), 400
    floors, error_response = _known_floors()
    if error_response:
        return error_response
    if floor is not None and floor not in floors:
        return _invalid_floor_response(floors)
    refresh_keys = (
        {f"aircon:{floor}"}
        if floor is not None
        else {f"aircon:{detected}" for detected in floors}
    )
    return _rpc(
        "control",
        {"floor": floor, "mode": mode, "temp": temp, "power": power},
        CONTROL_TIMEOUT_SECONDS,
        refresh_keys=refresh_keys,
    )


@app.route("/security/lock", methods=["POST"])
def control_lock():
    data, error_response = _parse_json_object()
    if error_response:
        return error_response
    action = data.get("action")
    if action not in ("lock", "unlock"):
        return jsonify({"error": "Invalid action. Use 'lock' or 'unlock'"}), 400
    capability_response = _security_capability_response("lock")
    if capability_response:
        return capability_response
    return _rpc(
        "lock",
        {"action": action},
        CONTROL_TIMEOUT_SECONDS,
        rate_path=request.path,
        refresh_keys={"security"},
    )


@app.route("/security/shutter", methods=["POST"])
def control_shutter():
    data, error_response = _parse_json_object()
    if error_response:
        return error_response
    action = data.get("action")
    if action not in ("open", "close"):
        return jsonify({"error": "Invalid action. Use 'open' or 'close'"}), 400
    capability_response = _security_capability_response("shutter")
    if capability_response:
        return capability_response
    return _rpc(
        "shutter",
        {"action": action},
        CONTROL_TIMEOUT_SECONDS,
        rate_path=request.path,
        refresh_keys={"security"},
    )


def _shutdown() -> None:
    global _shutdown_complete
    with _shutdown_lock:
        if _shutdown_complete:
            return
        _shutdown_complete = True
    runtime.stop_admission()
    coordinator.stop(timeout_seconds=READ_TIMEOUT_SECONDS + 2.0)
    runtime.wait_for_idle(CONTROL_TIMEOUT_SECONDS + 2.0)
    runtime.close()


def _handle_signal(_signum, _frame) -> None:
    _shutdown()
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    coordinator.start()
    try:
        app.run(host="0.0.0.0", port=5000, threaded=True)
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
