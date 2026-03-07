import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    NoAlertPresentException,
    InvalidSessionIdException
)

from driver_utils import (
    info, error, debug, set_log_callback,
    human_like_delay, click_delay
)
from auth import init_driver, login
from course_scanner import (
    get_uncompleted_lectures_by_week,
    get_active_weeks,
    _extract_course_id
)


def watch_lecture(driver, wait, week_number, lecture_info, stop_event=None):
    if stop_event and stop_event.is_set():
        return None

    lecture_title_element = lecture_info['element']
    lecture_title = lecture_info['title']
    main_window_handle = None

    try:
        human_like_delay()
        main_window_handle = driver.current_window_handle

        lecture_title_element.click()

        try:
            wait.until(EC.number_of_windows_to_be(2))
            new_window_handle = [w for w in driver.window_handles if w != main_window_handle][0]
            driver.switch_to.window(new_window_handle)
        except TimeoutException:
            error("동영상 플레이어 창이 열리지 않았습니다.")
            return None
        except InvalidSessionIdException:
            error("세션이 끊어졌습니다. 이 강의를 건너뜁니다.")
            return None

        human_like_delay()

        try:
            if EC.alert_is_present()(driver):
                alert = driver.switch_to.alert
                alert_text = alert.text
                alert.accept()
                if "열람이 불가능합니다" in alert_text:
                    error(f"강의 열람 불가: {lecture_title}")
                    driver.switch_to.window(main_window_handle)
                    return "unavailable"
        except NoAlertPresentException:
            pass
        except InvalidSessionIdException:
            error("세션이 끊어졌습니다. 이 강의를 건너뜁니다.")
            return None

        try:
            video_element = wait.until(EC.presence_of_element_located((By.TAG_NAME, "video")))

            try:
                play_button = driver.find_element(By.CLASS_NAME, "vjs-big-play-button")
                if play_button.is_displayed():
                    click_delay()
                    play_button.click()
            except NoSuchElementException:
                pass

            driver.execute_script("arguments[0].play();", video_element)
            info(f"강의 재생을 시작합니다: {lecture_title}")

            start_time = time.time()
            last_log_time = start_time

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

                    if is_ended is None:
                        is_ended = False
                    if current_time is None:
                        current_time = 0
                    if duration is None:
                        duration = 0

                    from driver_utils import video_progress_callback as _vpc
                    if _vpc is not None and duration > 0:
                        _vpc(int(current_time), int(duration), lecture_title)

                except InvalidSessionIdException:
                    error("세션이 끊어졌습니다. 강의 시청을 중단합니다.")
                    break
                except Exception as e:
                    error(f"동영상 상태 확인 중 오류: {e}")
                    time.sleep(3)
                    continue

                if is_ended or (duration > 0 and current_time >= duration):
                    info(f"강의 수강이 완료되었습니다: {lecture_title}")
                    break

                if time.time() - last_log_time > 60:
                    if duration > 0:
                        debug(f"[{lecture_title}] 현재 진행률: {int(current_time)}/{int(duration)} 초")
                    last_log_time = time.time()

                if time.time() - start_time > 3600:
                    error("시간 초과: 동영상 재생 시간이 너무 길어 중단합니다.")
                    break

                time.sleep(2)

        except InvalidSessionIdException:
            error(f"세션이 끊어졌습니다. 강의를 건너뜁니다: {lecture_title}")
        except Exception as e:
            error(f"강의 수강 중 오류가 발생했습니다. ('{lecture_title}'): {e}")

    except InvalidSessionIdException:
        error(f"세션이 끊어졌습니다. 강의를 건너뜁니다: {lecture_title}")
        return None
    except Exception as e:
        error(f"강의 시작 중 오류가 발생했습니다. ('{lecture_title}'): {e}")

    finally:
        try:
            current_windows = driver.window_handles if driver else []

            if len(current_windows) > 1:
                current_window = driver.current_window_handle
                if current_window != main_window_handle:
                    driver.close()

            if main_window_handle and main_window_handle in driver.window_handles:
                driver.switch_to.window(main_window_handle)
            elif len(driver.window_handles) > 0:
                driver.switch_to.window(driver.window_handles[0])

        except InvalidSessionIdException:
            error("세션이 끊어져 창을 닫을 수 없습니다.")
        except Exception as e:
            error(f"창을 닫는 중 오류가 발생했습니다: {e}")

    return lecture_title


def _get_weeks_to_process(driver, course_data):
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


def process_course(driver, wait, course_data, stop_event=None):
    try:
        info(f"[{course_data['class_name']}] 강의 페이지에 접속 했습니다.")

        weeks_to_process = _get_weeks_to_process(driver, course_data)
        if not weeks_to_process:
            return

        driver.get(course_data['url'])
    except InvalidSessionIdException:
        error(f"[{course_data['class_name']}] 세션이 끊어져 강의를 건너뜁니다.")
        return
    except Exception as e:
        error(f"[{course_data['class_name']}] 강의 페이지 접속 중 오류: {e}")
        return

    for week in weeks_to_process:
        if stop_event and stop_event.is_set():
            info("정지 요청을 확인했습니다. 강의 처리를 중단합니다.")
            break

        week_str = str(week)
        try:
            uncompleted_lectures = get_uncompleted_lectures_by_week(driver, week_str)

            if uncompleted_lectures:
                info(f"[{course_data['class_name']}] {week_str} 주차에 미수강 강의 {len(uncompleted_lectures)}개 발견.")

                for lecture_info in uncompleted_lectures:
                    if stop_event and stop_event.is_set():
                        break

                    try:
                        watch_lecture(driver, wait, week_str, lecture_info, stop_event)
                        driver.get(course_data['url'])
                        time.sleep(1)
                    except InvalidSessionIdException:
                        error(f"세션이 끊어졌습니다. 다음 강의로 넘어갑니다.")
                        continue
                    except Exception as e:
                        error(f"강의 처리 중 오류 발생: {e}. 다음 강의로 넘어갑니다.")
                        try:
                            driver.get(course_data['url'])
                        except:
                            pass
                        continue

        except InvalidSessionIdException:
            error(f"[{course_data['class_name']}] {week_str}주차 처리 중 세션이 끊어졌습니다.")
            break
        except Exception as e:
            error(f"[{course_data['class_name']}] {week_str}주차 처리 중 오류: {e}")
            continue


def process_course_with_recovery(driver, wait, course_data, stop_event, user_id, password, log_cb=None):
    max_retries = 3
    retry_count = 0
    skipped_lectures = set()

    while retry_count < max_retries:
        if stop_event and stop_event.is_set():
            return False, driver, wait

        try:
            info(f"[{course_data['class_name']}] 강의 페이지에 접속합니다.")

            weeks_to_process = _get_weeks_to_process(driver, course_data)
            if not weeks_to_process:
                info(f"[{course_data['class_name']}] 처리할 주차가 없습니다.")
                return True, driver, wait

            driver.get(course_data['url'])

            for week in weeks_to_process:
                if stop_event and stop_event.is_set():
                    info("정지 요청을 확인했습니다.")
                    return True, driver, wait

                week_str = str(week)
                try:
                    while True:
                        uncompleted_lectures = get_uncompleted_lectures_by_week(driver, week_str)
                        uncompleted_lectures = [l for l in uncompleted_lectures if l['title'] not in skipped_lectures]

                        if not uncompleted_lectures:
                            break

                        if stop_event and stop_event.is_set():
                            return True, driver, wait

                        lecture_info = uncompleted_lectures[0]

                        info(f"[{course_data['class_name']}] {week_str} 주차 - {lecture_info['title']} 수강 시작")

                        try:
                            result = watch_lecture(driver, wait, week_str, lecture_info, stop_event)
                            driver.get(course_data['url'])
                            time.sleep(2)

                            if result == "unavailable":
                                info(f"[{course_data['class_name']}] {lecture_info['title']} - 열람 불가로 건너뜁니다.")
                                skipped_lectures.add(lecture_info['title'])
                                continue
                        except InvalidSessionIdException:
                            error("세션이 끊어졌습니다. 재로그인을 시도합니다...")

                            try:
                                driver.quit()
                            except:
                                pass

                            driver, wait = init_driver(headless=True)

                            if not login(driver, wait, user_id, password):
                                error("재로그인 실패. 다시 시도합니다.")
                                retry_count += 1
                                raise InvalidSessionIdException("재로그인 실패")

                            info("재로그인 성공! 강의를 계속 진행합니다.")
                            set_log_callback(log_cb)
                            driver.get(course_data['url'])
                            time.sleep(2)
                            continue

                        except Exception as e:
                            error(f"강의 처리 중 오류: {e}")
                            try:
                                driver.get(course_data['url'])
                                time.sleep(2)
                            except:
                                pass
                            continue

                except InvalidSessionIdException:
                    error(f"{week_str}주차 처리 중 세션 끊김. 재로그인 시도...")
                    retry_count += 1
                    break

                except Exception as e:
                    error(f"{week_str}주차 처리 중 오류: {e}")
                    continue

            info(f"[{course_data['class_name']}] 모든 주차 처리 완료!")
            return True, driver, wait

        except InvalidSessionIdException:
            error(f"강의 처리 중 세션 끊김. 재시도 {retry_count + 1}/{max_retries}")
            retry_count += 1

            if retry_count >= max_retries:
                error(f"[{course_data['class_name']}] 최대 재시도 횟수 초과. 이 강의를 건너뜁니다.")
                return False, driver, wait

            try:
                driver.quit()
            except:
                pass

            driver, wait = init_driver(headless=True)

            if not login(driver, wait, user_id, password):
                error("재로그인 실패")
                return False, driver, wait

            info("재로그인 성공! 강의를 다시 시작합니다.")
            set_log_callback(log_cb)
            time.sleep(2)

        except Exception as e:
            error(f"예상치 못한 오류: {e}")
            return False, driver, wait

    return False, driver, wait

