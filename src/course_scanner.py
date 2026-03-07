import re
import datetime
from urllib.parse import urlparse, parse_qs

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException

from driver_utils import info, error, debug


def _extract_course_id(url):
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if 'id' in params:
            return params['id'][0]
    except:
        pass
    return None


def get_active_weeks(driver, course_id):
    try:
        vod_index_url = f"https://learn.hoseo.ac.kr/mod/vod/index.php?id={course_id}"
        driver.get(vod_index_url)

        rows = driver.find_elements(By.CSS_SELECTOR, "table.generaltable tbody tr")
        today = datetime.date.today()
        current_year = today.year

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
                        start_date = datetime.date(current_year, start_month, start_day)
                        end_date = datetime.date(current_year, end_month, end_day)
                        deadline_date = end_date + datetime.timedelta(days=7)

                        if start_date <= today <= deadline_date:
                            active_weeks.append(week_num)
                    except ValueError:
                        continue

            except NoSuchElementException:
                continue

        if active_weeks:
            info(f"현재 수강 가능한 주차: {active_weeks}")
            return active_weeks
        else:
            debug("현재 수강 가능한 주차가 없습니다.")
            return active_weeks

    except Exception as e:
        error(f"수강 가능 주차 확인 중 오류: {e}")
        return None


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


