import time
import random
import datetime

LMS_URL = "https://learn.hoseo.ac.kr/login/index.php"

log_callback = None
video_progress_callback = None
status_callback = None
VERBOSE = False
PRINT_STDOUT = True
_error_buffer: list[str] = []


def set_stdout(enabled):
    global PRINT_STDOUT
    PRINT_STDOUT = enabled


def set_log_callback(callback):
    global log_callback
    log_callback = callback


def set_video_progress_callback(callback):
    global video_progress_callback
    video_progress_callback = callback


def set_status_callback(callback):
    global status_callback
    status_callback = callback


def notify_status(msg: str):
    if status_callback:
        status_callback(msg)


def info(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    if PRINT_STDOUT:
        print(formatted_msg)
    if log_callback:
        log_callback(formatted_msg)


def error(msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] [ERROR] {msg}"
    _error_buffer.append(formatted_msg)
    if PRINT_STDOUT:
        print(formatted_msg)
    if log_callback:
        log_callback(formatted_msg)


def flush_errors() -> list[str]:
    errors = _error_buffer.copy()
    _error_buffer.clear()
    return errors


def debug(msg):
    if not VERBOSE:
        return
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] [DEBUG] {msg}"
    if PRINT_STDOUT:
        print(formatted_msg)
    if log_callback:
        log_callback(formatted_msg)


def human_like_delay():
    time.sleep(random.uniform(0.2, 0.6) + random.uniform(0.1, 0.2))


def typing_delay():
    time.sleep(random.uniform(0.02, 0.07))


def click_delay():
    time.sleep(random.uniform(0.3, 0.8))
