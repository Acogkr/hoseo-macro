from __future__ import annotations

import re
import datetime
from urllib.parse import urlparse, parse_qs

from .. import selectors as S
from ..http_client import AuthenticationExpiredError, fetch_soup
from ..models import Course, ScheduleItem, VOD

_WEEK_RE = re.compile(S.VOD_INDEX["week_pattern"])
_VOD_WINDOW_RE = re.compile(
    r"진도\s*체크\s*기간\s*:\s*"
    r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*~\s*"
    r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"
)
_DATE_RANGE_RE = re.compile(
    r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*~\s*"
    r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"
)
_WEEK_CELL_RE = re.compile(r"^\s*(\d+)\s*(?:주차|주)?\s*$")
_KST = datetime.timezone(datetime.timedelta(hours=9))

_EMPTY_VOD_MARKERS = (
    "등록된 동영상이 없습니다",
    "등록된 콘텐츠가 없습니다",
    "등록된 강의 자료가 없습니다",
    "학습할 동영상이 없습니다",
    "no video activities",
    "no activities",
)

_EMPTY_COURSE_MARKERS = (
    "수강 중인 강좌가 없습니다",
    "수강중인 강좌가 없습니다",
    "등록된 강좌가 없습니다",
    "등록된 강의가 없습니다",
    "표시할 강좌가 없습니다",
    "no courses",
)


class CourseListParseError(RuntimeError):
    pass


class AttendanceParseError(RuntimeError):
    pass


class CourseHomeParseError(RuntimeError):
    pass


class VodIndexParseError(RuntimeError):
    pass


def _header_indexes(table) -> tuple[int | None, int | None, int | None]:
    header = table.select_one("thead tr")
    if header is None:
        return None, None, None
    labels = [cell.get_text(" ", strip=True)
              for cell in header.find_all(["th", "td"])]
    return S.attendance_header_indexes(labels)


def _header_index(table, *names) -> int | None:
    header = table.select_one("thead tr") if table else None
    if header is None:
        return None
    normalized = [re.sub(r"\s+", "", cell.get_text(" ", strip=True))
                  for cell in header.find_all(["th", "td"])]
    return next((i for i, label in enumerate(normalized) if label in names), None)


def _vod_url(row) -> str:
    for link in row.select("a[href]"):
        url = S.absolute_module_url(link.get("href", ""), "vod")
        if url:
            return url
    return ""


def _has_vod_candidate(row) -> bool:
    for link in row.select("a[href]"):
        href = link.get("href", "")
        parsed = urlparse(href)
        if "/mod/vod/" in parsed.path.lower():
            return True
        relative_path = parsed.path.lstrip("./")
        if (not parsed.netloc and "/" not in relative_path
                and relative_path == "view.php" and "id" in parse_qs(parsed.query)):
            return True
    return False


def _week_number(text: str) -> str | None:
    match = _WEEK_CELL_RE.fullmatch(text or "")
    return match.group(1) if match else None


def _rowspan(cell) -> int:
    try:
        return max(1, int(cell.get("rowspan", 1)))
    except (TypeError, ValueError):
        return 1


def _explicit_empty_vod(scope) -> bool:
    text = re.sub(r"\s+", " ", scope.get_text(" ", strip=True)).casefold()
    return any(marker in text for marker in _EMPTY_VOD_MARKERS)


def _explicit_empty_courses(soup, rows) -> bool:
    if any(S.COURSE_LIST["empty_row_class"] in (row.get("class") or [])
           for row in rows if row is not None):
        return True
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).casefold()
    return any(marker in text for marker in _EMPTY_COURSE_MARKERS)


def _vod_index_rows(soup):
    tables = soup.select("table.generaltable")
    if not tables:
        if _explicit_empty_vod(soup):
            return []
        raise VodIndexParseError("동영상 목록 표를 찾을 수 없습니다.")

    candidate_tables = []
    for table in tables:
        header_text = re.sub(
            r"\s+", "", table.select_one("thead").get_text(" ", strip=True)
        ) if table.select_one("thead") else ""
        if (table.select_one(S.VOD_INDEX["week_cell"])
                or table.select_one(S.VOD_INDEX["item_link"])
                or "주차" in header_text):
            candidate_tables.append(table)
    if not candidate_tables:
        if _explicit_empty_vod(soup):
            return []
        raise VodIndexParseError("동영상 목록 표의 구조를 확인할 수 없습니다.")

    rows = [row for table in candidate_tables for row in table.select("tbody tr")]
    if not rows:
        if _explicit_empty_vod(soup):
            return []
        raise VodIndexParseError("동영상 목록 표의 본문을 찾을 수 없습니다.")
    if _explicit_empty_vod(soup) and not any(_vod_url(row) for row in rows):
        return []

    nonempty_week_cells = []
    for row in rows:
        cell = row.select_one(S.VOD_INDEX["week_cell"])
        if cell and cell.get_text(" ", strip=True):
            text = cell.get_text(" ", strip=True)
            if _WEEK_RE.search(text):
                nonempty_week_cells.append(text)
            elif _has_vod_candidate(row):
                raise VodIndexParseError("동영상 목록의 주차 형식을 해석할 수 없습니다.")
    if not nonempty_week_cells:
        if _explicit_empty_vod(soup):
            return []
        raise VodIndexParseError("동영상 목록에서 주차 정보를 찾을 수 없습니다.")
    return rows


def _attendance_rows(soup, active_weeks: list[int]):
    table = soup.select_one(S.ATTENDANCE["table"])
    if not table:
        raise AttendanceParseError("온라인 출석부 표를 찾을 수 없습니다.")
    header = table.select_one("thead tr")
    header_cells = header.find_all(["th", "td"], recursive=False) if header else []
    week_idx, name_idx, status_idx = _header_indexes(table)
    if header and (name_idx is None or status_idx is None):
        raise AttendanceParseError("온라인 출석부의 강의명 또는 출결상태 열을 찾을 수 없습니다.")
    # The live table leaves its week header blank, so column zero remains the
    # safe legacy fallback when a semantic week label is unavailable.
    layout_week_idx = week_idx if week_idx is not None else 0
    active_set = {str(w) for w in active_weeks}
    rows = []
    current = None
    week_span_remaining = 0
    vod_candidates_seen = 0
    recognized_week_rows = 0
    for row in table.select("tbody tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        texts = [cell.get_text(" ", strip=True) for cell in cells]

        week_omitted = week_span_remaining > 0
        if week_omitted:
            week_span_remaining -= 1
        else:
            candidate = (cells[layout_week_idx]
                         if layout_week_idx < len(cells) else None)
            parsed_week = _week_number(
                candidate.get_text(" ", strip=True) if candidate else "")
            if parsed_week is not None:
                current = parsed_week
                recognized_week_rows += 1
                week_span_remaining = _rowspan(candidate) - 1
            elif (current is not None and header_cells
                  and len(cells) == len(header_cells) - 1):
                # Some saved/normalized HTML omits the rowspan attribute but
                # still removes the week cell from continuation rows.
                week_omitted = True
            else:
                current = None

        vod_url = _vod_url(row)
        vod_candidate = bool(vod_url) or _has_vod_candidate(row)
        if vod_candidate:
            vod_candidates_seen += 1
            if current is None:
                raise AttendanceParseError("온라인 출석부의 동영상 주차를 해석할 수 없습니다.")
        if not current or current not in active_set:
            continue

        def row_index(index, fallback):
            resolved = index if index is not None else fallback
            if week_omitted and resolved > layout_week_idx:
                resolved -= 1
            return resolved

        ni = row_index(name_idx, 1)
        si = row_index(status_idx, 5)
        if vod_candidate and not vod_url:
            raise AttendanceParseError("온라인 출석부의 동영상 링크를 해석할 수 없습니다.")
        if not vod_candidate:
            # Attendance tables may mix Zoom or other activities into a week.
            continue
        if ni < 0 or si < 0 or ni >= len(texts) or si >= len(texts):
            raise AttendanceParseError("온라인 출석부의 동영상 행 구조를 해석할 수 없습니다.")
        name = texts[ni].strip()
        if not name:
            raise AttendanceParseError("온라인 출석부의 동영상 강의명이 비어 있습니다.")
        rows.append((current, name, texts[si].strip(), texts, vod_url))
    if vod_candidates_seen and recognized_week_rows == 0:
        raise AttendanceParseError("온라인 출석부의 동영상 주차를 해석할 수 없습니다.")
    if active_set and vod_candidates_seen == 0 and not _explicit_empty_vod(soup):
        raise AttendanceParseError(
            "온라인 출석부에서 동영상 행을 찾을 수 없습니다.")
    return rows


def _course_id_from_href(href: str):
    try:
        return parse_qs(urlparse(href).query).get("id", [None])[0]
    except Exception:
        return None


def fetch_courses(session) -> list[Course]:
    soup = fetch_soup(session, S.url("course_list"))
    courses: list[Course] = []
    primary_table = soup.select_one("table.table-coursemos")
    professor_idx = _header_index(primary_table, "교수", "교수명", "담당교수")
    rows = soup.select(S.COURSE_LIST["rows"])
    if not rows:
        fallback_links = soup.select("a[href*='course/view.php?id=']")
        if not fallback_links and primary_table is None:
            raise CourseListParseError("강의 목록 표를 찾을 수 없습니다.")
        rows = [link.find_parent("tr") for link in fallback_links]
    seen = set()
    for row in rows:
        if row is None:
            continue
        classes = row.get("class", [])
        if S.COURSE_LIST["empty_row_class"] in classes:
            continue
        link = row.select_one(S.COURSE_LIST["name_link"])
        if link is None:
            link = row.select_one("a[href*='course/view.php?id=']")
        if not link:
            continue
        course_id = _course_id_from_href(link.get("href", ""))
        if not course_id or course_id in seen:
            continue
        seen.add(course_id)
        professor = ""
        cells = row.find_all("td", recursive=False) or row.find_all("td")
        idx = professor_idx if professor_idx is not None else 2
        if len(cells) > idx:
            professor = cells[idx].get_text(" ", strip=True)
        courses.append(Course(
            course_id=course_id,
            name=link.get_text(strip=True),
            professor=professor,
            url=S.url("attendance", course_id=course_id),
            syllabus_url=S.url("syllabus", course_id=course_id),
        ))
    if not courses and not _explicit_empty_courses(soup, rows):
        raise CourseListParseError(
            "강의 목록 표는 열렸지만 강의 행을 해석할 수 없습니다.")
    return courses


def _week_details(text: str, today: datetime.date):
    match = _WEEK_RE.search(text or "")
    if not match:
        return None
    week, start_month, start_day, end_month, end_day = map(int, match.groups())
    try:
        start_year = today.year - 1 if start_month > end_month and today.month <= end_month else today.year
        end_year = start_year + 1 if end_month < start_month else start_year
        start = datetime.date(start_year, start_month, start_day)
        end = datetime.date(end_year, end_month, end_day)
    except ValueError:
        return None
    return week, start, end


def parse_course_home(soup, now=None) -> dict:
    current = now or datetime.datetime.now(_KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_KST)
    modules = set()
    completed_activity_cmids = set()
    vod_path_seen = False
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        candidate = S.absolute_lms_url(href)
        path = urlparse(candidate).path if candidate else ""
        if path.startswith("/mod/vod/"):
            vod_path_seen = True
        for kind in ("vod", "quiz", "assign", "zoom"):
            if path in (f"/mod/{kind}/view.php", f"/mod/{kind}/index.php"):
                modules.add(kind)

    # The quiz index can leave its score/status cell empty even after an
    # attempt was submitted.  The course home still exposes Moodle's automatic
    # completion state, so retain those module ids for the activity scanner to
    # reconcile without another network request.  Manual completion is not
    # equivalent to a quiz submission and is intentionally excluded.
    for activity in soup.select("li.activity[id^='module-']"):
        module_id = activity.get("id", "")
        match = re.fullmatch(r"module-(\d+)", module_id)
        if match and activity.select_one(".badge-completion-auto-y"):
            completed_activity_cmids.add(match.group(1))

    if (vod_path_seen and "vod" not in modules) or (soup.select_one(
            ".activity.vod, li.modtype_vod, [data-modname='vod']")
            and "vod" not in modules):
        raise CourseHomeParseError("과목 홈의 동영상 링크 형식을 해석할 수 없습니다.")

    sections = soup.select("li[id^='section-']")
    known_course_dom = bool(
        sections
        or soup.select_one(
            ".course-content, "
            "[data-region='course-content']")
        or modules
        or _explicit_empty_vod(soup)
    )
    if not known_course_dom:
        raise CourseHomeParseError("과목 홈의 강의 콘텐츠 영역을 찾을 수 없습니다.")

    active_weeks = set()
    vod_windows = {}
    sections_found = False
    for section in sections:
        title = section.select_one(".sectionname, .section-title, h3")
        details = _week_details(title.get_text(" ", strip=True) if title else "", current.date())
        if details is None:
            continue
        sections_found = True
        week, start, end = details
        if not (start <= current.date() <= end + datetime.timedelta(days=7)):
            continue
        active_weeks.add(week)
        for link in section.select("a[href]"):
            candidate = S.absolute_lms_url(link.get("href", ""))
            url = (S.absolute_module_url(candidate, "vod")
                   if urlparse(candidate).path == "/mod/vod/view.php" else "")
            if not url:
                continue
            activity = link.find_parent("li")
            text = (activity or link).get_text(" ", strip=True)
            match = _DATE_RANGE_RE.search(text)
            if not match:
                continue
            try:
                starts_at = datetime.datetime.fromisoformat(match.group(1)).replace(tzinfo=_KST)
                ends_at = datetime.datetime.fromisoformat(match.group(2)).replace(tzinfo=_KST)
            except ValueError:
                continue
            vod_windows[url] = (starts_at.isoformat(), ends_at.isoformat())
    return {
        "modules": modules,
        "modules_known": True,
        "active_weeks": sorted(active_weeks),
        "active_weeks_known": sections_found,
        "vod_windows": vod_windows,
        "completed_activity_cmids": completed_activity_cmids,
    }


def fetch_course_home(session, course_id: str) -> dict:
    return parse_course_home(fetch_soup(session, S.url("course_home", course_id=course_id)))


def get_active_weeks(session, course_id: str) -> list[int]:
    soup = fetch_soup(session, S.url("vod_index", course_id=course_id))
    today = datetime.date.today()
    active: list[int] = []
    rows = _vod_index_rows(soup)
    valid_weeks = 0
    for row in rows:
        cell = row.select_one(S.VOD_INDEX["week_cell"])
        if not cell:
            continue
        text = cell.get_text(strip=True)
        if _WEEK_RE.search(text) is None:
            continue
        details = _week_details(text, today)
        if details is None:
            raise VodIndexParseError("동영상 목록의 주차 날짜를 해석할 수 없습니다.")
        valid_weeks += 1
        week, start, end = details
        if start <= today <= end + datetime.timedelta(days=7):
            active.append(week)
    if rows and valid_weeks == 0:
        raise VodIndexParseError("동영상 목록에서 유효한 주차를 찾을 수 없습니다.")
    return active


def fetch_vod_list(session, course_id: str) -> list[dict]:
    soup = fetch_soup(session, S.url("vod_index", course_id=course_id))
    result = []
    current_week = None
    for row in _vod_index_rows(soup):
        cell = row.select_one(S.VOD_INDEX["week_cell"])
        if cell:
            m = _WEEK_RE.search(cell.get_text(strip=True))
            if m:
                current_week = int(m.group(1))
        link = row.select_one(S.VOD_INDEX["item_link"])
        if link and current_week is not None:
            href = link.get("href", "")
            try:
                cmid = parse_qs(urlparse(href).query).get("id", [None])[0]
            except Exception:
                continue
            if cmid:
                url = S.absolute_module_url(href, "vod")
                if url:
                    result.append({"week": current_week, "cmid": cmid, "url": url})
        elif link and current_week is None:
            raise VodIndexParseError("동영상 항목의 주차를 확인할 수 없습니다.")
    return result


def parse_attendance(soup, active_weeks: list[int]):
    active_set = {str(w) for w in active_weeks}
    per_week = {w: 0 for w in active_set}
    done = S.ATTENDANCE["status_done_values"]
    uncompleted = total = 0
    for week, _name, status, _texts, _url in _attendance_rows(soup, active_weeks):
        total += 1
        if status not in done:
            per_week[week] += 1
            uncompleted += 1
    weeks = sorted((w for w, c in per_week.items() if c > 0), key=int)
    return uncompleted, weeks, total


def collect_vod_progress(session, course_id: str, active_weeks: list[int], now=None,
                         vod_windows=None):
    soup = fetch_soup(session, S.url("attendance", course_id=course_id))
    done = S.ATTENDANCE["status_done_values"]
    uncompleted = total = available = 0
    uncompleted_weeks = set()
    available_weeks = set()
    schedules = []
    for week, name, status, _texts, url in _attendance_rows(soup, active_weeks):
        total += 1
        if status in done:
            continue
        uncompleted += 1
        uncompleted_weeks.add(week)
        window = (vod_windows or {}).get(url)
        if window:
            start, end = window
            state = classify_vod_state(False, start, end, now=now)
            is_available = state == "available"
        else:
            is_available, start, end = fetch_vod_availability(session, url, now=now)
        state = classify_vod_state(is_available, start, end, now=now)
        schedules.append(ScheduleItem(
            type=VOD,
            title=name,
            url=url,
            week=week,
            starts_at=start,
            ends_at=end,
            status=state,
        ))
        if state == "available":
            available += 1
            available_weeks.add(week)
    return (uncompleted, sorted(uncompleted_weeks, key=int), total,
            available, sorted(available_weeks, key=int), schedules)


def count_uncompleted_vods(session, course_id: str, active_weeks: list[int], now=None):
    """Backward-compatible count view of the richer VOD progress scan."""
    return collect_vod_progress(session, course_id, active_weeks, now=now)[:5]


_TIME_COLON_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
_TIME_KOR_RE = re.compile(r"(?:(\d+)\s*시간)?\s*(?:(\d+)\s*분)?\s*(?:(\d+)\s*초)")


def _to_seconds(text: str) -> int:
    best = 0
    for h, m, s in _TIME_COLON_RE.findall(text):
        secs = int(h) * 3600 + int(m) * 60 + int(s) if s else int(h) * 60 + int(m)
        if 10 <= secs < 86400:
            best = max(best, secs)
    for h, m, s in _TIME_KOR_RE.findall(text):
        secs = int(h or 0) * 3600 + int(m or 0) * 60 + int(s or 0)
        if 10 <= secs < 86400:
            best = max(best, secs)
    return best


def parse_uncompleted_seconds(soup, active_weeks: list[int]) -> int:
    done = S.ATTENDANCE["status_done_values"]
    total = 0
    for _week, _name, status, texts, _url in _attendance_rows(soup, active_weeks):
        if status not in done:
            total += _to_seconds(" ".join(texts))
    return total


def count_uncompleted_vod_seconds(session, course_id: str, active_weeks: list[int], now=None) -> int:
    soup = fetch_soup(session, S.url("attendance", course_id=course_id))
    done = S.ATTENDANCE["status_done_values"]
    total = 0
    for _week, _name, status, texts, url in _attendance_rows(soup, active_weeks):
        if status in done:
            continue
        is_available, _start, _end = fetch_vod_availability(session, url, now=now)
        if is_available is True:
            total += _to_seconds(" ".join(texts))
    return total


def parse_vod_window(soup):
    text = soup.get_text(" ", strip=True)
    match = _VOD_WINDOW_RE.search(text)
    if not match:
        return None, None
    try:
        start = datetime.datetime.fromisoformat(match.group(1)).replace(tzinfo=_KST)
        end = datetime.datetime.fromisoformat(match.group(2)).replace(tzinfo=_KST)
        return start, end
    except ValueError:
        return None, None


def fetch_vod_availability(session, url: str, now=None):
    try:
        start, end = parse_vod_window(fetch_soup(session, url))
    except AuthenticationExpiredError:
        raise
    except Exception:
        return None, None, None
    if start is None or end is None:
        return None, None, None
    current = now or datetime.datetime.now(_KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_KST)
    return start <= current <= end, start.isoformat(), end.isoformat()


def classify_vod_state(available, start: str | None, end: str | None, now=None) -> str:
    if available is True:
        return "available"
    if start is None or end is None:
        return "unknown"
    current = now or datetime.datetime.now(_KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_KST)
    try:
        starts_at = datetime.datetime.fromisoformat(start)
        ends_at = datetime.datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return "unknown"
    if current < starts_at:
        return "upcoming"
    if current > ends_at:
        return "expired"
    return "available"


def parse_vod_status(soup):
    done = S.ATTENDANCE["status_done_values"]
    return [{"week": week, "name": name, "completed": status in done, "url": url}
            for week, name, status, _texts, url in _attendance_rows(soup, range(1, 1000))]


def fetch_vod_status(session, course_id: str):
    soup = fetch_soup(session, S.url("attendance", course_id=course_id))
    result = parse_vod_status(soup)
    for item in result:
        if item["completed"]:
            item.update(available=True, available_from=None, available_until=None,
                        state="completed")
            continue
        available, start, end = fetch_vod_availability(session, item["url"])
        state = classify_vod_state(available, start, end)
        item.update(available=state == "available",
                    available_from=start, available_until=end, state=state)
    return result
