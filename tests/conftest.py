"""pytest 共通設定。

`hems_api.py` はモジュールレベルで `HEMSController(...)` を即時実行して実 Chrome を
起動しようとし、かつ `HEMS_USER` 等の環境変数が未設定だと `ValueError` を送出する。
そのため、このファイルの **モジュールレベル** (import 時) の処理順序が重要:

1. ダミー環境変数を設定する
2. `selenium.webdriver.Chrome` をフェイクdriverを返す callable に差し替える
3. その後で初めて `import hems_api` / `import hems_control` する

この順序を守らないと `import hems_api` の時点で実Chrome起動を試みてCIがハングする。
"""

import os
import time as time_module

# --- 1. ダミー環境変数 (import より前) ---
os.environ.setdefault("HEMS_USER", "test-user")
os.environ.setdefault("HEMS_PASSWORD", "test-password")
os.environ.setdefault("HEMS_URL", "http://hems.example.invalid")
os.environ.setdefault("HEMS_API_KEY_READ", "test-read-key-0123456789abcdef0123456789")
os.environ.setdefault("HEMS_API_KEY_CONTROL", "test-control-key-0123456789abcdef0123456789")

from selenium import webdriver  # noqa: E402
from selenium.common.exceptions import NoSuchElementException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402


class FakeElement:
    """Selenium WebElement の最小限フェイク。

    attrs / text は自由に設定・変更でき、click() には任意のコールバックを注入できる。
    `find_element(s)` は自身の `children` レジストリ (相対検索用) を参照する。
    """

    def __init__(self, attrs=None, text="", on_click=None):
        self.attrs = dict(attrs or {})
        self.text = text
        self.on_click = on_click
        self.children = {}

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_displayed(self):
        return bool(self.attrs.get("_displayed", True))

    def is_enabled(self):
        return bool(self.attrs.get("_enabled", True))

    def click(self):
        if self.on_click:
            self.on_click()

    def find_element(self, by, value):
        return _resolve(self.children, by, value, single=True)

    def find_elements(self, by, value):
        return _resolve(self.children, by, value, single=False)


class FakeDriver:
    """`selenium.webdriver.Chrome` の代替。

    `registry` に `(By.XXX, "selector")` -> `FakeElement` / `list[FakeElement]` を
    登録しておくと `find_element` / `find_elements` がそれを返す。登録が無ければ
    `NoSuchElementException`（find_elements は空リスト）。
    """

    def __init__(self):
        self.registry = {}
        self.current_url = ""
        self.cdp_calls = []
        self.timeouts = {}

    def execute_cdp_cmd(self, cmd, params):
        self.cdp_calls.append((cmd, params))

    def get(self, url):
        self.current_url = url

    def set_page_load_timeout(self, seconds):
        self.timeouts["page_load"] = seconds

    def set_script_timeout(self, seconds):
        self.timeouts["script"] = seconds

    def find_element(self, by, value):
        return _resolve(self.registry, by, value, single=True)

    def find_elements(self, by, value):
        return _resolve(self.registry, by, value, single=False)

    def quit(self):
        pass


def _resolve(registry, by, value, single):
    key = (by, value)
    result = registry.get(key)

    if isinstance(result, BaseException):
        raise result

    if callable(result) and not isinstance(result, FakeElement):
        result = result()
        if isinstance(result, BaseException):
            raise result

    if single:
        if result is None:
            raise NoSuchElementException(f"no element for {key}")
        if isinstance(result, list):
            if not result:
                raise NoSuchElementException(f"no element for {key}")
            return result[0]
        return result
    else:
        if result is None:
            return []
        return result if isinstance(result, list) else [result]


# --- 2. webdriver.Chrome をフェイクに差し替え (import より前) ---
webdriver.Chrome = lambda *args, **kwargs: FakeDriver()

# --- 3. ここでようやく import する ---
import hems_api  # noqa: E402
import hems_control  # noqa: E402

import pytest  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    """レート制限の内部状態がテスト間で汚染されないようにする。"""
    hems_api._last_request_time.clear()
    yield
    hems_api._last_request_time.clear()


@pytest.fixture
def api_client():
    """`hems_api.controller` を MagicMock に差し替えた Flask test_client。"""
    original_controller = hems_api.controller
    hems_api.controller = MagicMock()
    try:
        with hems_api.app.test_client() as client:
            yield client
    finally:
        hems_api.controller = original_controller


def _default_smart_airs_ready(driver):
    """Smart Airs 画面が AJAX 反映済みの状態をデフォルトで用意する。

    (電源 ON / フロアボタン1つ存在 / floor-box, header 等の必須ID) 個々のテストは
    必要なキーを上書きすればよい。
    """
    driver.registry[(By.ID, "header")] = FakeElement()
    driver.registry[(By.ID, "floor-box")] = FakeElement()
    driver.registry[(By.CSS_SELECTOR, "#floor-box li a[id^='floor_']")] = [
        FakeElement(attrs={"id": "floor_1"})
    ]
    driver.registry[(By.CSS_SELECTOR, "div.untenButton img")] = FakeElement(
        attrs={"src": "/img/btn_aircontrol_operating_on.png"}
    )
    driver.registry[(By.ID, "lalock")] = FakeElement()
    driver.registry[(By.CSS_SELECTOR, "#lalock span")] = [
        FakeElement(attrs={"class": "btn_on_left"})
    ]


@pytest.fixture
def fake_driver(monkeypatch):
    """時刻をフェイク化した (待機系が実時間で待たない) FakeDriver を返す。"""
    state = {"t": 0.0}
    monkeypatch.setattr(time_module, "time", lambda: state["t"])
    monkeypatch.setattr(time_module, "monotonic", lambda: state["t"])
    monkeypatch.setattr(time_module, "sleep", lambda s: state.__setitem__("t", state["t"] + s))

    driver = FakeDriver()
    _default_smart_airs_ready(driver)
    return driver


@pytest.fixture
def controller_with_fake_driver(fake_driver, monkeypatch):
    """FakeDriver 上に構築した実 HEMSController インスタンス。

    DOM 状態はテストごとに `controller.driver.registry[(By.XXX, "...")] = ...` で
    上書き・追加する。
    """
    monkeypatch.setattr(webdriver, "Chrome", lambda *a, **kw: fake_driver)
    monkeypatch.setattr(hems_control.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(hems_control.os, "access", lambda path, mode: True)
    controller = hems_control.HEMSController(
        "http://hems.example.invalid", "test-user", "test-password", headless=True
    )
    # navigate_to_smart_airs(force_reload=False) がリロードを省略できるよう、
    # 既に Smart Airs ページ上で state ready とみなせる URL にしておく。
    fake_driver.current_url = controller.base_url + "/DUIKC0003.cgi?gid=MUIEF0001&bid=13"
    return controller
