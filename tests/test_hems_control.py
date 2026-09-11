"""hems_control.py (Selenium DOM 操作 / 業務判断) のテスト。

`controller_with_fake_driver` フィクスチャで実 `HEMSController` を FakeDriver 上に
構築し、DOM 状態をテストごとに `controller.driver.registry[...]` で設定する。
"""

import os
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.remote.client_config import ClientConfig

from tests.conftest import FakeElement, FakeDriver
import hems_control


# ---- toggle_power ----


def test_diagnostic_requires_url_when_env_is_missing(monkeypatch, capsys):
    monkeypatch.delenv("HEMS_URL", raising=False)

    assert hems_control.main(["--user", "u", "--password", "p", "--status"]) == 2
    assert "HEMS_URL" in capsys.readouterr().err


def test_diagnostic_rejects_floor_outside_detected_list(monkeypatch, capsys):
    calls = []

    class Controller:
        def __init__(self, *args, **kwargs):
            pass

        def login(self):
            calls.append("login")

        def navigate_to_smart_airs(self):
            calls.append("navigate")

        def detected_floors(self):
            return [1, 3]

        def get_current_status(self, **kwargs):
            calls.append("status")
            return {"floor": 1}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(hems_control, "HEMSController", Controller)

    assert hems_control.main(
        [
            "--url",
            "http://hems.example.invalid",
            "--user",
            "u",
            "--password",
            "p",
            "--status",
            "--floor",
            "2",
        ]
    ) == 2
    assert "detected floors: 1, 3" in capsys.readouterr().err
    assert calls == ["login", "navigate", "close"]

def test_toggle_power_unknown_state_refuses_to_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    # 電源 img の src がトークンを含まない = 状態不明
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/unknown.png"}
    )
    clicked = {"count": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )

    assert controller.toggle_power("ON") is hems_control.PowerTransition.UNCONFIRMED
    assert clicked["count"] == 0


def test_toggle_power_already_target_state_skips_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    clicked = {"count": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )

    assert controller.toggle_power("ON") is hems_control.PowerTransition.ALREADY_ON
    assert clicked["count"] == 0


def test_toggle_power_click_confirmed(controller_with_fake_driver):
    controller = controller_with_fake_driver
    power_icon = FakeElement(attrs={"src": "/img/btn_aircontrol_stop_off.png"})
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = power_icon

    def on_click():
        power_icon.attrs["src"] = "/img/btn_aircontrol_operating_on.png"

    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(on_click=on_click)
    controller.driver.registry[(By.CSS_SELECTOR, "#mode-box span")] = [
        FakeElement(attrs={"class": "btn_mode_on"})
    ]

    assert controller.toggle_power("ON") is hems_control.PowerTransition.OFF_TO_ON


def test_toggle_power_click_not_confirmed_times_out(controller_with_fake_driver):
    controller = controller_with_fake_driver
    power_icon = FakeElement(attrs={"src": "/img/btn_aircontrol_stop_off.png"})
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = power_icon
    # クリックしても状態が変わらないシナリオ (現実には反映失敗)
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(on_click=lambda: None)

    assert controller.toggle_power("ON") is hems_control.PowerTransition.UNCONFIRMED


def test_toggle_power_non_session_click_failure_stays_unconfirmed(
    controller_with_fake_driver, monkeypatch
):
    controller = controller_with_fake_driver
    monkeypatch.setattr(controller, "navigate_to_smart_airs", lambda **_kwargs: None)
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_stop_off.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(
        on_click=lambda: (_ for _ in ()).throw(
            ElementClickInterceptedException("temporary overlay")
        )
    )

    assert controller.toggle_power("ON") is hems_control.PowerTransition.UNCONFIRMED


def test_toggle_power_session_death_propagates_for_worker_recovery(
    controller_with_fake_driver, monkeypatch
):
    controller = controller_with_fake_driver
    monkeypatch.setattr(controller, "navigate_to_smart_airs", lambda **_kwargs: None)
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = WebDriverException(
        "invalid session id: session deleted"
    )

    with pytest.raises(WebDriverException, match="invalid session"):
        controller.toggle_power("ON")


def test_toggle_power_requires_mode_ready_after_off_to_on(controller_with_fake_driver):
    controller = controller_with_fake_driver
    power_icon = FakeElement(attrs={"src": "/img/btn_aircontrol_stop_off.png"})
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = power_icon
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(
        on_click=lambda: power_icon.attrs.__setitem__("src", "/img/btn_aircontrol_operating_on.png")
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#mode-box span")] = []

    assert controller.toggle_power("ON") is hems_control.PowerTransition.UNCONFIRMED


def test_toggle_power_reports_off_to_on_only_after_mode_ready(controller_with_fake_driver):
    controller = controller_with_fake_driver
    power_icon = FakeElement(attrs={"src": "/img/btn_aircontrol_stop_off.png"})
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = power_icon
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(
        on_click=lambda: power_icon.attrs.__setitem__("src", "/img/btn_aircontrol_operating_on.png")
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#mode-box span")] = [
        FakeElement(attrs={"class": "btn_mode_on"})
    ]

    result = controller.toggle_power("ON")

    assert result is hems_control.PowerTransition.OFF_TO_ON
    assert controller.off_to_on_transition is True
    assert controller.power_confirmed is True
    assert controller.mode_ready is True


def test_deadline_clamps_selenium_waits(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    monkeypatch.setattr(hems_control.time, "monotonic", lambda: 100.0)

    controller._clamp_deadline_timeouts(104.0)

    assert controller.driver.timeouts == {"page_load": 4.0, "script": 4.0}


def test_toggle_power_uses_separate_mode_ready_deadline(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    power_icon = FakeElement(attrs={"src": "/img/btn_aircontrol_stop_off.png"})
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = power_icon
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(
        on_click=lambda: power_icon.attrs.__setitem__("src", "/img/btn_aircontrol_operating_on.png")
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#mode-box span")] = [
        FakeElement(attrs={"class": "btn_mode_on"})
    ]
    deadlines = []
    monkeypatch.setattr(
        controller,
        "_wait_until",
        lambda predicate, *, timeout, deadline=None: deadlines.append(deadline) or True,
    )

    assert controller.toggle_power("ON", deadline=20.0, mode_ready_deadline=30.0)
    assert deadlines == [20.0, 30.0]


@pytest.mark.parametrize("action", ["floor", "power", "mode"])
def test_ac_clicks_are_suppressed_at_the_deadline(controller_with_fake_driver, monkeypatch, action):
    """All AC mutations use the same immediately-before-click deadline guard."""
    controller = controller_with_fake_driver
    monkeypatch.setattr(controller, "navigate_to_smart_airs", lambda **_kwargs: None)
    clicks = {"count": 0}

    if action == "floor":
        button = FakeElement(on_click=lambda: clicks.__setitem__("count", clicks["count"] + 1))
        button.children[(By.TAG_NAME, "span")] = FakeElement(attrs={"class": "btn_mode_off"})
        controller.driver.registry[(By.ID, "floor_2")] = button
        ticks = iter((99.0, 99.0, 99.0, 99.0, 99.0, 100.0))
        monkeypatch.setattr(hems_control.time, "monotonic", lambda: next(ticks))
    elif action == "power":
        monkeypatch.setattr(hems_control.time, "monotonic", lambda: 100.0)
        controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
            attrs={"src": "/img/btn_aircontrol_stop_off.png"}
        )
        controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton a")] = FakeElement(
            on_click=lambda: clicks.__setitem__("count", clicks["count"] + 1)
        )
    else:
        monkeypatch.setattr(hems_control.time, "monotonic", lambda: 100.0)
        controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
            attrs={"src": "/img/btn_aircontrol_operating_on.png"}
        )
        mode_span = FakeElement(attrs={"class": "btn_mode_off"})
        mode_span.children[(By.XPATH, "./..")] = FakeElement(
            on_click=lambda: clicks.__setitem__("count", clicks["count"] + 1)
        )
        controller.driver.registry[
            (By.XPATH, "//div[@id='mode-box']//span[normalize-space(text())='冷房']")
        ] = mode_span

    with pytest.raises(TimeoutException):
        if action == "floor":
            controller.select_floor(2, deadline=100.0)
        elif action == "power":
            controller.toggle_power("ON", deadline=100.0)
        else:
            controller.set_mode("冷房", deadline=100.0)
    assert clicks["count"] == 0


def test_temperature_deadline_shortage_never_reaches_confirm_click(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    clock = [0.0]
    monkeypatch.setattr(controller, "navigate_to_smart_airs", lambda **_kwargs: None)
    monkeypatch.setattr(hems_control.time, "monotonic", lambda: clock[0])
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = FakeElement(text="25.0")
    adjusted = {"count": 0}
    confirmed = {"count": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: adjusted.__setitem__("count", adjusted["count"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=lambda: confirmed.__setitem__("count", confirmed["count"] + 1)
    )

    def exhaust_after_adjust(_seconds, _deadline):
        clock[0] = 1.0
        return False

    monkeypatch.setattr(controller, "_sleep_until", exhaust_after_adjust)

    with pytest.raises(TimeoutException):
        controller.set_temperature(27.0, deadline=1.0)
    assert adjusted == {"count": 1}
    assert confirmed == {"count": 0}


# ---- set_mode / set_temperature: 電源 OFF 中は操作しない ----

def test_set_mode_skipped_when_power_off(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_stop_off.png"}
    )
    clicked = {"count": 0}
    mode_span = FakeElement(attrs={"class": "btn_mode_off"})
    mode_span.children[(By.XPATH, "./..")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )
    controller.driver.registry[
        (By.XPATH, "//div[@id='mode-box']//span[normalize-space(text())='暖房']")
    ] = mode_span

    assert controller.set_mode("暖房") is False
    assert clicked["count"] == 0


def test_set_mode_skipped_when_power_unknown(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/unknown.png"}
    )

    assert controller.set_mode("暖房") is False


def test_set_temperature_skipped_when_power_off(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_stop_off.png"}
    )
    clicked = {"count": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )

    assert controller.set_temperature(25.0) is False
    assert clicked["count"] == 0


# ---- set_mode: 通常動作 ----

def test_set_mode_click_confirmed(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    mode_span = FakeElement(attrs={"class": "btn_mode_off"})

    def on_click():
        mode_span.attrs["class"] = "btn_mode_on"

    mode_span.children[(By.XPATH, "./..")] = FakeElement(on_click=on_click)
    controller.driver.registry[
        (By.XPATH, "//div[@id='mode-box']//span[normalize-space(text())='冷房']")
    ] = mode_span

    assert controller.set_mode("冷房") is True


def test_set_mode_already_set_skips_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    clicked = {"count": 0}
    mode_span = FakeElement(attrs={"class": "btn_mode_on"})
    mode_span.children[(By.XPATH, "./..")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )
    controller.driver.registry[
        (By.XPATH, "//div[@id='mode-box']//span[normalize-space(text())='自動']")
    ] = mode_span

    assert controller.set_mode("自動") is True
    assert clicked["count"] == 0


# ---- set_temperature ----

def test_set_temperature_grey_confirm_button_returns_false(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_g"}
    )

    assert controller.set_temperature(25.0) is False


def test_set_temperature_already_at_target_skips_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = FakeElement(text="25.0")
    clicked = {"count": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )

    assert controller.set_temperature(25.0) is True
    assert clicked["count"] == 0


def test_set_temperature_confirmed_change(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    value_span = FakeElement(text="25.0")
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = value_span
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(on_click=lambda: None)
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(on_click=lambda: None)

    def on_confirm():
        value_span.text = "27.0"

    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(on_click=on_confirm)

    assert controller.set_temperature(27.0) is True


def test_set_temperature_increase_uses_one_degree_clicks(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    value_span = FakeElement(text="25.0")
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = value_span
    clicked = {"up": 0, "down": 0, "confirm": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("up", clicked["up"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("down", clicked["down"] + 1)
    )

    def on_confirm():
        clicked["confirm"] += 1
        value_span.text = "27.0"

    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=on_confirm
    )

    assert controller.set_temperature(27.0) is True
    assert clicked == {"up": 2, "down": 0, "confirm": 1}


def test_set_temperature_decrease_uses_one_degree_clicks(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    value_span = FakeElement(text="25.0")
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = value_span
    clicked = {"up": 0, "down": 0, "confirm": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("up", clicked["up"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("down", clicked["down"] + 1)
    )

    def on_confirm():
        clicked["confirm"] += 1
        value_span.text = "23.0"

    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=on_confirm
    )

    assert controller.set_temperature(23.0) is True
    assert clicked == {"up": 0, "down": 2, "confirm": 1}


def test_set_temperature_same_rounded_value_skips_all_clicks(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = FakeElement(
        text="22.5"
    )
    clicked = {"up": 0, "down": 0, "confirm": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("up", clicked["up"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("down", clicked["down"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("confirm", clicked["confirm"] + 1)
    )

    assert controller.set_temperature(22.0) is True
    assert clicked == {"up": 0, "down": 0, "confirm": 0}


def test_set_temperature_normalizes_decimal_current_value(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    value_span = FakeElement(text="22.5")
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = value_span
    clicked = {"up": 0, "down": 0, "confirm": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("up", clicked["up"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("down", clicked["down"] + 1)
    )

    def on_confirm():
        clicked["confirm"] += 1
        value_span.text = "24.0"

    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=on_confirm
    )

    assert controller.set_temperature(24.0) is True
    assert clicked == {"up": 2, "down": 0, "confirm": 1}


def test_set_temperature_near_lower_limit_uses_down_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    value_span = FakeElement(text="18.0")
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = value_span
    clicked = {"up": 0, "down": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("up", clicked["up"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("down", clicked["down"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=lambda: value_span.__setattr__("text", "17.0")
    )

    assert controller.set_temperature(17.0) is True
    assert clicked == {"up": 0, "down": 1}


def test_set_temperature_near_upper_limit_uses_up_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    value_span = FakeElement(text="29.0")
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = value_span
    clicked = {"up": 0, "down": 0}
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("up", clicked["up"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(
        on_click=lambda: clicked.__setitem__("down", clicked["down"] + 1)
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(
        on_click=lambda: value_span.__setattr__("text", "30.0")
    )

    assert controller.set_temperature(30.0) is True
    assert clicked == {"up": 1, "down": 0}


def test_set_temperature_not_confirmed_times_out(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box span")] = FakeElement(
        attrs={"class": "set_y"}
    )
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .value span")] = FakeElement(text="25.0")
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .up a")] = FakeElement(on_click=lambda: None)
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .down a")] = FakeElement(on_click=lambda: None)
    # 決定ボタンを押しても温度表示が変わらない (反映失敗) シナリオ
    controller.driver.registry[(By.CSS_SELECTOR, "#humidity-box .set-box a")] = FakeElement(on_click=lambda: None)

    assert controller.set_temperature(27.0) is False


# ---- select_floor ----

def test_select_floor_missing_button_and_current_floor_mismatch_returns_false(controller_with_fake_driver):
    controller = controller_with_fake_driver
    # floor_2 の ID を登録しない、かつ現在選択中フロアも判定できない (両方未登録)
    assert controller.select_floor(2) is False


def test_select_floor_missing_button_but_already_on_requested_floor_returns_true(controller_with_fake_driver):
    controller = controller_with_fake_driver
    # floor_2 ボタンは存在しないが、floor_1 が btn_mode_on = 単一フロア構成で 1F 選択中
    span = FakeElement(attrs={"class": "btn_mode_on"})
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_1 span")] = span

    assert controller.select_floor(1) is True


def test_select_floor_already_selected_skips_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    clicked = {"count": 0}
    floor_btn = FakeElement(on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1))
    floor_btn.children[(By.TAG_NAME, "span")] = FakeElement(attrs={"class": "btn_mode_on"})
    controller.driver.registry[(By.ID, "floor_1")] = floor_btn
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_1 span")] = floor_btn.children[(By.TAG_NAME, "span")]

    assert controller.select_floor(1) is True
    assert clicked["count"] == 0


def test_select_floor_click_confirmed_1_to_2(controller_with_fake_driver):
    controller = controller_with_fake_driver
    span = FakeElement(attrs={"class": "btn_mode_off"})

    def on_click():
        span.attrs["class"] = "btn_mode_on"

    floor_btn = FakeElement(on_click=on_click)
    floor_btn.children[(By.TAG_NAME, "span")] = span
    controller.driver.registry[(By.ID, "floor_2")] = floor_btn
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_2 span")] = span

    assert controller.select_floor(2) is True
    assert span.attrs["class"] == "btn_mode_on"


def test_select_floor_click_confirmed_2_to_1(controller_with_fake_driver):
    controller = controller_with_fake_driver
    span = FakeElement(attrs={"class": "btn_mode_off"})

    def on_click():
        span.attrs["class"] = "btn_mode_on"

    floor_btn = FakeElement(on_click=on_click)
    floor_btn.children[(By.TAG_NAME, "span")] = span
    controller.driver.registry[(By.ID, "floor_1")] = floor_btn
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_1 span")] = span

    assert controller.select_floor(1) is True
    assert span.attrs["class"] == "btn_mode_on"


def test_select_floor_click_not_confirmed_times_out(controller_with_fake_driver):
    controller = controller_with_fake_driver
    span = FakeElement(attrs={"class": "btn_mode_off"})
    # クリックしても btn_mode_on に変化しないシナリオ (反映失敗)
    floor_btn = FakeElement(on_click=lambda: None)
    floor_btn.children[(By.TAG_NAME, "span")] = span
    controller.driver.registry[(By.ID, "floor_2")] = floor_btn
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_2 span")] = span

    assert controller.select_floor(2) is False


def test_detected_floors_and_current_floor_are_not_two_story_fixed(
    controller_with_fake_driver,
):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "#floor-box li a[id^='floor_']")] = [
        FakeElement(attrs={"id": "floor_3"}),
        FakeElement(attrs={"id": "floor_1"}),
    ]
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_3 span")] = FakeElement(
        attrs={"class": "btn_mode_on"}
    )

    assert controller.detected_floors() == [1, 3]
    assert controller._current_floor() == 3


# ---- ensure_connection ----

def test_ensure_connection_when_already_connected_does_not_relogin(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    login_mock_calls = {"count": 0}
    monkeypatch.setattr(controller, "login", lambda: login_mock_calls.__setitem__("count", login_mock_calls["count"] + 1))

    controller.ensure_connection()

    assert login_mock_calls["count"] == 0


def test_ensure_connection_relogins_when_session_lost(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    # header が見つからない = セッション切れ（ただし致命的なクラッシュではない）
    del controller.driver.registry[(By.ID, "header")]

    login_mock_calls = {"count": 0}
    monkeypatch.setattr(
        controller, "login", lambda: login_mock_calls.__setitem__("count", login_mock_calls["count"] + 1)
    )

    controller.ensure_connection()

    assert login_mock_calls["count"] == 1


def test_ensure_connection_recreates_driver_on_dead_session(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    original_driver = controller.driver
    controller.driver.registry[(By.ID, "header")] = WebDriverException("session deleted because of page crash")
    # _init_driver() が再生成時に呼ぶ webdriver.Chrome を、毎回新しい FakeDriver を
    # 返すファクトリに差し替える (fixture 既定の "常に同じインスタンスを返す" 差し替えを上書き)。
    monkeypatch.setattr(webdriver, "Chrome", lambda *a, **kw: FakeDriver())

    login_mock_calls = {"count": 0}
    monkeypatch.setattr(
        controller, "login", lambda: login_mock_calls.__setitem__("count", login_mock_calls["count"] + 1)
    )

    controller.ensure_connection()

    assert controller.driver is not original_driver
    assert login_mock_calls["count"] == 1


# ---- control_lock / control_shutter ----

def _shutter_li(open_class="btn_off", close_class="btn_on_left"):
    visible_li = FakeElement()
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='開']")] = FakeElement(
        attrs={"class": open_class}
    )
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='閉']")] = FakeElement(
        attrs={"class": close_class}
    )
    return visible_li


def test_security_status_without_lock_keeps_shutter_available(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry.pop((By.ID, "lalock"))
    controller.driver.registry.pop((By.CSS_SELECTOR, "#lalock span"))
    controller.driver.registry[
        (
            By.XPATH,
            "//div[contains(@class,'sec03')]"
            "//li[not(contains(translate(@style,' ',''),'display:none'))]",
        )
    ] = _shutter_li()

    assert controller.get_security_status(deadline=1.0) == {
        "lock": "NOT_INSTALLED",
        "shutter": "CLOSED",
        "capabilities": {"lock": "not_installed", "shutter": "available"},
    }


def test_security_status_reports_both_available_capabilities(controller_with_fake_driver):
    controller = controller_with_fake_driver
    lock_section = FakeElement()
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='施錠']")] = (
        FakeElement(attrs={"class": "btn_on_left"})
    )
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='解錠']")] = (
        FakeElement(attrs={"class": "btn_off"})
    )
    controller.driver.registry[(By.ID, "lalock")] = lock_section
    controller.driver.registry[
        (
            By.XPATH,
            "//div[contains(@class,'sec03')]"
            "//li[not(contains(translate(@style,' ',''),'display:none'))]",
        )
    ] = _shutter_li()

    assert controller.get_security_status() == {
        "lock": "LOCKED",
        "shutter": "CLOSED",
        "capabilities": {"lock": "available", "shutter": "available"},
    }


def test_security_status_all_devices_absent_is_valid(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry.pop((By.ID, "lalock"))
    controller.driver.registry.pop((By.CSS_SELECTOR, "#lalock span"))

    assert controller.get_security_status() == {
        "lock": "NOT_INSTALLED",
        "shutter": "NOT_INSTALLED",
        "capabilities": {"lock": "not_installed", "shutter": "not_installed"},
    }


def test_security_present_lock_without_ajax_state_is_failure(controller_with_fake_driver):
    controller = controller_with_fake_driver
    controller.driver.registry[(By.CSS_SELECTOR, "#lalock span")] = []

    with pytest.raises(TimeoutException):
        controller.navigate_to_security()


def test_security_redirect_is_not_misclassified_as_no_devices(
    controller_with_fake_driver, monkeypatch
):
    controller = controller_with_fake_driver
    controller.driver.registry.pop((By.ID, "lalock"))
    controller.driver.registry.pop((By.CSS_SELECTOR, "#lalock span"))

    def redirect(_url):
        controller.driver.current_url = controller.base_url + "/MUILG0001.cgi"

    monkeypatch.setattr(controller.driver, "get", redirect)

    with pytest.raises(TimeoutException):
        controller.navigate_to_security()


def test_disable_lock_overrides_detected_dom(controller_with_fake_driver, monkeypatch):
    monkeypatch.setenv("HEMS_DISABLE_LOCK", "TrUe")
    controller = hems_control.HEMSController(
        "http://hems.example.invalid", "test-user", "test-password", headless=True
    )

    assert controller.get_security_status()["lock"] == "DISABLED"
    assert controller.security_capabilities["lock"] == "disabled"


@pytest.mark.parametrize("value", ["yes", " true ", "1", ""])
def test_invalid_disable_lock_value_fails_closed(monkeypatch, value):
    monkeypatch.setenv("HEMS_DISABLE_LOCK", value)

    with pytest.raises(ValueError, match="HEMS_DISABLE_LOCK"):
        hems_control.HEMSController(
            "http://hems.example.invalid", "test-user", "test-password", headless=True
        )


def test_control_lock_already_target_state_skips_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    lock_section = FakeElement()
    lock_span = FakeElement(attrs={"class": "btn_on_left"})
    # 非対象 (解錠) 側が off であること (排他条件) を満たしていて初めて「既に対象状態」
    unlock_span = FakeElement(attrs={"class": "btn_off"})
    clicked = {"count": 0}
    lock_span.children[(By.XPATH, "./..")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='施錠']")] = lock_span
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='解錠']")] = unlock_span
    controller.driver.registry[(By.ID, "lalock")] = lock_section

    assert controller.control_lock("lock") is True
    assert clicked["count"] == 0


def test_control_lock_click_when_not_target_state(controller_with_fake_driver):
    controller = controller_with_fake_driver
    lock_section = FakeElement()
    lock_span = FakeElement(attrs={"class": "btn_off"})

    def on_click():
        lock_span.attrs["class"] = "btn_on_left"

    lock_span.children[(By.XPATH, "./..")] = FakeElement(on_click=on_click)
    unlock_span = FakeElement(attrs={"class": "btn_off"})
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='施錠']")] = lock_span
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='解錠']")] = unlock_span
    controller.driver.registry[(By.ID, "lalock")] = lock_section

    assert controller.control_lock("lock") is True
    assert lock_span.attrs["class"] == "btn_on_left"


def test_control_lock_times_out_returns_false(controller_with_fake_driver):
    """クリック後も対象 span が btn_on にならない (タイムアウト) → fail-closed で False。"""
    controller = controller_with_fake_driver
    lock_section = FakeElement()
    lock_span = FakeElement(attrs={"class": "btn_off"})
    lock_span.children[(By.XPATH, "./..")] = FakeElement(on_click=lambda: None)
    unlock_span = FakeElement(attrs={"class": "btn_on_left"})
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='施錠']")] = lock_span
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='解錠']")] = unlock_span
    controller.driver.registry[(By.ID, "lalock")] = lock_section

    assert controller.control_lock("lock") is False


def test_control_lock_dom_inconsistent_returns_false(controller_with_fake_driver):
    """クリック後、対象・非対象の両方が btn_on のまま (DOM 不整合) → fail-closed で False。"""
    controller = controller_with_fake_driver
    lock_section = FakeElement()
    lock_span = FakeElement(attrs={"class": "btn_off"})

    def on_click():
        # 対象は on になるが、非対象側が off に戻らない不整合な DOM を再現
        lock_span.attrs["class"] = "btn_on_left"

    lock_span.children[(By.XPATH, "./..")] = FakeElement(on_click=on_click)
    unlock_span = FakeElement(attrs={"class": "btn_on_left"})
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='施錠']")] = lock_span
    lock_section.children[(By.XPATH, ".//span[normalize-space(text())='解錠']")] = unlock_span
    controller.driver.registry[(By.ID, "lalock")] = lock_section

    assert controller.control_lock("lock") is False


def test_control_shutter_already_target_state_skips_click(controller_with_fake_driver):
    controller = controller_with_fake_driver
    sec03 = FakeElement()
    sec03.attrs["class"] = "sec03"
    open_span = FakeElement(attrs={"class": "btn_on_left"})
    # 非対象 (閉) 側が off であること (排他条件) を満たしていて初めて「既に対象状態」
    close_span = FakeElement(attrs={"class": "btn_off"})
    clicked = {"count": 0}
    open_span.children[(By.XPATH, "./..")] = FakeElement(
        on_click=lambda: clicked.__setitem__("count", clicked["count"] + 1)
    )
    visible_li = FakeElement()
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='開']")] = open_span
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='閉']")] = close_span
    controller.driver.registry[
        (
            By.XPATH,
            "//div[contains(@class,'sec03')]"
            "//li[not(contains(translate(@style,' ',''),'display:none'))]",
        )
    ] = visible_li

    assert controller.control_shutter("open") is True
    assert clicked["count"] == 0


def test_control_shutter_times_out_returns_false(controller_with_fake_driver):
    """クリック後も対象 span が btn_on にならない (タイムアウト) → fail-closed で False。"""
    controller = controller_with_fake_driver
    open_span = FakeElement(attrs={"class": "btn_off"})
    open_span.children[(By.XPATH, "./..")] = FakeElement(on_click=lambda: None)
    close_span = FakeElement(attrs={"class": "btn_on_left"})
    visible_li = FakeElement()
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='開']")] = open_span
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='閉']")] = close_span
    controller.driver.registry[
        (
            By.XPATH,
            "//div[contains(@class,'sec03')]"
            "//li[not(contains(translate(@style,' ',''),'display:none'))]",
        )
    ] = visible_li

    assert controller.control_shutter("open") is False


def test_control_shutter_dom_inconsistent_returns_false(controller_with_fake_driver):
    """クリック後、対象・非対象の両方が btn_on のまま (DOM 不整合) → fail-closed で False。"""
    controller = controller_with_fake_driver
    open_span = FakeElement(attrs={"class": "btn_off"})

    def on_click():
        # 対象は on になるが、非対象側が off に戻らない不整合な DOM を再現
        open_span.attrs["class"] = "btn_on_left"

    open_span.children[(By.XPATH, "./..")] = FakeElement(on_click=on_click)
    close_span = FakeElement(attrs={"class": "btn_on_left"})
    visible_li = FakeElement()
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='開']")] = open_span
    visible_li.children[(By.XPATH, ".//span[normalize-space(text())='閉']")] = close_span
    controller.driver.registry[
        (
            By.XPATH,
            "//div[contains(@class,'sec03')]"
            "//li[not(contains(translate(@style,' ',''),'display:none'))]",
        )
    ] = visible_li

    assert controller.control_shutter("open") is False


# ---- runtime hardening / request deadlines ----

def test_init_driver_uses_explicit_chromedriver_and_configures_timeouts(monkeypatch):
    class RecordingDriver(FakeDriver):
        def __init__(self):
            super().__init__()
            self.timeouts = {}

        def set_page_load_timeout(self, seconds):
            self.timeouts["page_load"] = seconds

        def set_script_timeout(self, seconds):
            self.timeouts["script"] = seconds

    driver = RecordingDriver()
    calls = {}
    monkeypatch.setenv("CHROMEDRIVER_PATH", "/custom/chromedriver")
    monkeypatch.setattr(os.path, "isfile", lambda path: True)
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    monkeypatch.setattr(webdriver, "Chrome", lambda *args, **kwargs: calls.update(kwargs) or driver)

    hems_control.HEMSController("http://hems.example.invalid", "u", "p")

    assert isinstance(calls["service"], ChromeService)
    assert calls["service"].path == "/custom/chromedriver"
    assert "--disable-crashpad-for-testing" not in calls["options"].arguments
    assert driver.timeouts == {"page_load": 15, "script": 15}


def test_init_driver_limits_chromedriver_http_transport(monkeypatch):
    class RecordingPool:
        def __init__(self, timeout):
            self.timeout = timeout

    class RecordingExecutor:
        def __init__(self):
            self.client_config = ClientConfig("http://driver.invalid", timeout=120)
            self._conn = self._get_connection_manager()

        def _get_connection_manager(self):
            return RecordingPool(self.client_config.timeout)

    class RecordingDriver(FakeDriver):
        def __init__(self):
            super().__init__()
            self.command_executor = RecordingExecutor()

        def set_page_load_timeout(self, seconds):
            pass

        def set_script_timeout(self, seconds):
            pass

    driver = RecordingDriver()
    monkeypatch.setattr(os.path, "isfile", lambda path: True)
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    monkeypatch.setattr(webdriver, "Chrome", lambda *args, **kwargs: driver)

    hems_control.HEMSController("http://hems.example.invalid", "u", "p")

    assert driver.command_executor.client_config.timeout == 15
    assert driver.command_executor._conn.timeout == 15


def test_wait_until_clips_sleep_to_absolute_deadline(controller_with_fake_driver, monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(hems_control.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        hems_control.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    assert controller_with_fake_driver._wait_until(
        lambda: False, timeout=10, interval=0.3, deadline=1.0
    ) is False
    assert clock["now"] == 1.0


def test_smart_airs_ready_keeps_fifteen_second_timeout(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    clock = {"now": 0.0}
    monkeypatch.setattr(hems_control.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        hems_control.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(controller, "_smart_airs_state_ready", lambda: clock["now"] >= 12)

    controller._wait_smart_airs_ready()

    assert clock["now"] >= 12
    assert clock["now"] <= 15


def test_select_floor_uses_three_second_settle_after_target_class_confirmed(
    controller_with_fake_driver, monkeypatch
):
    controller = controller_with_fake_driver
    span = FakeElement(attrs={"class": "btn_mode_off"})
    floor_btn = FakeElement(on_click=lambda: span.attrs.__setitem__("class", "btn_mode_on"))
    floor_btn.children[(By.TAG_NAME, "span")] = span
    controller.driver.registry[(By.ID, "floor_2")] = floor_btn
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_2 span")] = span
    sleeps = []
    monkeypatch.setattr(hems_control.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert controller.select_floor(2) is True
    assert sleeps == [3]


def test_select_floor_fails_closed_when_three_second_settle_wont_fit_deadline(
    controller_with_fake_driver, monkeypatch
):
    controller = controller_with_fake_driver
    span = FakeElement(attrs={"class": "btn_mode_off"})
    floor_btn = FakeElement(on_click=lambda: span.attrs.__setitem__("class", "btn_mode_on"))
    floor_btn.children[(By.TAG_NAME, "span")] = span
    controller.driver.registry[(By.ID, "floor_2")] = floor_btn
    controller.driver.registry[(By.CSS_SELECTOR, "#floor_2 span")] = span
    clock = {"now": 0.0}
    sleeps = []
    monkeypatch.setattr(hems_control.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        hems_control.time,
        "sleep",
        lambda seconds: (sleeps.append(seconds), clock.__setitem__("now", clock["now"] + seconds)),
    )

    assert controller.select_floor(2, deadline=2.0) is False
    assert sleeps == []


def test_missing_chromedriver_path_fails_before_browser_start(monkeypatch):
    calls = []
    monkeypatch.setenv("CHROMEDRIVER_PATH", "/missing/chromedriver")
    monkeypatch.setattr(webdriver, "Chrome", lambda *args, **kwargs: calls.append(True))

    with pytest.raises(RuntimeError, match="CHROMEDRIVER_PATH.*not found"):
        hems_control.HEMSController("http://hems.example.invalid", "u", "p")

    assert calls == []


def test_docker_resolves_latest_stable_chrome_and_matching_driver_at_build_time():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()

    assert "ARG CHROME_VERSION" not in dockerfile
    assert "ARG CHROMEDRIVER_VERSION" not in dockerfile
    assert "ARG CHROMEDRIVER_URL" not in dockerfile
    assert "ARG CHROMEDRIVER_SHA256" not in dockerfile
    assert "google-chrome-stable=" not in dockerfile
    assert "google-chrome --product-version" in dockerfile
    assert dockerfile.count("^[0-9]+([.][0-9]+){3}$") == 3
    assert "LATEST_RELEASE_${chrome_build}" in dockerfile
    assert "chrome-for-testing-public/${chromedriver_version}/linux64/chromedriver-linux64.zip" in dockerfile
    assert 'test "$chrome_build" = "$chromedriver_build"' in dockerfile
    assert 'test "$actual_driver_version" = "$chromedriver_version"' in dockerfile
    assert "/usr/local/share/hems-api/chrome-build-info" in dockerfile
    for field in (
        "chrome_version",
        "chrome_build",
        "chromedriver_version",
        "chromedriver_url",
        "chromedriver_archive_sha256",
        "chromedriver_binary_sha256",
    ):
        assert f"{field}=" in dockerfile
    assert "# hadolint ignore=DL3008  #" in dockerfile


def test_ensure_connection_can_probe_without_relogin(controller_with_fake_driver, monkeypatch):
    controller = controller_with_fake_driver
    del controller.driver.registry[(By.ID, "header")]
    relogins = []
    monkeypatch.setattr(controller, "login", lambda: relogins.append(True))

    assert controller.ensure_connection(relogin=False) is False
    assert relogins == []
