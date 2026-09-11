import sys
import time
import argparse
import os
from enum import Enum
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.service import Service as ChromeService
from hems_runtime import parse_bool_env

# HEMS のスマート・エアーズ画面は初期 HTML 返却後に AJAX で状態を流し込む。
# 流し込み前の DOM は「電源 img=stop_off, 全モード btn_mode_off, 温度 '--', floor li の <a> が display:none」で、
# その状態でクリックや状態判定をすると逆操作になったり HEMS 内部状態が壊れる。
STATE_READY_TIMEOUT = 15
PAGE_LOAD_TIMEOUT = 15
SCRIPT_TIMEOUT = 15
TRANSPORT_TIMEOUT = 15
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "/usr/local/bin/chromedriver")

# 電源画像 (div.untenButton img) の src から AC 状態を判定。
#   ON  運転中: btn_aircontrol_operating_on.png  → "operating" を含む
#   OFF 停止中: btn_aircontrol_stop_off.png      → "stop" を含む
# AJAX 未反映状態は別途 _smart_airs_state_ready (floor_N の id 属性) で判定するので、
# ここではトークンだけで ON/OFF を区別する。
POWER_ON_TOKEN = "operating"
POWER_OFF_TOKEN = "stop"


class PowerTransition(str, Enum):
    """電源操作の確認結果。

    `ALREADY_ON` は要求状態が既に成立していた場合（OFF要求も含む）、
    `OFF_TO_ON` は OFF からクリックして ON と mode-ready を確認できた場合、
    `UNCONFIRMED` は実状態または mode-ready を確認できなかった場合を表す。
    """

    ALREADY_ON = "already_on"
    OFF_TO_ON = "off_to_on"
    UNCONFIRMED = "unconfirmed"

    def __bool__(self):
        return self is not self.UNCONFIRMED


class HEMSController:
    def __init__(self, base_url, user_id, password, headless=False):
        self.base_url = base_url.rstrip('/')
        self.user_id = user_id
        self.password = password
        self.headless = headless
        self._off_to_on_transition = False
        self._power_confirmed = False
        self._mode_ready = True
        self._disable_lock = parse_bool_env("HEMS_DISABLE_LOCK")
        self._security_capabilities = None
        self._init_driver()

    @property
    def off_to_on_transition(self):
        """直近の電源操作が OFF から ON への実クリックだったか。"""
        return self._off_to_on_transition

    @property
    def power_confirmed(self):
        """直近の電源操作で要求した電源状態を確認できたか。"""
        return self._power_confirmed

    @property
    def mode_ready(self):
        """OFF→ON 後にモード UI の反映完了を確認できたか。"""
        return self._mode_ready

    @property
    def security_capabilities(self):
        if self._security_capabilities is None:
            return None
        return dict(self._security_capabilities)

    def _init_driver(self):
        options = webdriver.ChromeOptions()
        if self.headless:
            options.add_argument('--headless=new')

        options.add_argument('--log-level=3')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-zygote')

        driver_path = os.environ.get("CHROMEDRIVER_PATH", CHROMEDRIVER_PATH)
        if not os.path.isfile(driver_path):
            raise RuntimeError(
                f"CHROMEDRIVER_PATH={driver_path!r} not found; install the pinned ChromeDriver"
            )
        if not os.access(driver_path, os.X_OK):
            raise RuntimeError(
                f"CHROMEDRIVER_PATH={driver_path!r} is not executable"
            )

        self.driver = webdriver.Chrome(
            service=ChromeService(driver_path),
            options=options,
        )
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)
        self._configure_transport_timeout()

        # Chrome 145 renderer crash 対策: common_login.js の document.write を no-op に
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': "document.write = function() {}; document.writeln = function() {};"
        })

    def _configure_transport_timeout(self):
        """Apply a finite timeout to Selenium's HTTP command transport.

        Selenium 4.44 exposes the transport's ``ClientConfig`` through the
        public ``command_executor.client_config`` property.  Rebuilding the
        pool manager makes the new value effective for an already-created
        ChromeRemoteConnection.
        """
        executor = getattr(self.driver, "command_executor", None)
        if executor is None:
            return
        executor.client_config.timeout = TRANSPORT_TIMEOUT
        executor._conn = executor._get_connection_manager()

    def _clamp_deadline_timeouts(self, deadline):
        """Selenium の各待ち時間を、要求の stage deadline より長くしない。"""
        if deadline is None:
            return
        remaining = max(0.001, self._remaining(deadline))
        self.driver.set_page_load_timeout(min(PAGE_LOAD_TIMEOUT, remaining))
        self.driver.set_script_timeout(min(SCRIPT_TIMEOUT, remaining))
        executor = getattr(self.driver, "command_executor", None)
        if executor is not None:
            client_config = getattr(executor, "client_config", None)
            if client_config is not None:
                client_config.timeout = min(TRANSPORT_TIMEOUT, remaining)
                executor._conn = executor._get_connection_manager()

    @staticmethod
    def _operation_deadline(timeout, deadline=None):
        local_deadline = time.monotonic() + timeout
        return min(local_deadline, deadline) if deadline is not None else local_deadline

    @staticmethod
    def _remaining(deadline):
        return max(0.0, deadline - time.monotonic())

    @staticmethod
    def _click_before_deadline(element, deadline=None):
        """Do not begin an AC mutation once its assigned stage has expired."""
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutException("Operation deadline exceeded before device click")
        element.click()

    def _wait_for(self, condition, timeout=10, deadline=None):
        result = {"value": None}

        def probe():
            self._clamp_deadline_timeouts(deadline)
            try:
                value = condition(self.driver)
            except Exception as error:
                if self._is_session_dead(error):
                    raise
                return False
            if value:
                result["value"] = value
                return True
            return False

        if self._wait_until(probe, timeout=timeout, interval=0.1, deadline=deadline):
            return result["value"]
        raise TimeoutException("Operation deadline exceeded")

    def _sleep_until(self, seconds, deadline):
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return False
        time.sleep(min(seconds, remaining))
        return True

    def _sleep_exact(self, seconds, deadline=None):
        """Sleep exactly ``seconds`` only when the request can afford it."""
        if deadline is not None and self._remaining(deadline) < seconds:
            return False
        time.sleep(seconds)
        return True

    @staticmethod
    def _is_session_dead(err):
        msg = str(err).lower()
        return any(t in msg for t in (
            "tab crashed",
            "session deleted",
            "invalid session",
            "no such window",
            "disconnected",
            "chrome not reachable",
        ))

    def login(self, *, deadline=None):
        """Logs into the HEMS system."""
        self._security_capabilities = None
        print(f"Connecting to {self.base_url}...")
        try:
            self._clamp_deadline_timeouts(deadline)
            self.driver.get(self.base_url + "/MUILG0001.cgi")
            
            # Wait for login form
            print("Logging in...")
            self._wait_for(
                EC.presence_of_element_located((By.NAME, "id")),
                timeout=10,
                deadline=deadline,
            )
            id_input = self.driver.find_element(By.NAME, "id")
            pass_input = self.driver.find_element(By.NAME, "password")
            
            id_input.clear()
            id_input.send_keys(self.user_id)
            pass_input.clear()
            pass_input.send_keys(self.password)
            
            # Click login button (it's an anchor tag with onclick)
            login_btn = self.driver.find_element(By.CSS_SELECTOR, "a[onclick*='document.form1.submit()']")
            login_btn.click()
            
            # Wait for navigation (check for a common element on the next page, e.g., header or nav)
            self._wait_for(
                EC.presence_of_element_located((By.ID, "header")),
                timeout=10,
                deadline=deadline,
            )
            print("Login successful.")
            
        except Exception as e:
            print(f"Login failed: {e}")
            # Don't close here, let the caller handle it or retry
            raise

    def ensure_connection(self, *, relogin=True, deadline=None):
        """Check if connected; re-login if not. Recreate driver on tab/session crash."""
        try:
            self.driver.find_element(By.ID, "header")
            return True
        except Exception as probe_err:
            if not relogin:
                print("Session probe failed; re-login disabled for this request.")
                return False
            if self._is_session_dead(probe_err):
                print(f"Chrome session is dead; recreating driver. ({type(probe_err).__name__})")
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self._init_driver()
            print("Session lost or not on correct page. Re-connecting...")
            try:
                if deadline is None:
                    self.login()
                else:
                    self.login(deadline=deadline)
            except Exception as e:
                if self._is_session_dead(e):
                    # ログイン中にもクラッシュした → もう一度 driver を作り直して 1 回だけ再試行
                    print("Driver crashed during login; recreating and retrying once.")
                    try:
                        self.driver.quit()
                    except Exception:
                        pass
                    self._init_driver()
                    if deadline is None:
                        self.login()
                    else:
                        self.login(deadline=deadline)
                    return True
                print(f"Re-connection failed: {e}")
                raise
            return True

    # ---- AJAX 反映待ち & 状態判定ヘルパ ----

    def _read_power_token(self):
        """電源 img の状態を ON / OFF / None (= 未確定) で返す。"""
        try:
            src = self.driver.find_element(
                By.CSS_SELECTOR, "div.untenButton img"
            ).get_attribute("src") or ""
        except Exception as error:
            if self._is_session_dead(error):
                raise
            return None
        if POWER_ON_TOKEN in src:
            return "ON"
        if POWER_OFF_TOKEN in src:
            return "OFF"
        return None

    def _smart_airs_state_ready(self):
        """スマート・エアーズ画面の AJAX 反映完了判定。

        floor_1 / floor_2 の id 属性は AJAX で後付けされる (pre-AJAX の <a> は
        id 無しの display:none)。これを確実な post-AJAX マーカーに使う。
        さらに電源 img の src も読めることを確認する。
        """
        try:
            floors = self.driver.find_elements(
                By.CSS_SELECTOR, "#floor-box li a[id^='floor_']"
            )
            if not floors:
                return False
            self.driver.find_element(By.CSS_SELECTOR, "div.untenButton img")
            return True
        except Exception as error:
            if self._is_session_dead(error):
                raise
            return False

    def _wait_smart_airs_ready(self, timeout=STATE_READY_TIMEOUT, deadline=None):
        if self._wait_until(
            self._smart_airs_state_ready,
            timeout=timeout,
            deadline=deadline,
        ):
            return
        raise TimeoutException("Smart Airs page state did not become ready (AJAX not populated).")

    def detected_floors(self):
        floors = set()
        for element in self.driver.find_elements(
            By.CSS_SELECTOR, "#floor-box li a[id^='floor_']"
        ):
            element_id = element.get_attribute("id") or ""
            prefix, separator, value = element_id.partition("_")
            if prefix != "floor" or separator != "_" or not value.isdigit():
                continue
            floor = int(value)
            if floor > 0:
                floors.add(floor)
        return sorted(floors)

    def _wait_until(self, predicate, timeout=10, interval=0.3, deadline=None):
        end = self._operation_deadline(timeout, deadline)
        while time.monotonic() < end:
            self._clamp_deadline_timeouts(deadline)
            try:
                if predicate() and time.monotonic() <= end:
                    return True
            except Exception as error:
                if self._is_session_dead(error):
                    raise
                pass
            if not self._sleep_until(interval, end):
                break
        return False

    def _security_container(self, device):
        if device == "lock":
            return self.driver.find_element(By.ID, "lalock")
        return self._visible_shutter_li()

    def _security_device_state_ready(self, device):
        """搭載済み機器の相反する状態 span が AJAX 反映済みかを返す。"""
        try:
            container = self._security_container(device)
            if device == "lock":
                spans = self.driver.find_elements(By.CSS_SELECTOR, "#lalock span")
            else:
                spans = [
                    container.find_element(
                        By.XPATH, f".//span[normalize-space(text())='{label}']"
                    )
                    for label in ("開", "閉")
                ]
            return any(self._has_btn_on(span.get_attribute("class")) for span in spans)
        except Exception as error:
            if self._is_session_dead(error):
                raise
            return False

    def _detect_security_capabilities(self, *, deadline=None):
        capabilities = {"lock": "disabled"} if self._disable_lock else {}
        devices = [device for device in ("lock", "shutter") if device not in capabilities]
        seen = {device: False for device in devices}

        def all_ready():
            for device in devices:
                if capabilities.get(device) == "available":
                    continue
                try:
                    self._security_container(device)
                    seen[device] = True
                except Exception as error:
                    if self._is_session_dead(error):
                        raise
                    continue
                if self._security_device_state_ready(device):
                    capabilities[device] = "available"
            return all(capabilities.get(device) == "available" for device in devices)

        self._wait_until(all_ready, timeout=STATE_READY_TIMEOUT, deadline=deadline)
        for device in devices:
            if capabilities.get(device) == "available":
                continue
            if seen[device]:
                raise TimeoutException(
                    f"Security {device} state did not become ready (AJAX not populated)."
                )
            capabilities[device] = "not_installed"
        return capabilities

    def _ensure_security_capabilities(self, *, deadline=None):
        if self._security_capabilities is None:
            self._security_capabilities = self._detect_security_capabilities(
                deadline=deadline
            )
            return

        for device, capability in self._security_capabilities.items():
            if capability == "available" and not self._wait_until(
                lambda device=device: self._security_device_state_ready(device),
                timeout=STATE_READY_TIMEOUT,
                deadline=deadline,
            ):
                raise TimeoutException(
                    f"Security {device} state did not become ready (AJAX not populated)."
                )

    def navigate_to_smart_airs(self, force_reload=False, *, deadline=None):
        """Smart Airs ページに遷移。

        HEMS UI は自動リフレッシュしないため、外部 (リモコン等) で状態が変わった
        場合に最新を取るには force_reload=True で明示的に再取得が必要。
        force_reload=False の場合、既に同ページ上で状態確定済みなら再ロードを省略。
        """
        print("Navigating to Smart Airs control page...")
        self._clamp_deadline_timeouts(deadline)
        target_url = self.base_url + "/DUIKC0003.cgi?gid=MUIEF0001&bid=13"
        try:
            if not force_reload:
                current_url = self.driver.current_url
                if "DUIKC0003.cgi" in current_url and self._smart_airs_state_ready():
                    print("Already on Smart Airs page (state ready).")
                    return

            self.driver.get(target_url)
            self._wait_for(
                EC.presence_of_element_located((By.ID, "floor-box")),
                timeout=10,
                deadline=deadline,
            )
            self._wait_smart_airs_ready(deadline=deadline)
            print("Arrived at Smart Airs control page.")
        except Exception as e:
            print(f"Navigation to Smart Airs failed: {e}")
            raise

    def navigate_to_security(self, *, deadline=None):
        print("Navigating to Security page...")
        self._clamp_deadline_timeouts(deadline)
        target_url = self.base_url + "/DUIHS0001.cgi?gid=MUIGN0001&bid=88"
        try:
            self.driver.get(target_url)
            self._wait_for(
                EC.presence_of_element_located((By.ID, "header")),
                timeout=10,
                deadline=deadline,
            )
            if "DUIHS0001.cgi" not in self.driver.current_url:
                raise TimeoutException("Security page navigation did not reach the target URL.")
            self._ensure_security_capabilities(deadline=deadline)
            print("Arrived at Security page.")
        except Exception as e:
            print(f"Navigation to Security failed: {e}")
            raise

    def _current_floor(self):
        """検出済み floor の span を見て、現在選択中のフロアを返す。"""
        for fl in self.detected_floors():
            try:
                span = self.driver.find_element(By.CSS_SELECTOR, f"#floor_{fl} span")
                if "btn_mode_on" in (span.get_attribute("class") or ""):
                    return fl
            except NoSuchElementException:
                continue
        return None

    def select_floor(self, floor, *, deadline=None):
        self.navigate_to_smart_airs(deadline=deadline)
        print(f"Selecting floor: {floor}F")
        floor_id = f"floor_{floor}"
        try:
            floor_btn = self._wait_for(
                EC.element_to_be_clickable((By.ID, floor_id)),
                timeout=10,
                deadline=deadline,
            )
        except TimeoutException:
            actual = self._current_floor()
            if actual == floor:
                print(f"Floor button {floor_id} not visible, but floor {floor}F is already selected.")
                return True
            print(f"Floor button {floor_id} not visible and current floor ({actual}) does not match requested.")
            return False

        span = floor_btn.find_element(By.TAG_NAME, "span")
        if "btn_mode_on" in (span.get_attribute("class") or ""):
            print(f"Floor {floor}F is already selected.")
            return True

        self._click_before_deadline(floor_btn, deadline)

        ok = self._wait_until(
            lambda: "btn_mode_on" in (
                self.driver.find_element(By.CSS_SELECTOR, f"#{floor_id} span").get_attribute("class") or ""
            ),
            timeout=10,
            deadline=deadline,
        )
        if not ok:
            print(f"Floor switch to {floor}F was not confirmed within timeout.")
            return False

        settle_deadline = deadline if deadline is not None else self._operation_deadline(3)
        if not self._sleep_exact(3, settle_deadline):
            print(f"Floor switch to {floor}F has no room for the 3-second settle.")
            return False
        self._wait_smart_airs_ready(deadline=deadline)
        print(f"Selected floor {floor}F.")
        return True

    def set_mode(self, mode, *, deadline=None):
        self.navigate_to_smart_airs(deadline=deadline)
        print(f"Setting mode to: {mode}")

        # 電源 OFF / 状態不明のときモードボタンは無効。クリックすると HEMS が壊れる。
        power = self._read_power_token()
        if power != "ON":
            print(f"Power is {power or 'UNKNOWN'}; mode buttons are disabled. Skipping set_mode.")
            return False

        xpath = f"//div[@id='mode-box']//span[normalize-space(text())='{mode}']"
        try:
            mode_span = self.driver.find_element(By.XPATH, xpath)
        except NoSuchElementException:
            print(f"Mode '{mode}' not found.")
            return False

        if "btn_mode_on" in (mode_span.get_attribute("class") or ""):
            print(f"Mode is already set to {mode}.")
            return True

        try:
            self._click_before_deadline(mode_span.find_element(By.XPATH, "./.."), deadline)
        except TimeoutException:
            raise
        except Exception as e:
            if self._is_session_dead(e):
                raise
            print(f"Failed to click mode button: {e}")
            return False

        ok = self._wait_until(
            lambda: "btn_mode_on" in (
                self.driver.find_element(By.XPATH, xpath).get_attribute("class") or ""
            ),
            timeout=10,
            deadline=deadline,
        )
        if ok:
            print(f"Mode set to {mode}.")
        else:
            print(f"Mode change to {mode} not confirmed within timeout.")
        return ok

    def _temperature_settable(self):
        """決定ボタン span が set_g (グレー) なら温度操作不可。"""
        try:
            cls = self.driver.find_element(
                By.CSS_SELECTOR, "#humidity-box .set-box span"
            ).get_attribute("class") or ""
            return "set_g" not in cls
        except Exception as error:
            if self._is_session_dead(error):
                raise
            return False

    def _read_current_temp(self):
        try:
            txt = self.driver.find_element(
                By.CSS_SELECTOR, "#humidity-box .value span"
            ).text.strip()
            return float(txt)
        except Exception as error:
            if self._is_session_dead(error):
                raise
            return None

    def set_temperature(self, target_temp, *, deadline=None):
        operation_deadline = self._operation_deadline(30, deadline)
        self.navigate_to_smart_airs(deadline=operation_deadline)
        print(f"Setting temperature to: {target_temp}")

        def read_normalized_current_temp():
            current_temp = self._read_current_temp()
            return round(current_temp) if current_temp is not None else None

        power = self._read_power_token()
        if power != "ON":
            print(f"Power is {power or 'UNKNOWN'}; temperature controls disabled. Skipping.")
            return False

        if not self._temperature_settable():
            print("Temperature controls are disabled in current mode (Fan/Dry). Skipping.")
            return False

        current_temp = read_normalized_current_temp()
        if current_temp is None:
            print("Current temperature is not a number. Skipping set_temperature.")
            return False

        target_temp = round(target_temp)
        print(f"Current temperature: {current_temp}")
        if current_temp == target_temp:
            print("Temperature is already at target.")
            return True

        diff = target_temp - current_temp
        if diff > 0:
            anchor_css = "#humidity-box .up a"
            action = "Increasing"
        else:
            anchor_css = "#humidity-box .down a"
            action = "Decreasing"

        for _ in range(abs(diff)):
            try:
                self._click_before_deadline(
                    self.driver.find_element(By.CSS_SELECTOR, anchor_css), operation_deadline
                )
                if not self._sleep_until(0.3, operation_deadline):
                    break
            except TimeoutException:
                raise
            except Exception as error:
                if self._is_session_dead(error):
                    raise
                print(f"Temperature adjust anchor {anchor_css} failed: {error}")
                break

        print(f"{action} temperature by {abs(diff)} degrees.")

        try:
            self._click_before_deadline(
                self.driver.find_element(By.CSS_SELECTOR, "#humidity-box .set-box a"), operation_deadline
            )
        except TimeoutException:
            raise
        except Exception as e:
            if self._is_session_dead(e):
                raise
            print(f"Confirm click failed: {e}")
            return False

        if self._wait_until(
            lambda: read_normalized_current_temp() == target_temp,
            timeout=10,
            deadline=operation_deadline,
        ):
            print("Temperature setting confirmed.")
            return True
        else:
            print("Temperature change not confirmed within timeout.")
            return False

    def toggle_power(self, state, *, deadline=None, mode_ready_deadline=None):
        self.navigate_to_smart_airs(deadline=deadline)
        self._clamp_deadline_timeouts(deadline)
        print(f"Setting power to: {state}")

        self._off_to_on_transition = False
        self._power_confirmed = False
        self._mode_ready = state != "ON"

        current = self._read_power_token()
        if current is None:
            # AJAX 未反映で状態不明のままクリックすると逆操作になる危険があるため拒否。
            print("Power state is unknown (AJAX not populated); refusing to click.")
            return PowerTransition.UNCONFIRMED

        if current == state:
            self._power_confirmed = True
            self._mode_ready = True
            print(f"Power is already {state}.")
            return PowerTransition.ALREADY_ON

        self._off_to_on_transition = current == "OFF" and state == "ON"

        try:
            self._click_before_deadline(
                self.driver.find_element(By.CSS_SELECTOR, "div.untenButton a"), deadline
            )
            print("Toggling power button...")
        except TimeoutException:
            raise
        except Exception as e:
            if self._is_session_dead(e):
                raise
            print(f"Failed to click power button: {e}")
            return PowerTransition.UNCONFIRMED

        if not self._wait_until(
            lambda: self._read_power_token() == state,
            timeout=15,
            deadline=deadline,
        ):
            print(f"Power did not change to {state} within timeout.")
            return PowerTransition.UNCONFIRMED
        self._power_confirmed = True
        print(f"Power confirmed: {state}.")

        # ON 直後は mode-box にいずれかの btn_mode_on が立つまで AJAX 反映を待つ。
        # OFF にしたときは mode が全消去されるので待たない。
        if state == "ON":
            self._mode_ready = self._wait_until(
                lambda: any(
                    "btn_mode_on" in (s.get_attribute("class") or "")
                    for s in self.driver.find_elements(By.CSS_SELECTOR, "#mode-box span")
                ),
                timeout=10,
                deadline=mode_ready_deadline if mode_ready_deadline is not None else deadline,
            )
            if not self._mode_ready:
                print("Mode controls did not become ready after power ON.")
                return PowerTransition.UNCONFIRMED
        return PowerTransition.OFF_TO_ON if self._off_to_on_transition else PowerTransition.ALREADY_ON

    def get_current_status(self, target_floor=None, *, deadline=None):
        """AC の現在状態を返す。HEMS UI は自動更新されないため毎回ページを再ロードする。"""
        # 外部 (リモコン等) で変えた状態を取りこぼさないよう強制リロード
        self.navigate_to_smart_airs(force_reload=True, deadline=deadline)
        print(f"Getting status (Target Floor: {target_floor})...")
        status = {}
        try:
            if target_floor:
                self.select_floor(target_floor, deadline=deadline)
            self._wait_smart_airs_ready(deadline=deadline)

            # Floor
            status['floor'] = self._current_floor()

            # Mode
            status['mode'] = None
            for mode in ['暖房', '冷房', '除湿', '自動', '送風']:
                try:
                    xpath = f"//div[@id='mode-box']//span[normalize-space(text())='{mode}']"
                    elem = self.driver.find_element(By.XPATH, xpath)
                    if "btn_mode_on" in (elem.get_attribute("class") or ""):
                        status['mode'] = mode
                        break
                except NoSuchElementException:
                    continue

            # Temperature (送風 / 除湿 では None)
            status['temperature'] = self._read_current_temp()

            # Power
            power = self._read_power_token()
            status['power'] = power if power else "UNKNOWN"

            return status
        except TimeoutException:
            raise
        except Exception as e:
            if self._is_session_dead(e):
                raise
            print(f"Failed to get status: {e}")
            return None

    @staticmethod
    def _has_btn_on(cls):
        cls = cls or ""
        return ("btn_on_left" in cls) or ("btn_on_right" in cls)

    def _visible_shutter_li(self):
        """sec03 配下で display:none でない <li> を返す (予備 li を除外)。"""
        return self.driver.find_element(
            By.XPATH,
            "//div[contains(@class,'sec03')]"
            "//li[not(contains(translate(@style,' ',''),'display:none'))]"
        )

    def get_security_status(self, *, deadline=None):
        self.navigate_to_security(deadline=deadline)
        print("Getting security status...")
        capabilities = self.security_capabilities
        status = {"capabilities": capabilities}

        # Lock
        if capabilities["lock"] == "disabled":
            status["lock"] = "DISABLED"
        elif capabilities["lock"] == "not_installed":
            status["lock"] = "NOT_INSTALLED"
        else:
            try:
                lock_section = self.driver.find_element(By.ID, "lalock")
                lock_cls = lock_section.find_element(
                    By.XPATH, ".//span[normalize-space(text())='施錠']"
                ).get_attribute("class") or ""
                unlock_cls = lock_section.find_element(
                    By.XPATH, ".//span[normalize-space(text())='解錠']"
                ).get_attribute("class") or ""

                lock_on = self._has_btn_on(lock_cls)
                unlock_on = self._has_btn_on(unlock_cls)
                if lock_on and not unlock_on:
                    status['lock'] = "LOCKED"
                elif unlock_on and not lock_on:
                    status['lock'] = "UNLOCKED"
                else:
                    print(f"DEBUG: Lock Unknown. lock='{lock_cls}' unlock='{unlock_cls}'")
                    status['lock'] = "UNKNOWN"
            except Exception as e:
                print(f"DEBUG: Lock detection failed: {e}")
                status['lock'] = "UNKNOWN"

        # Shutter
        if capabilities["shutter"] == "not_installed":
            status["shutter"] = "NOT_INSTALLED"
        else:
            try:
                visible_li = self._visible_shutter_li()
                open_cls = visible_li.find_element(
                    By.XPATH, ".//span[normalize-space(text())='開']"
                ).get_attribute("class") or ""
                close_cls = visible_li.find_element(
                    By.XPATH, ".//span[normalize-space(text())='閉']"
                ).get_attribute("class") or ""

                open_on = self._has_btn_on(open_cls)
                close_on = self._has_btn_on(close_cls)
                if open_on and not close_on:
                    status['shutter'] = "OPEN"
                elif close_on and not open_on:
                    status['shutter'] = "CLOSED"
                else:
                    print(f"DEBUG: Shutter Unknown. open='{open_cls}' close='{close_cls}'")
                    status['shutter'] = "UNKNOWN"
            except Exception as e:
                print(f"DEBUG: Shutter detection failed: {e}")
                status['shutter'] = "UNKNOWN"

        return status

    def control_lock(self, action, *, deadline=None):
        self.navigate_to_security(deadline=deadline)
        print(f"Controlling Lock: {action}")
        if action == "lock":
            target_text, other_text, target_state = "施錠", "解錠", "LOCKED"
        elif action == "unlock":
            target_text, other_text, target_state = "解錠", "施錠", "UNLOCKED"
        else:
            print("Invalid lock action.")
            return False

        try:
            lock_section = self.driver.find_element(By.ID, "lalock")
            target_span = lock_section.find_element(
                By.XPATH, f".//span[normalize-space(text())='{target_text}']"
            )
            other_span = lock_section.find_element(
                By.XPATH, f".//span[normalize-space(text())='{other_text}']"
            )
            target_on = self._has_btn_on(target_span.get_attribute("class") or "")
            other_on = self._has_btn_on(other_span.get_attribute("class") or "")
            if target_on and not other_on:
                print(f"Lock is already {target_state}.")
                return True

            target_span.find_element(By.XPATH, "./..").click()

            def _confirmed():
                section = self.driver.find_element(By.ID, "lalock")
                t = self._has_btn_on(
                    section.find_element(
                        By.XPATH, f".//span[normalize-space(text())='{target_text}']"
                    ).get_attribute("class") or ""
                )
                o = self._has_btn_on(
                    section.find_element(
                        By.XPATH, f".//span[normalize-space(text())='{other_text}']"
                    ).get_attribute("class") or ""
                )
                return t and not o

            ok = self._wait_until(_confirmed, timeout=15, deadline=deadline)
            if ok:
                print(f"Lock command confirmed: {target_state}.")
                return True
            print(f"Lock command failed: {target_state} not confirmed within timeout (fail-closed).")
            return False
        except Exception as e:
            print(f"Failed to control lock: {e}")
            return False

    def control_shutter(self, action, *, deadline=None):
        self.navigate_to_security(deadline=deadline)
        print(f"Controlling Shutter: {action}")
        if action == "open":
            target_text, other_text, target_state = "開", "閉", "OPEN"
        elif action == "close":
            target_text, other_text, target_state = "閉", "開", "CLOSED"
        else:
            print("Invalid shutter action.")
            return False

        try:
            visible_li = self._visible_shutter_li()
            target_span = visible_li.find_element(
                By.XPATH, f".//span[normalize-space(text())='{target_text}']"
            )
            other_span = visible_li.find_element(
                By.XPATH, f".//span[normalize-space(text())='{other_text}']"
            )
            target_on = self._has_btn_on(target_span.get_attribute("class") or "")
            other_on = self._has_btn_on(other_span.get_attribute("class") or "")
            if target_on and not other_on:
                print(f"Shutter is already {target_state}.")
                return True

            target_span.find_element(By.XPATH, "./..").click()

            def _confirmed():
                li = self._visible_shutter_li()
                t = self._has_btn_on(
                    li.find_element(
                        By.XPATH, f".//span[normalize-space(text())='{target_text}']"
                    ).get_attribute("class") or ""
                )
                o = self._has_btn_on(
                    li.find_element(
                        By.XPATH, f".//span[normalize-space(text())='{other_text}']"
                    ).get_attribute("class") or ""
                )
                return t and not o

            ok = self._wait_until(_confirmed, timeout=20, deadline=deadline)
            if ok:
                print(f"Shutter command confirmed: {target_state}.")
                return True
            print(f"Shutter command failed: {target_state} not confirmed within timeout (fail-closed).")
            return False
        except Exception as e:
            print(f"Failed to control shutter: {e}")
            return False

    def close(self):
        driver = getattr(self, "driver", None)
        if driver is not None:
            try:
                driver.quit()
            finally:
                self.driver = None

def main(argv=None):
    import json

    parser = argparse.ArgumentParser(description="HEMS read-only diagnostics")
    parser.add_argument(
        "--url",
        default=os.environ.get("HEMS_URL"),
        help="HEMS device URL (defaults to HEMS_URL)",
    )
    parser.add_argument("--user", help="Login ID", required=True)
    parser.add_argument("--password", help="Login Password", required=True)
    parser.add_argument("--floor", type=int, help="Target floor for --status")
    parser.add_argument("--mode", choices=["暖房", "冷房", "除湿", "自動", "送風"])
    parser.add_argument("--temp", type=float)
    parser.add_argument("--power", choices=["ON", "OFF"])
    parser.add_argument("--status", action="store_true", help="Get current AC status")
    parser.add_argument("--lock", choices=["lock", "unlock"])
    parser.add_argument("--shutter", choices=["open", "close"])
    parser.add_argument("--security-status", action="store_true", help="Get security status")
    args = parser.parse_args(argv)

    has_mutation = any(
        value is not None
        for value in (args.mode, args.temp, args.power, args.lock, args.shutter)
    ) or (args.floor is not None and not args.status)
    if has_mutation or not (args.status or args.security_status):
        print(
            "CLI mutation is disabled; use the HEMS REST API for control. "
            "CLI supports --status and --security-status diagnostics only.",
            file=sys.stderr,
        )
        return 2
    if not args.url or not args.url.strip():
        print("HEMS_URL environment variable or --url must be set.", file=sys.stderr)
        return 2

    controller = HEMSController(
        args.url.strip(), args.user, args.password, headless=True
    )
    try:
        controller.login()
        if args.status:
            if args.floor is not None:
                controller.navigate_to_smart_airs()
                floors = controller.detected_floors()
                if args.floor not in floors:
                    detected = ", ".join(str(floor) for floor in floors) or "none"
                    print(
                        f"Invalid floor {args.floor}; detected floors: {detected}",
                        file=sys.stderr,
                    )
                    return 2
            print(
                json.dumps(
                    controller.get_current_status(target_floor=args.floor),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        if args.security_status:
            print(
                json.dumps(
                    controller.get_security_status(), ensure_ascii=False, indent=2
                )
            )
        return 0
    except Exception as exc:
        print(f"Read-only diagnostic failed: {exc}", file=sys.stderr)
        return 1
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
