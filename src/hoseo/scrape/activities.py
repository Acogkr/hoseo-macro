from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

from .. import selectors as S
from ..http_client import fetch_soup, fetch_html
from ..models import Activity, ScheduleItem, QUIZ, ASSIGN, ZOOM
from ..dates import parse_kor_datetime

_DUR_ISO = re.compile(
    r'"duration"\s*:\s*"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?'
)
_DUR_SEC = re.compile(r'"duration"\s*:\s*(\d{2,6}(?:\.\d+)?)')
_DUR_DATA = re.compile(r'data-duration["\s:=]+["\']?(\d{2,6})')

_EMPTY_ACTIVITY_MARKERS = {
    QUIZ: (
        "등록된 퀴즈가 없습니다", "퀴즈가 없습니다", "no quizzes",
        "no quiz activities",
    ),
    ASSIGN: (
        "등록된 과제가 없습니다", "과제가 없습니다", "no assignments",
        "no assignment activities",
    ),
    ZOOM: (
        "등록된 zoom이 없습니다", "zoom 활동이 없습니다", "화상강의가 없습니다",
        "no zoom activities",
    ),
}


class ActivityIndexParseError(RuntimeError):
    pass


def _cmid(href: str):
    try:
        return parse_qs(urlparse(href).query).get("id", [None])[0]
    except Exception:
        return None


def _cell_text(tr, cls: str) -> str:
    el = tr.select_one(f"td.{cls}")
    return el.get_text(" ", strip=True) if el else ""


def parse_vod_duration(html: str) -> int:
    m = _DUR_ISO.search(html)
    if m:
        return int(int(m.group(1) or 0) * 3600
                   + int(m.group(2) or 0) * 60
                   + float(m.group(3) or 0))
    m = _DUR_SEC.search(html)
    if m:
        sec = float(m.group(1))
        if 10 < sec < 86400:
            return int(sec)
    m = _DUR_DATA.search(html)
    if m:
        sec = int(m.group(1))
        if 10 < sec < 86400:
            return sec
    return 0


def fetch_vod_duration(session, url: str) -> int:
    try:
        return parse_vod_duration(fetch_html(session, url))
    except Exception:
        return 0


def _cell(tr, cls, idx):
    el = tr.select_one(f"td.{cls}")
    if el is None:
        cells = tr.find_all("td")
        el = cells[idx] if len(cells) > idx else None
    return el.get_text(" ", strip=True) if el else ""


def _explicit_empty_activity(soup, kind: str) -> bool:
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).casefold()
    return any(marker in text for marker in _EMPTY_ACTIVITY_MARKERS[kind])


def _module_candidate(href: str, kind: str) -> bool:
    parsed = urlparse(href or "")
    if parsed.netloc and parsed.netloc.lower() != urlparse(S.BASE).netloc.lower():
        return False
    path = parsed.path.lower()
    if f"/mod/{kind}/" in path:
        return True
    return not parsed.netloc and path.lstrip("./") == "view.php"


def _activity_rows(soup, table_selector: str):
    rows = soup.select(f"{table_selector} tbody tr")
    return rows or soup.select("tr")


def parse_quiz_index(soup) -> list[Activity]:
    result = []
    seen = set()
    rows = _activity_rows(soup, S.QUIZ_INDEX["table"])
    for tr in rows:
        link = tr.select_one(S.QUIZ_INDEX["item_link"])
        if link is None:
            continue
        href = link.get("href", "")
        if not _module_candidate(href, QUIZ):
            continue
        url = S.absolute_module_url(href, "quiz")
        cmid = _cmid(url)
        if not cmid:
            raise ActivityIndexParseError("퀴즈 항목 링크를 해석할 수 없습니다.")
        if cmid in seen:
            continue
        seen.add(cmid)
        title = " ".join(link.get_text(" ", strip=True).split())
        deadline = _cell(tr, "c2", 2)
        score = _cell(tr, "c3", 3)
        submitted = bool(score and score != "-" and any(ch.isdigit() for ch in score))
        a = Activity(type=QUIZ, title=title, cmid=cmid, url=url)
        a.deadline = parse_kor_datetime(deadline)
        a.completed = submitted
        a.score = score if submitted else None
        a.status = "응시완료" if submitted else "미응시"
        result.append(a)
    if not result and not _explicit_empty_activity(soup, QUIZ):
        raise ActivityIndexParseError("퀴즈 목록 구조를 해석할 수 없습니다.")
    return result


def fetch_quizzes(session, course_id: str) -> list[Activity]:
    soup = fetch_soup(session, S.url("quiz_index", course_id=course_id))
    return parse_quiz_index(soup)


def fetch_assignments(session, course_id: str) -> list[Activity]:
    soup = fetch_soup(session, S.url("assign_index", course_id=course_id))
    # The index is the authoritative source for whether Moodle received a
    # submission.  Text such as "별도 제출 없음" in the assignment description
    # is guidance, not a completed LMS submission, so it must never hide a
    # ``미제출`` row from the user's todo list.  The detail page is fetched only
    # when assignment details are opened, which also keeps initial login
    # lightweight.
    return parse_assignment_index(soup)


def _zoom_duration(text: str) -> int:
    hours = re.search(r"(\d+)\s*시간", text or "")
    minutes = re.search(r"(\d+)\s*분", text or "")
    return int(hours.group(1) if hours else 0) * 60 + int(minutes.group(1) if minutes else 0)


def parse_zoom_index(soup) -> list[ScheduleItem]:
    result = []
    tables = soup.select("table.generaltable.mod_index")
    if not tables:
        tables = [table for table in soup.select("table.generaltable")
                  if "강의 시간" in table.get_text(" ", strip=True)]
    for table in tables:
        for row in table.select("tbody tr"):
            link = row.select_one("td.c1 a[href*='view.php?id=']")
            if not link:
                continue
            href = link.get("href", "")
            if not _module_candidate(href, ZOOM):
                continue
            url = S.absolute_module_url(href, "zoom")
            if not url:
                raise ActivityIndexParseError("Zoom 항목 링크를 해석할 수 없습니다.")
            week_text = _cell(row, "c0", 0)
            week_match = re.search(r"(\d+)\s*주차", week_text)
            starts_at = parse_kor_datetime(_cell(row, "c2", 2))
            duration = _zoom_duration(_cell(row, "c3", 3))
            ends_at = None
            if starts_at and duration:
                ends_at = (datetime.fromisoformat(starts_at)
                           + timedelta(minutes=duration)).isoformat()
            result.append(ScheduleItem(
                type=ZOOM,
                title=" ".join(link.get_text(" ", strip=True).split()),
                url=url,
                week=week_match.group(1) if week_match else "",
                starts_at=starts_at,
                ends_at=ends_at,
                status="scheduled",
            ))
    if not result and not _explicit_empty_activity(soup, ZOOM):
        raise ActivityIndexParseError("Zoom 목록 구조를 해석할 수 없습니다.")
    return result


def fetch_zooms(session, course_id: str) -> list[ScheduleItem]:
    soup = fetch_soup(session, S.url("zoom_index", course_id=course_id))
    return parse_zoom_index(soup)


def parse_assignment_index(soup) -> list[Activity]:
    result = []
    rows = _activity_rows(soup, S.ASSIGN_INDEX["table"])
    seen = set()
    for tr in rows:
        link = tr.select_one(S.ASSIGN_INDEX["item_link"])
        if not link:
            continue
        href = link.get("href", "")
        if not _module_candidate(href, ASSIGN):
            continue
        url = S.absolute_module_url(href, "assign")
        cmid = _cmid(url)
        if not cmid:
            raise ActivityIndexParseError("과제 항목 링크를 해석할 수 없습니다.")
        if cmid in seen:
            continue
        seen.add(cmid)
        title = " ".join(link.get_text(" ", strip=True).split())
        submit_text = _cell(tr, "c3", 3)
        submitted = "제출완료" in submit_text.replace(" ", "")
        score = _cell(tr, "c4", 4)
        a = Activity(type=ASSIGN, title=title, cmid=cmid, url=url)
        a.deadline = parse_kor_datetime(_cell(tr, "c2", 2))
        a.completed = submitted
        a.status = submit_text or ("제출완료" if submitted else "미제출")
        a.score = score if (score and score != "-") else None
        result.append(a)
    if not result and not _explicit_empty_activity(soup, ASSIGN):
        raise ActivityIndexParseError("과제 목록 구조를 해석할 수 없습니다.")
    return result
