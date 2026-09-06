import os
import sys
import platform
import subprocess

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth

from .driver_utils import (
    LMS_URL, info, error,
)
from .browser_locator import find_chrome
from .selectors import BASE, url as lms_url

_chromedriver_path = None

_SAME_SITE_VALUES = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
}


def _cookie_browser_attributes(cookie):
    """Return browser security attributes retained by requests' cookie jar."""

    http_only = False
    same_site = None
    for key, value in getattr(cookie, "_rest", {}).items():
        normalized = str(key).casefold()
        if normalized == "httponly":
            http_only = value is not False
        elif normalized == "samesite" and isinstance(value, str):
            same_site = _SAME_SITE_VALUES.get(value.strip().casefold())
    return http_only, same_site


def _platform_identity():
    system = platform.system()
    if system == "Darwin":
        return "Macintosh; Intel Mac OS X 10_15_7", "MacIntel"
    if system == "Linux":
        return "X11; Linux x86_64", "Linux x86_64"
    return "Windows NT 10.0; Win64; x64", "Win32"


def _get_chromedriver_path():
    global _chromedriver_path
    if _chromedriver_path is None:
        _chromedriver_path = ChromeDriverManager().install()
    return _chromedriver_path


def _make_service():
    path = _get_chromedriver_path()
    if sys.platform == "win32":
        popen_kw = {"creation_flags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        popen_kw = {"start_new_session": True}
    try:
        return Service(path, popen_kw=popen_kw)
    except TypeError:
        return Service(path)


def init_driver(headless=True):
    options = Options()

    if headless:
        options.add_argument("--headless=new")
        info("Headless 모드 활성화")
    else:
        info("일반 모드 (화면 표시)")

    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-infobars")
    options.add_argument("--mute-audio")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--disable-accelerated-2d-canvas")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")

    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.media_stream_mic": 2,
        "profile.default_content_setting_values.media_stream_camera": 2,
        "profile.default_content_setting_values.geolocation": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.default_content_settings.popups": 0,
        "download.prompt_for_download": False,
        "safebrowsing.enabled": True
    })

    if os.environ.get("HOSEO_CHROME_NO_SANDBOX") == "1":
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-setuid-sandbox")

    _, stealth_platform = _platform_identity()

    chrome_path = find_chrome()
    if chrome_path:
        options.binary_location = chrome_path

    service = _make_service()
    driver = webdriver.Chrome(service=service, options=options)
    try:
        driver._hoseo_headless = bool(headless)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(60)

        stealth(driver,
                languages=["ko-KR", "ko", "en-US", "en"],
                vendor="Google Inc.",
                platform=stealth_platform,
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
                run_on_insecure_origins=True)

        wait = WebDriverWait(driver, 30)
        return driver, wait
    except BaseException:
        # Once webdriver.Chrome returns, this function owns the process even
        # if a later timeout/stealth setup step fails before the caller can
        # receive the driver and close it.
        try:
            driver.quit()
        except Exception:
            pass
        raise


def login(driver, wait, user_id, password):
    try:
        driver.get(LMS_URL)
        user_id_input = wait.until(EC.presence_of_element_located((By.ID, "input-username")))
        user_password_input = wait.until(EC.presence_of_element_located((By.ID, "input-password")))
        user_id_input.send_keys(user_id)
        user_password_input.send_keys(password)
        user_login_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".btn.btn-login")))
        user_login_button.click()

        try:
            WebDriverWait(driver, 15).until(
                lambda d: ("login" not in d.current_url
                           and bool(d.find_elements(By.CSS_SELECTOR, "a[href*='/login/logout.php']")))
            )
            return True
        except TimeoutException:
            error("로그인 실패: 자격증명을 확인하세요.")
            return False

    except Exception as e:
        error(f"로그인 중 오류 발생: {e}")
        return False


def adopt_authenticated_session(driver, client, verify: bool = True) -> bool:
    """Copy an authenticated requests session into Chrome without retyping credentials."""
    cookies = list(client.cookies)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.delete_all_cookies()
        installed = 0
        for cookie in cookies:
            http_only, same_site = _cookie_browser_attributes(cookie)
            payload = {
                "name": cookie.name,
                "value": cookie.value,
                "url": BASE,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "httpOnly": http_only,
            }
            if cookie.expires:
                payload["expires"] = float(cookie.expires)
            if same_site:
                payload["sameSite"] = same_site
            result = driver.execute_cdp_cmd("Network.setCookie", payload)
            installed += int(result.get("success", False))
        if installed and not verify:
            return True
        if installed:
            driver.get(lms_url("course_list"))
            WebDriverWait(driver, 10).until(
                lambda d: ("/login/" not in d.current_url
                           and bool(d.find_elements(
                               By.CSS_SELECTOR, "a[href*='/login/logout.php']")))
            )
            return True
    except Exception:
        # Older Chrome/driver pairs may not expose Network.setCookie.
        pass

    try:
        driver.get(f"{BASE}/login/index.php")
        driver.delete_all_cookies()
        for cookie in cookies:
            http_only, same_site = _cookie_browser_attributes(cookie)
            payload = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "httpOnly": http_only,
            }
            if cookie.expires:
                payload["expiry"] = int(cookie.expires)
            if same_site:
                payload["sameSite"] = same_site
            try:
                driver.add_cookie(payload)
            except Exception:
                continue
        driver.get(lms_url("course_list"))
        WebDriverWait(driver, 10).until(
            lambda d: ("/login/" not in d.current_url
                       and bool(d.find_elements(By.CSS_SELECTOR, "a[href*='/login/logout.php']")))
        )
        return True
    except Exception:
        return False
