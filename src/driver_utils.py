import time
import random
import datetime

LMS_URL = "https://learn.hoseo.ac.kr/login/index.php"

log_callback = None
video_progress_callback = None
VERBOSE = False


def set_verbose(enabled):
    global VERBOSE
    VERBOSE = enabled


def set_log_callback(callback):
    global log_callback
    log_callback = callback


def set_video_progress_callback(callback):
    global video_progress_callback
    video_progress_callback = callback


def info(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    print(formatted_msg)
    if log_callback:
        log_callback(formatted_msg)


def error(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] [ERROR] {msg}"
    print(formatted_msg)
    if log_callback:
        log_callback(formatted_msg)


def debug(msg):
    if not VERBOSE:
        return
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] [DEBUG] {msg}"
    print(formatted_msg)
    if log_callback:
        log_callback(formatted_msg)


def random_sleep(min_sec=1.0, max_sec=2.0):
    time.sleep(random.uniform(min_sec, max_sec))


def human_like_delay():
    time.sleep(random.uniform(0.2, 0.6) + random.uniform(0.1, 0.2))


def typing_delay():
    time.sleep(random.uniform(0.02, 0.07))


def click_delay():
    time.sleep(random.uniform(0.3, 0.8))


def random_mouse_movement(driver):
    try:
        script = """
        var event = new MouseEvent('mousemove', {
            'view': window, 'bubbles': true, 'cancelable': true,
            'clientX': arguments[0], 'clientY': arguments[1]
        });
        document.dispatchEvent(event);
        """
        driver.execute_script(script, random.randint(100, 800), random.randint(100, 600))
        time.sleep(random.uniform(0.1, 0.3))
    except:
        pass


def random_scroll(driver):
    try:
        driver.execute_script(f"window.scrollBy(0, {random.randint(100, 500)});")
        time.sleep(random.uniform(0.5, 1.5))
    except:
        pass


def simulate_human_behavior(driver):
    if random.random() > 0.7:
        random_mouse_movement(driver)
    if random.random() > 0.8:
        random_scroll(driver)
