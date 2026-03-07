from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth

from driver_utils import (
    LMS_URL, info, error,
    human_like_delay, typing_delay, click_delay, random_sleep
)


def init_driver(headless=True, block_eum=False):
    options = Options()

    if headless:
        options.add_argument("--headless=new")
        info("Headless 모드 활성화")
    else:
        info("일반 모드 (화면 표시)")

    options.add_argument("--start-maximized")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
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
    options.add_argument("--disable-web-security")
    options.add_argument("--allow-running-insecure-content")
    options.add_argument("--disable-features=IsolateOrigins,site-per-process")
    options.add_argument("--disable-site-isolation-trials")
    options.add_argument("--disable-setuid-sandbox")
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
        "safebrowsing.enabled": False
    })

    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    options.add_argument(f"user-agent={user_agent}")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)

    stealth(driver,
            languages=["ko-KR", "ko", "en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
            run_on_insecure_origins=True)

    if block_eum:
        driver.execute_script("""
            const realBeacon = navigator.sendBeacon;
            navigator.sendBeacon = function(url, data) {
                if (url.includes('eum') || url.includes('analytics')) {
                    return true;
                }
                return realBeacon.call(navigator, url, data);
            };
        """)

    wait = WebDriverWait(driver, 30)
    return driver, wait


def login(driver, wait, user_id, password):
    try:
        driver.get(LMS_URL)
        human_like_delay()

        user_id_input = wait.until(EC.presence_of_element_located((By.ID, "input-username")))
        human_like_delay()

        for char in user_id:
            user_id_input.send_keys(char)
            typing_delay()

        human_like_delay()
        user_password_input = wait.until(EC.presence_of_element_located((By.ID, "input-password")))
        human_like_delay()

        for char in password:
            user_password_input.send_keys(char)
            typing_delay()

        click_delay()
        user_login_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".btn.btn-login")))
        user_login_button.click()

        random_sleep(1.5, 2.5)
        if "login" in driver.current_url:
            try:
                driver.find_element(By.CLASS_NAME, "userpicture")
                return True
            except:
                error("로그인 실패: 대시보드로 이동하지 못했습니다.")
                return False

        return True
    except Exception as e:
        error(f"로그인 중 오류 발생 : {e}")
        return False
