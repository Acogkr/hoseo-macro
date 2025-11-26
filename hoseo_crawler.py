import time
import random
import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException, 
    TimeoutException, 
    NoAlertPresentException, 
    InvalidSessionIdException
)
from webdriver_manager.chrome import ChromeDriverManager

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

def _get_timestamp():
    return datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

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
    delay_time = random.uniform(min_sec, max_sec)
    time.sleep(delay_time)

def human_like_delay():
    base_delay = random.uniform(0.2, 0.6)
    micro_delay = random.uniform(0.1, 0.2)
    time.sleep(base_delay + micro_delay)

def typing_delay():
    delay = random.uniform(0.02, 0.07)
    time.sleep(delay)

def click_delay():
    delay = random.uniform(0.3, 0.8)
    time.sleep(delay)

def random_mouse_movement(driver):
    try:
        script = """
        var event = new MouseEvent('mousemove', {
            'view': window,
            'bubbles': true,
            'cancelable': true,
            'clientX': arguments[0],
            'clientY': arguments[1]
        });
        document.dispatchEvent(event);
        """
        x = random.randint(100, 800)
        y = random.randint(100, 600)
        driver.execute_script(script, x, y)
        time.sleep(random.uniform(0.1, 0.3))
    except:
        pass

def random_scroll(driver):
    try:
        scroll_amount = random.randint(100, 500)
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        time.sleep(random.uniform(0.5, 1.5))
    except:
        pass

def simulate_human_behavior(driver):
    if random.random() > 0.7:
        random_mouse_movement(driver)
    if random.random() > 0.8:
        random_scroll(driver)



def get_stealth_js():
    return '''
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });

        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['ko-KR', 'ko', 'en-US', 'en']
        });

        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {}
        };

        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );

        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';
            if (parameter === 37446) return 'Intel Iris OpenGL Engine';
            return getParameter.call(this, parameter);
        };
        
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
        Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
        Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
    '''

def init_driver():
    options = Options()
    
    options.add_argument("--headless=new")
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
    
    driver.execute_cdp_cmd('Network.setUserAgentOverride', {
        "userAgent": user_agent
    })
    
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': get_stealth_js()
    })
    
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

def get_course_list(driver, wait):
    course_list = []
    try:
        driver.get("https://learn.hoseo.ac.kr/local/ubion/user/index.php")
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "table-coursemos")))
        
        rows = driver.find_elements(By.CSS_SELECTOR, "table.table-coursemos tbody tr")
        for row in rows:
            try:
                if "emptyrow" in row.get_attribute("class"):
                    continue

                name_cell = row.find_element(By.CSS_SELECTOR, "td.col-name a")
                class_name = name_cell.text.strip()
                link = name_cell.get_attribute("href")
                
                if "id=" in link:
                    course_id = link.split("id=")[-1]
                    attendance_url = f"https://learn.hoseo.ac.kr/local/ubonattend/my_status.php?id={course_id}"
                    
                    course_list.append({
                        "class_name": class_name,
                        "url": attendance_url
                    })
            except NoSuchElementException:
                continue
                
    except Exception as e:
        error(f"강의 리스트를 가져오는 중 오류 발생: {e}")
        
    return course_list

def get_uncompleted_lectures_by_week(driver, week_number):
    uncompleted_lectures = []

    try:
        xpath_for_week_cell = f"//table[contains(@class, 'table-coursemos')]//td[normalize-space(text())='{week_number}']"
        week_cell = driver.find_element(By.XPATH, xpath_for_week_cell)
        
        current_row = week_cell.find_element(By.XPATH, "./parent::tr")
        
        rowspan_value = week_cell.get_attribute("rowspan")
        rowspan = int(rowspan_value) if rowspan_value else 1

        try:
            lecture_title_element = current_row.find_element(By.XPATH, "./td[2]//a")
            attendance_status_element = current_row.find_element(By.XPATH, "./td[6]")
            
            attendance_status = attendance_status_element.text.strip()
            if attendance_status != 'O' and attendance_status != 'X':
                uncompleted_lectures.append({
                    "element": lecture_title_element,
                    "title": lecture_title_element.text.strip()
                })
        except NoSuchElementException:
            pass

        if rowspan > 1:
            for _ in range(rowspan - 1):
                current_row = current_row.find_element(By.XPATH, "./following-sibling::tr[1]")
                
                try:
                    lecture_title_element = current_row.find_element(By.XPATH, "./td[1]//a")
                    attendance_status_element = current_row.find_element(By.XPATH, "./td[5]")
                    
                    attendance_status = attendance_status_element.text.strip()
                    if attendance_status != 'O' and attendance_status != 'X':
                        uncompleted_lectures.append({
                            "element": lecture_title_element,
                            "title": lecture_title_element.text.strip()
                        })
                except NoSuchElementException:
                    continue

    except NoSuchElementException:
        pass
    except Exception as e:
        error(f"강의 정보 파싱 중 오류 발생: {e}")

    return uncompleted_lectures

def scan_courses(driver, wait):
    course_list = get_course_list(driver, wait)
    detailed_courses = []
    
    for course in course_list:
        info(f"Scanning {course['class_name']}...")
        driver.get(course['url'])
        
        uncompleted_count = 0
        uncompleted_weeks = []
        
        for week in range(1, 16):
            week_str = str(week)
            lectures = get_uncompleted_lectures_by_week(driver, week_str)
            if lectures:
                count = len(lectures)
                uncompleted_count += count
                uncompleted_weeks.append(week_str)
        
        course['uncompleted_count'] = uncompleted_count
        course['uncompleted_weeks'] = uncompleted_weeks
        detailed_courses.append(course)
        
    return detailed_courses

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
                    error(f"강의 열람 불가: {lecture_title} - 수강 기간이 아니거나 제한된 강의입니다.")
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
                    is_ended = driver.execute_script("return arguments[0].ended;", video_element)
                    current_time = driver.execute_script("return arguments[0].currentTime;", video_element)
                    duration = driver.execute_script("return arguments[0].duration;", video_element)
                    
                    if video_progress_callback and duration > 0:
                        video_progress_callback(int(current_time), int(duration), lecture_title)
                        
                except InvalidSessionIdException:
                    error("세션이 끊어졌습니다. 강의 시청을 중단합니다.")
                    break
                except Exception as e:
                    error(f"동영상 상태 확인 중 오류: {e}")
                    break
                
                if is_ended or (duration > 0 and current_time >= duration):
                    info(f"강의 수강이 완료되었습니다: {lecture_title}")
                    break
                
                if time.time() - last_log_time > 60:
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

def process_course(driver, wait, course_data, stop_event=None):
    try:
        info(f"[{course_data['class_name']}] 강의 페이지에 접속 했습니다.")
        driver.get(course_data['url'])
    except InvalidSessionIdException:
        error(f"[{course_data['class_name']}] 세션이 끊어져 강의를 건너뜁니다.")
        return
    except Exception as e:
        error(f"[{course_data['class_name']}] 강의 페이지 접속 중 오류: {e}")
        return
    
    for week in range(1, 16):
        if stop_event and stop_event.is_set():
            info("정지 요청을 확인했습니다. 강의 처리를 중단합니다.")
            break
        
        try:
            week_str = str(week)
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

def process_course_with_recovery(driver, wait, course_data, stop_event, user_id, password, log_callback=None):
    max_retries = 3
    retry_count = 0
    skipped_lectures = set()
    
    while retry_count < max_retries:
        if stop_event and stop_event.is_set():
            return False, driver, wait
            
        try:
            info(f"[{course_data['class_name']}] 강의 페이지에 접속합니다.")
            driver.get(course_data['url'])
            
            for week in range(1, 16):
                if stop_event and stop_event.is_set():
                    info("정지 요청을 확인했습니다.")
                    return True, driver, wait
                
                try:
                    week_str = str(week)
                    
                    while True:
                        uncompleted_lectures = get_uncompleted_lectures_by_week(driver, week_str)
                        
                        uncompleted_lectures = [l for l in uncompleted_lectures if l['title'] not in skipped_lectures]
                        
                        if not uncompleted_lectures:
                            break
                        
                        if stop_event and stop_event.is_set():
                            return True
                        
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
                            
                            driver, wait = init_driver()
                            
                            if not login(driver, wait, user_id, password):
                                error("재로그인 실패. 다시 시도합니다.")
                                retry_count += 1
                                raise InvalidSessionIdException("재로그인 실패")
                            
                            info("재로그인 성공! 강의를 계속 진행합니다.")
                            set_log_callback(log_callback)
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
            
            driver, wait = init_driver()
            
            if not login(driver, wait, user_id, password):
                error("재로그인 실패")
                return False, driver, wait
            
            info("재로그인 성공! 강의를 다시 시작합니다.")
            set_log_callback(log_callback)
            time.sleep(2)
            
        except Exception as e:
            error(f"예상치 못한 오류: {e}")
            return False, driver, wait
    
    return False, driver, wait
