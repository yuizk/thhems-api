import json
from pathlib import Path
from unittest.mock import MagicMock

from jinja2 import Template
import pytest
import yaml

import hems_api


CONFIG = Path(__file__).parents[1] / "configuration_hems.yaml.example"


def test_ha_example_is_generic_and_keeps_secret_references():
    text = CONFIG.read_text()
    assert text.count("hems-api.example.invalid:5000") == 6
    assert text.count("!secret hems_api_key_read") == 3
    assert text.count("!secret hems_api_key_control") == 3


def test_ha_copy_keeps_three_independent_rest_sensors_and_timeouts():
    text = CONFIG.read_text()
    assert text.count("platform: rest") == 3
    assert text.count("scan_interval: 60") == 3
    assert text.count("timeout: 10") == 3
    assert text.count("timeout: 30") == 3
    assert "/status?floor=1" in text
    assert "/status?floor=2" in text
    assert "/security/status" in text


def test_ha_copy_retained_state_guards_reject_invalid_snapshot_values():
    text = CONFIG.read_text()
    assert text.count("not in ['unknown', 'unavailable']") >= 3
    assert "state_attr('sensor.hems_aircon_1f_status', 'floor') == 1" in text
    assert "state_attr('sensor.hems_aircon_2f_status', 'floor') == 2" in text
    assert text.count("'power') in ['ON', 'OFF']") >= 2
    assert "'lock') in ['LOCKED', 'UNLOCKED']" in text
    assert "'shutter') in ['OPEN', 'CLOSED']" in text


def test_security_publish_guards_lock_and_shutter_independently():
    config = _ha_config()
    automation = next(
        item
        for item in config["automation"]
        if item["alias"] == "HEMS Security: Sync Status to MQTT"
    )

    top_guard = automation["condition"][0]["value_template"]
    assert "not in ['unknown', 'unavailable']" in top_guard
    assert "'lock') in" not in top_guard
    assert "'shutter') in" not in top_guard

    actions = automation["action"]
    assert len(actions) == 2
    assert all("if" in action and "then" in action for action in actions)
    lock_guard = actions[0]["if"][0]["value_template"]
    shutter_guard = actions[1]["if"][0]["value_template"]
    assert "'lock') in ['LOCKED', 'UNLOCKED']" in lock_guard
    assert "'shutter') in ['OPEN', 'CLOSED']" in shutter_guard
    assert actions[0]["then"][0]["data"]["topic"] == "hems/security/lock/state"
    assert actions[1]["then"][0]["data"]["topic"] == "hems/security/shutter/state"


def test_ha_copy_does_not_add_trigger_based_poll_serialization():
    text = CONFIG.read_text()
    sensor_section = text.split("rest_command:", 1)[0]
    assert "trigger:" not in sensor_section


def _ha_config():
    return yaml.load(CONFIG.read_text(), Loader=_HaLoader)


def test_ha_copy_mqtt_climates_are_not_optimistic():
    climates = _ha_config()["mqtt"]["climate"]
    assert [climate["optimistic"] for climate in climates] == [False, False]


@pytest.mark.parametrize("floor", [1, 2])
@pytest.mark.parametrize("command_kind", ["Mode", "Temp"])
def test_ha_command_automations_fail_open_then_refresh_once(floor, command_kind):
    config = _ha_config()
    alias = f"HEMS {floor}F: Handle {command_kind} Command"
    automation = next(item for item in config["automation"] if item["alias"] == alias)
    actions = automation["action"]

    assert len(actions) == 2
    assert actions[0]["service"] == "rest_command.hems_control"
    assert actions[0]["continue_on_error"] is True
    assert actions[1] == {
        "service": "homeassistant.update_entity",
        "entity_id": f"sensor.hems_aircon_{floor}f_status",
    }
    assert "mode" not in automation
    assert all("delay" not in action for action in actions)
    assert all("repeat" not in action and "retry" not in action for action in actions)


@pytest.mark.parametrize("floor", [1, 2])
def test_ha_temperature_publish_guard_keeps_last_value_for_dry_and_fan(floor):
    config = _ha_config()
    alias = f"HEMS {floor}F: Sync Status to MQTT"
    automation = next(item for item in config["automation"] if item["alias"] == alias)
    temperature_index = next(
        index
        for index, action in enumerate(automation["action"])
        if action.get("service") == "mqtt.publish"
        and action.get("data", {}).get("topic") == f"hems/{floor}f/temp/state"
    )
    guard = automation["action"][temperature_index - 1]["value_template"]

    assert "state_attr('sensor.hems_aircon_%sf_status', 'power') == 'ON'" % floor in guard
    assert "state_attr('sensor.hems_aircon_%sf_status', 'mode') in ['暖房', '冷房', '自動']" % floor in guard
    assert "is_number(state_attr('sensor.hems_aircon_%sf_status', 'temperature'))" % floor in guard
    assert "除湿" not in guard
    assert "送風" not in guard


class _HaLoader(yaml.SafeLoader):
    """`!secret` などの HA 独自タグを解決せず読み飛ばすための Loader。"""


_HaLoader.add_multi_constructor("!", lambda loader, suffix, node: f"!{suffix}")


class _CapturingRuntime:
    def __init__(self):
        self.payloads = []
        self.floors = (1, 2)

    def execute_control(self, operation, payload, *, deadline, before_execute=None, after_send=None):
        self.payloads.append(payload)
        if after_send is not None:
            after_send()
        return 200, {"floor": payload.get("floor"), "power": payload.get("power")}


@pytest.fixture
def control_runtime(monkeypatch):
    runtime = _CapturingRuntime()
    monkeypatch.setattr(hems_api, "runtime", runtime)
    monkeypatch.setattr(hems_api, "coordinator", MagicMock())
    return runtime


def _ha_render(template, **context):
    # HA はテンプレート描画結果の前後空白を除去してからサービスデータへ渡す。
    return Template(template).render(**context).strip()


def _control_request_body(floor, mqtt_payload):
    """HA が mode command を受けて /control へ送る body を、実テンプレートで再現する。"""
    config = yaml.load(CONFIG.read_text(), Loader=_HaLoader)
    alias = f"HEMS {floor}F: Handle Mode Command"
    automation = next(item for item in config["automation"] if item["alias"] == alias)
    service_data = automation["action"][0]["data"]
    context = {"trigger": {"payload": mqtt_payload}}
    rendered = {
        key: _ha_render(value, **context) if isinstance(value, str) else value
        for key, value in service_data.items()
    }
    return json.loads(_ha_render(config["rest_command"]["hems_control"]["payload"], **rendered))


@pytest.mark.parametrize("floor", [1, 2])
@pytest.mark.parametrize(
    "mqtt_payload,expected_mode,expected_power",
    [
        ("off", None, "OFF"),
        ("heat", "暖房", "ON"),
        ("cool", "冷房", "ON"),
        ("dry", "除湿", "ON"),
        ("fan_only", "送風", "ON"),
        ("auto", "自動", "ON"),
    ],
)
def test_ha_mode_command_reaches_worker_with_expected_control_payload(
    api_client, control_runtime, floor, mqtt_payload, expected_mode, expected_power
):
    """off は mode を送らない。空文字の mode は 400 になり電源操作ごと失われる。"""
    body = _control_request_body(floor, mqtt_payload)
    assert body.get("mode") == expected_mode

    response = api_client.post(
        "/control", json=body, headers={"X-API-Key": "test-control-key"}
    )

    assert response.status_code == 200, response.get_json()
    assert control_runtime.payloads == [
        {"floor": floor, "mode": expected_mode, "temp": None, "power": expected_power}
    ]
