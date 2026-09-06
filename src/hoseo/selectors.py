BASE = "https://learn.hoseo.ac.kr"

import re
from urllib.parse import urljoin, urlparse

_BASE_HOST = urlparse(BASE).netloc


def absolute_lms_url(href: str) -> str:
    candidate = urljoin(BASE + "/", href or "")
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.netloc != _BASE_HOST:
        return ""
    return candidate


def absolute_module_url(href: str, module: str) -> str:
    """Resolve a module link and reject links outside the expected LMS module."""
    candidate = urljoin(f"{BASE}/mod/{module}/index.php", href or "")
    parsed = urlparse(candidate)
    if (parsed.scheme != "https" or parsed.netloc != _BASE_HOST
            or parsed.path != f"/mod/{module}/view.php"):
        return ""
    return candidate


def attendance_header_indexes(labels):
    """Return week, lecture-name and per-lecture status column indexes."""
    normalized = [re.sub(r"\s+", "", label or "") for label in labels]

    def find_exact(*names):
        return next((i for i, label in enumerate(normalized) if label in names), None)

    # The live LMS table also contains "출석인정요구시간" and
    # "주차 출결상태". Generic substring matching would select those instead
    # of the per-lecture status column.
    return (
        find_exact("주차", "주"),
        find_exact("강의자료", "강의콘텐츠", "콘텐츠", "학습자료", "강의명"),
        find_exact("출결상태", "출석상태", "이수상태", "수강상태", "출석"),
    )

URL = {
    "login":        f"{BASE}/login/index.php",
    "course_list":  f"{BASE}/local/ubion/user/index.php",
    "course_home":  f"{BASE}/course/view.php?id={{course_id}}",
    "attendance":   f"{BASE}/local/ubonattend/my_status.php?id={{course_id}}",
    "vod_index":    f"{BASE}/mod/vod/index.php?id={{course_id}}",
    "quiz_index":   f"{BASE}/mod/quiz/index.php?id={{course_id}}",
    "assign_index": f"{BASE}/mod/assign/index.php?id={{course_id}}",
    "zoom_index":   f"{BASE}/mod/zoom/index.php?id={{course_id}}",
    "syllabus":     f"{BASE}/local/ubion/course/syllabus.php?id={{course_id}}",
}


def url(name: str, **kwargs) -> str:
    return URL[name].format(**kwargs)


COURSE_LIST = {
    "rows":            "table.table-coursemos tbody tr",
    "empty_row_class": "emptyrow",
    "name_link":       "td.col-name a",
}

ATTENDANCE = {
    "table":              "table.table-coursemos",
    "rows":               "tbody tr",
    "status_done_values": ("O",),
}

VOD_INDEX = {
    "rows":         "table.generaltable tbody tr",
    "week_cell":    "td.cell.c0",
    "item_link":    "a[href*='view.php?id=']",
    "week_pattern": r"(\d+)주차\s*\[(\d+)월(\d+)일\s*-\s*(\d+)월(\d+)일\]",
}

QUIZ_INDEX = {
    "table":     "table.generaltable",
    "item_link": "a[href*='view.php?id=']",
}

ASSIGN_INDEX = {
    "table":     "table.generaltable",
    "item_link": "a[href*='/mod/assign/view.php']",
}
