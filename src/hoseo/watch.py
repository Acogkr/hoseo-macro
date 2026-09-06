import re
import time
import random
import datetime
from urllib.parse import urlparse, parse_qs

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    InvalidSessionIdException,
    NoSuchWindowException,
    WebDriverException,
)
from . import driver_utils
from .driver_utils import (
    info, error, debug, set_log_callback,
    human_like_delay, click_delay
)
from .selectors import BASE, absolute_module_url, attendance_header_indexes
from .session import init_driver, login


class AttendancePageError(RuntimeError):
    """Raised when the attendance page cannot be interpreted safely."""


class BrowserAuthenticationExpiredError(RuntimeError):
    """Raised when an LMS browser navigation has returned to the login page."""


_FATAL_WEBDRIVER_MARKERS = (
    "invalid session id",
    "disconnected",
    "not connected to devtools",
    "no such window",
    "target window already closed",
    "chrome not reachable",
    "devtoolsactiveport",
)


def _is_fatal_webdriver_error(exc):
    if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)):
        return True
    return (isinstance(exc, WebDriverException)
            and any(marker in str(exc).lower()
                    for marker in _FATAL_WEBDRIVER_MARKERS))


def _raise_if_fatal_webdriver(exc):
    if (isinstance(exc, BrowserAuthenticationExpiredError)
            or _is_fatal_webdriver_error(exc)):
        raise exc


def _raise_if_authentication_expired(driver):
    try:
        path = urlparse(str(getattr(driver, "current_url", "") or "")).path
    except Exception as exc:
        _raise_if_fatal_webdriver(exc)
        return
    if path == "/login" or path.startswith("/login/"):
        raise BrowserAuthenticationExpiredError(
            "LMS 로그인 세션이 만료되었습니다.")


def _extract_course_id(url):
    try:
        params = parse_qs(urlparse(url).query)
        if 'id' in params:
            return params['id'][0]
    except Exception:
        pass
    return None


def get_active_weeks(driver, course_id):
    try:
        driver.get(f"{BASE}/mod/vod/index.php?id={course_id}")
        rows = driver.find_elements(By.CSS_SELECTOR, "table.generaltable tbody tr")
        today = datetime.date.today()
        active_weeks = []
        week_pattern = re.compile(r'(\d+)주차\s*\[(\d+)월(\d+)일\s*-\s*(\d+)월(\d+)일\]')
        for row in rows:
            try:
                first_cell = row.find_element(By.CSS_SELECTOR, "td.cell.c0")
                cell_text = first_cell.text.strip()
                if not cell_text:
                    continue
                match = week_pattern.search(cell_text)
                if match:
                    week_num = int(match.group(1))
                    start_month = int(match.group(2))
                    start_day = int(match.group(3))
                    end_month = int(match.group(4))
                    end_day = int(match.group(5))
                    try:
                        start_year = (today.year - 1
                                      if start_month > end_month and today.month <= end_month
                                      else today.year)
                        end_year = start_year + 1 if end_month < start_month else start_year
                        start_date = datetime.date(start_year, start_month, start_day)
                        end_date = datetime.date(end_year, end_month, end_day)
                        if start_date <= today <= end_date + datetime.timedelta(days=7):
                            active_weeks.append(week_num)
                    except ValueError:
                        continue
            except NoSuchElementException:
                continue
        if active_weeks:
            info(f"현재 수강 가능한 주차: {active_weeks}")
        else:
            debug("현재 수강 가능한 주차가 없습니다.")
        return active_weeks
    except InvalidSessionIdException:
        raise
    except Exception as e:
        _raise_if_fatal_webdriver(e)
        error(f"수강 가능 주차 확인 중 오류: {e}")
        return None


def _header_indexes(table):
    header_cells = table.find_elements(By.CSS_SELECTOR, "thead tr th, thead tr td")
    labels = [cell.text.strip() for cell in header_cells]
    week_idx, name_idx, status_idx = attendance_header_indexes(labels)
    # The live table sometimes leaves the leading week header blank.
    if week_idx is None and labels and not labels[0]:
        week_idx = 0
    return week_idx, name_idx, status_idx, len(labels)


def _attendance_row_indexes(cells, header_count, week_idx, name_idx,
                            status_idx, current_week):
    if header_count:
        week_text = (cells[week_idx].text.strip()
                     if week_idx < len(cells) else "")
        if week_text.isdigit():
            current_week = week_text
            ni, si = name_idx, status_idx
        elif len(cells) < header_count and current_week is not None:
            ni = name_idx - int(name_idx > week_idx)
            si = status_idx - int(status_idx > week_idx)
        else:
            return None, current_week
    else:
        starts_week = cells[0].text.strip().isdigit()
        if starts_week:
            current_week = cells[0].text.strip()
        elif current_week is None:
            return None, current_week
        ni, si = ((1, 5) if starts_week else (0, 4))
    if min(ni, si) < 0 or max(ni, si) >= len(cells):
        return None, current_week
    return (current_week, ni, si), current_week


def _candidate_vod_link(cells):
    for cell in cells:
        try:
            link = cell.find_element(By.CSS_SELECTOR, "a")
        except NoSuchElementException:
            continue
        url = absolute_module_url(link.get_attribute("href") or "", "vod")
        if url:
            return link, url
    return None, ""


def _attendance_records(driver):
    try:
        table = driver.find_element(By.CSS_SELECTOR, "table.table-coursemos")
    except NoSuchElementException as exc:
        _raise_if_authentication_expired(driver)
        raise AttendancePageError("온라인 출석부 표를 찾을 수 없습니다.") from exc
    week_idx, name_idx, status_idx, header_count = _header_indexes(table)
    if header_count and any(index is None for index in (week_idx, name_idx, status_idx)):
        raise AttendancePageError(
            "온라인 출석부의 주차, 강의명 또는 출결상태 열을 찾을 수 없습니다.")
    current_week = None
    vod_candidates = 0
    for row in table.find_elements(By.CSS_SELECTOR, "tbody tr"):
        cells = row.find_elements(By.XPATH, "./td")
        if not cells:
            continue
        candidate, _candidate_url = _candidate_vod_link(cells)
        indexes, current_week = _attendance_row_indexes(
            cells, header_count, week_idx, name_idx, status_idx, current_week)
        if indexes is None:
            if candidate:
                raise AttendancePageError("온라인 출석부의 동영상 행을 해석할 수 없습니다.")
            continue
        if not candidate:
            continue
        vod_candidates += 1
        week, ni, si = indexes
        try:
            link = cells[ni].find_element(By.CSS_SELECTOR, "a")
        except NoSuchElementException as exc:
            raise AttendancePageError("온라인 출석부의 동영상 행을 해석할 수 없습니다.") from exc
        url = absolute_module_url(link.get_attribute("href") or "", "vod")
        title = link.text.strip()
        if not url or not title:
            raise AttendancePageError("온라인 출석부의 동영상 행을 해석할 수 없습니다.")
        yield {
            "week": week,
            "element": link,
            "title": title,
            "url": url,
            "status": cells[si].text.strip(),
        }
    if vod_candidates == 0:
        raise AttendancePageError(
            "온라인 출석부에서 동영상 행을 찾을 수 없습니다.")


def get_uncompleted_lectures_by_week(driver, week_number):
    uncompleted_lectures = []
    try:
        target_week = str(week_number)
        for record in _attendance_records(driver):
            if record["week"] != target_week or record["status"] == "O":
                continue
            uncompleted_lectures.append({
                key: record[key] for key in ("element", "title", "url")
            })
    except InvalidSessionIdException:
        raise
    except AttendancePageError:
        raise
    except Exception as e:
        _raise_if_fatal_webdriver(e)
        error(f"강의 정보 파싱 중 오류 발생: {e}")
        raise AttendancePageError("온라인 출석부를 해석할 수 없습니다.") from e
    return uncompleted_lectures


def _interlude(stop_event, min_sec: float, max_sec: float):
    end = time.time() + random.uniform(min_sec, max_sec)
    while time.time() < end:
        if stop_event and stop_event.is_set():
            driver_utils.notify_status("")
            return
        remaining = max(1, int(end - time.time()) + 1)
        driver_utils.notify_status(f"다음 강의까지 {remaining}초")
        time.sleep(0.5)
    driver_utils.notify_status("")


def watch_lecture(driver, wait, week_number, lecture_info, stop_event=None):
    if stop_event and stop_event.is_set():
        return None
    lecture_title_element = lecture_info['element']
    lecture_title = lecture_info['title']
    main_window_handle = None
    player_window_handle = None
    windows_before = set()
    try:
        driver_utils.notify_status("강의 페이지 이동 중...")
        human_like_delay()
        main_window_handle = driver.current_window_handle
        windows_before = set(driver.window_handles)
        try:
            lecture_title_element.click()
        except Exception as e:
            _raise_if_fatal_webdriver(e)
            debug(f"일반 클릭 실패 ({e}), JavaScript로 강제 클릭을 시도합니다.")
            driver.execute_script("arguments[0].scrollIntoView(true);", lecture_title_element)
            driver.execute_script("arguments[0].click();", lecture_title_element)
        try:
            player_window_handle = wait.until(
                lambda d: next(iter(set(d.window_handles) - windows_before), None))
            driver.switch_to.window(player_window_handle)
        except TimeoutException:
            error("동영상 플레이어 창이 열리지 않았습니다.")
            return None
        except InvalidSessionIdException:
            raise
        human_like_delay()
        try:
            WebDriverWait(driver, 2).until(EC.alert_is_present())
            alert = driver.switch_to.alert
            alert_text = alert.text
            alert.accept()
            if "열람이 불가능합니다" in alert_text:
                error(f"강의 열람 불가: {lecture_title}")
                driver.switch_to.window(main_window_handle)
                return "unavailable"
        except TimeoutException:
            pass
        except InvalidSessionIdException:
            raise
        try:
            video_element = wait.until(EC.presence_of_element_located((By.TAG_NAME, "video")))
            try:
                play_button = driver.find_element(By.CLASS_NAME, "vjs-big-play-button")
                if play_button.is_displayed():
                    click_delay()
                    try:
                        play_button.click()
                    except Exception as e:
                        debug(f"재생 버튼 일반 클릭 실패 ({e}), JS로 시도합니다.")
                        driver.execute_script("arguments[0].click();", play_button)
            except NoSuchElementException:
                pass
            driver_utils.notify_status("")
            driver.execute_script("arguments[0].play();", video_element)
            info(f"강의 재생을 시작합니다: {lecture_title}")
            start_time = time.time()
            last_log_time = start_time
            last_progress_time = start_time
            last_current_time = -1.0
            poll_failures = 0
            progress_cb = driver_utils.video_progress_callback
            while True:
                if stop_event and stop_event.is_set():
                    info("정지 요청을 확인했습니다. 동영상 창을 닫습니다.")
                    break
                try:
                    status = driver.execute_script("""
                        var v = arguments[0];
                        return {
                            ended: v.ended || false,
                            currentTime: v.currentTime || 0,
                            duration: (v.duration && isFinite(v.duration)) ? v.duration : 0
                        };
                    """, video_element)
                    is_ended = status.get('ended', False) if status else False
                    current_time = status.get('currentTime', 0) if status else 0
                    duration = status.get('duration', 0) if status else 0
                    poll_failures = 0
                    if current_time > last_current_time + 0.2:
                        last_progress_time = time.time()
                        last_current_time = current_time
                    if progress_cb is not None and duration > 0:
                        progress_cb(int(current_time), int(duration), lecture_title)
                except InvalidSessionIdException:
                    raise
                except Exception as e:
                    _raise_if_fatal_webdriver(e)
                    error(f"동영상 상태 확인 중 오류: {e}")
                    poll_failures += 1
                    if poll_failures >= 3:
                        return None
                    time.sleep(3)
                    continue
                if is_ended or (duration > 0 and current_time >= duration):
                    if progress_cb is not None and duration > 0:
                        progress_cb(int(duration), int(duration), lecture_title)
                    info(f"동영상 재생이 끝났습니다. 출석 반영을 확인합니다: {lecture_title}")
                    return lecture_title
                if time.time() - last_log_time > 60:
                    if duration > 0:
                        debug(f"[{lecture_title}] 현재 진행률: {int(current_time)}/{int(duration)} 초")
                    last_log_time = time.time()
                if time.time() - last_progress_time > 120:
                    error("동영상 재생이 2분 이상 진행되지 않아 중단합니다.")
                    return None
                max_runtime = max(3600, duration + 600) if duration > 0 else 3600
                if time.time() - start_time > max_runtime:
                    error("시간 초과: 동영상 재생 시간이 너무 길어 중단합니다.")
                    return None
                time.sleep(3)
        except InvalidSessionIdException:
            raise
        except Exception as e:
            _raise_if_fatal_webdriver(e)
            error(f"강의 수강 중 오류가 발생했습니다. ('{lecture_title}'): {e}")
            return None
    except InvalidSessionIdException:
        raise
    except Exception as e:
        _raise_if_fatal_webdriver(e)
        error(f"강의 시작 중 오류가 발생했습니다. ('{lecture_title}'): {e}")
        return None
    finally:
        try:
            current_windows = driver.window_handles if driver else []
            if player_window_handle in current_windows:
                driver.switch_to.window(player_window_handle)
                driver.close()
            if main_window_handle and main_window_handle in driver.window_handles:
                driver.switch_to.window(main_window_handle)
            elif len(driver.window_handles) > 0:
                driver.switch_to.window(driver.window_handles[0])
        except InvalidSessionIdException:
            error("세션이 끊어져 창을 닫을 수 없습니다.")
            raise
        except Exception as e:
            _raise_if_fatal_webdriver(e)
            error(f"창을 닫는 중 오류가 발생했습니다: {e}")


def _wait_for_attendance_page(driver, timeout=10):
    _raise_if_authentication_expired(driver)
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.table-coursemos"))
        )
    except TimeoutException:
        _raise_if_authentication_expired(driver)
        raise AttendancePageError("온라인 출석부 로딩 시간이 초과되었습니다.")


def _attendance_status_for_url(driver, target_url):
    target_url = absolute_module_url(target_url, "vod")
    target_cmid = _extract_course_id(target_url)
    for record in _attendance_records(driver):
        row_url = record["url"]
        if (row_url == target_url
                or (target_cmid and _extract_course_id(row_url) == target_cmid)):
            return record["status"]
    return None


def _wait_for_attendance_completion(driver, attendance_url, lecture_url,
                                    attempts=6, interval=3, sleep_fn=None,
                                    stop_event=None):
    sleep_fn = sleep_fn or time.sleep
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        if stop_event and stop_event.is_set():
            return False
        driver.get(attendance_url)
        _wait_for_attendance_page(driver)
        if _attendance_status_for_url(driver, lecture_url) == "O":
            return True
        if attempt + 1 < attempts:
            sleep_fn(interval)
    return False


def _available_vod_allowlist(course_data):
    if ("available_vod_urls" not in course_data
            and "available_vod_cmids" not in course_data):
        return None
    urls = {
        safe for url in (course_data.get("available_vod_urls") or [])
        if (safe := absolute_module_url(url, "vod"))
    }
    cmids = {str(cmid) for cmid in (course_data.get("available_vod_cmids") or []) if cmid}
    return urls, cmids


def _vod_is_allowed(lecture, allowlist):
    if allowlist is None:
        return True
    urls, cmids = allowlist
    url = absolute_module_url(lecture.get("url", ""), "vod")
    cmid = _extract_course_id(url)
    return bool(url and (url in urls or (cmid and cmid in cmids)))


def _allowlist_completed(driver, allowlist):
    if allowlist is None:
        return True
    pending_urls, pending_cmids = (set(allowlist[0]), set(allowlist[1]))
    if not pending_urls and not pending_cmids:
        return True
    for record in _attendance_records(driver):
        if record["status"] != "O":
            continue
        url = record["url"]
        cmid = _extract_course_id(url)
        pending_urls.discard(url)
        if cmid:
            pending_cmids.discard(cmid)
    return not pending_urls and not pending_cmids


def _get_weeks_to_process(driver, course_data):
    available_weeks = course_data.get('available_weeks')
    if available_weeks is not None:
        return [int(w) for w in available_weeks]
    prescanned = course_data.get('uncompleted_weeks')
    if prescanned is not None:
        return [int(w) for w in prescanned]

    course_id = _extract_course_id(course_data['url'])
    if course_id:
        active_weeks = get_active_weeks(driver, course_id)
        if active_weeks is not None:
            if not active_weeks:
                info(f"[{course_data['class_name']}] 현재 수강 가능한 주차가 없습니다.")
            return active_weeks
        else:
            debug("수강 가능 주차 확인 실패. 전체 주차를 대상으로 진행합니다.")
    return list(range(1, 16))


def process_course_with_recovery(driver, wait, course_data, stop_event, user_id, password, log_cb=None, headless=True):
    max_retries = 3
    retry_count = 0
    authentication_retried = False
    skipped_lectures = set()
    attempted_lectures = set()
    lecture_failure_count = {}
    available_vods = _available_vod_allowlist(course_data)
    MAX_LECTURE_FAILURES = 3
    while retry_count < max_retries:
        if stop_event and stop_event.is_set():
            return False, driver, wait
        try:
            info(f"[{course_data['class_name']}] 강의 페이지에 접속합니다.")
            weeks_to_process = _get_weeks_to_process(driver, course_data)
            if not weeks_to_process:
                info(f"[{course_data['class_name']}] 처리할 주차가 없습니다.")
                if available_vods is None or not any(available_vods):
                    return True, driver, wait
            driver.get(course_data['url'])
            _wait_for_attendance_page(driver)
            for week in weeks_to_process:
                if stop_event and stop_event.is_set():
                    info("정지 요청을 확인했습니다.")
                    return False, driver, wait
                week_str = str(week)
                try:
                    while True:
                        uncompleted_lectures = get_uncompleted_lectures_by_week(driver, week_str)
                        def lecture_key(item):
                            return item.get('url') or f"{week_str}:{item['title']}"
                        uncompleted_lectures = [
                            lecture for lecture in uncompleted_lectures
                            if _vod_is_allowed(lecture, available_vods)
                            and lecture_key(lecture) not in skipped_lectures
                            and lecture_key(lecture) not in attempted_lectures
                        ]
                        if not uncompleted_lectures:
                            break
                        if stop_event and stop_event.is_set():
                            return False, driver, wait
                        lecture_info = uncompleted_lectures[0]
                        lecture_title = lecture_info['title']
                        key = lecture_key(lecture_info)
                        info(f"[{course_data['class_name']}] {week_str} 주차 - {lecture_title} 수강 시작")
                        try:
                            result = watch_lecture(driver, wait, week_str, lecture_info, stop_event)
                            if stop_event and stop_event.is_set():
                                return False, driver, wait
                            if result not in (None, "unavailable"):
                                confirmed = _wait_for_attendance_completion(
                                    driver, course_data['url'], lecture_info['url'],
                                    stop_event=stop_event)
                                if confirmed:
                                    attempted_lectures.add(key)
                                    info(f"강의 수강이 완료되었습니다: {lecture_title}")
                                    _interlude(stop_event, 5, 12)
                                    continue
                                error(f"[{course_data['class_name']}] {lecture_title} - 출석 반영을 확인하지 못했습니다.")
                                result = None
                            _interlude(stop_event, 2, 4)
                            driver.get(course_data['url'])
                            _wait_for_attendance_page(driver)
                            if result == "unavailable":
                                info(f"[{course_data['class_name']}] {lecture_title} - 열람 불가로 건너뜁니다.")
                                skipped_lectures.add(key)
                                continue
                            if result is None:
                                lecture_failure_count[key] = lecture_failure_count.get(key, 0) + 1
                                if lecture_failure_count[key] >= MAX_LECTURE_FAILURES:
                                    error(f"[{course_data['class_name']}] {lecture_title} - {MAX_LECTURE_FAILURES}회 실패. 건너뜁니다.")
                                    skipped_lectures.add(key)
                                continue
                        except InvalidSessionIdException:
                            raise
                        except Exception as e:
                            _raise_if_fatal_webdriver(e)
                            if stop_event and stop_event.is_set():
                                return False, driver, wait
                            error(f"강의 처리 중 오류: {e}")
                            lecture_failure_count[key] = lecture_failure_count.get(key, 0) + 1
                            if lecture_failure_count[key] >= MAX_LECTURE_FAILURES:
                                error(f"[{course_data['class_name']}] {lecture_title} - 반복 오류로 건너뜁니다.")
                                skipped_lectures.add(key)
                            try:
                                driver.get(course_data['url'])
                                _wait_for_attendance_page(driver)
                            except Exception:
                                pass
                            continue
                except InvalidSessionIdException:
                    error(f"{week_str}주차 처리 중 세션 끊김. 재로그인 시도...")
                    raise
                except Exception as e:
                    if stop_event and stop_event.is_set():
                        return False, driver, wait
                    error(f"{week_str}주차 처리 중 오류: {e}")
                    raise
            if skipped_lectures:
                error(f"[{course_data['class_name']}] 처리하지 못한 영상이 있습니다.")
                return False, driver, wait
            if not _allowlist_completed(driver, available_vods):
                error(
                    f"[{course_data['class_name']}] 대상 영상의 출석 완료를 모두 확인하지 못했습니다.")
                return False, driver, wait
            info(f"[{course_data['class_name']}] 모든 주차 처리 완료!")
            return True, driver, wait
        except Exception as e:
            if stop_event and stop_event.is_set():
                return False, driver, wait
            authentication_expired = isinstance(
                e, BrowserAuthenticationExpiredError)
            if not authentication_expired and not _is_fatal_webdriver_error(e):
                error(f"예상치 못한 오류: {e}")
                return False, driver, wait
            if authentication_expired and authentication_retried:
                error(
                    f"[{course_data['class_name']}] 로그인 세션이 다시 만료되어 "
                    "이 강의를 건너뜁니다.")
                return False, driver, wait
            if authentication_expired:
                authentication_retried = True
                reason = "로그인 세션 만료"
            else:
                reason = "브라우저 연결 끊김"
            error(
                f"강의 처리 중 {reason}. "
                f"재시도 {retry_count + 1}/{max_retries}")
            retry_count += 1
            if retry_count >= max_retries:
                error(f"[{course_data['class_name']}] 최대 재시도 횟수 초과. 이 강의를 건너뜁니다.")
                return False, driver, wait
            try:
                driver.quit()
            except Exception:
                pass
            driver, wait = init_driver(headless=headless)
            if not login(driver, wait, user_id, password):
                error("재로그인 실패")
                return False, driver, wait
            info("재로그인 성공! 강의를 다시 시작합니다.")
            set_log_callback(log_cb)
    return False, driver, wait
