from __future__ import annotations

import threading
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import scanner
from . import http_client
from .driver_utils import error as log_error
from .models import Course, QUIZ
from .scrape import activities as act_scrape
from .scrape import courses as course_scraper
from .scrape.courses import fetch_vod_list, count_uncompleted_vod_seconds
from .scrape.activities import fetch_vod_duration

_WORKERS = 10


def close_temp_drivers() -> None:
    """Backward-compatible no-op; the CLI owns its authenticated browser."""


def _call_with_clone(source, callable_, *args, **kwargs):
    """Run one request job with an isolated session and always release it."""
    client = http_client.clone_session(source)
    try:
        return callable_(client, *args, **kwargs)
    finally:
        client.close()


def full_scan(driver, wait, progress_callback=None, with_activities=True) -> list[Course]:
    session = http_client.session_from_driver(driver)
    try:
        return full_scan_session(
            session, progress_callback=progress_callback, with_activities=with_activities)
    finally:
        session.close()


def full_scan_session(session, progress_callback=None, with_activities=True) -> list[Course]:
    courses = course_scraper.fetch_courses(session)
    total = len(courses)
    counter = {"n": 0}
    lock = threading.Lock()

    def _pipeline(course: Course) -> Course:
        worker_session = http_client.clone_session(session)
        try:
            home = None
            try:
                home = course_scraper.fetch_course_home(worker_session, course.course_id)
            except http_client.AuthenticationExpiredError:
                raise
            except Exception:
                # The legacy per-module scan remains a safe fallback if the course
                # home is temporarily unavailable or its markup changes.
                pass
            if with_activities:
                modules = home.get("modules") if home else None
                activity_seed = http_client.clone_session(worker_session)
                try:
                    with ThreadPoolExecutor(max_workers=2) as stage:
                        vod_future = stage.submit(
                            scanner.scan_one, worker_session, course, home=home)
                        activity_future = stage.submit(
                            _collect_activities, activity_seed, course, modules)
                        vod_future.result()
                        _apply_activity_results(
                            course, activity_future.result(), home=home)
                finally:
                    activity_seed.close()
            else:
                scanner.scan_one(worker_session, course, home=home)
            with lock:
                counter["n"] += 1
                if progress_callback:
                    progress_callback(counter["n"], total, course.name)
            return course
        finally:
            worker_session.close()

    failed: list[Course] = []
    with ThreadPoolExecutor(max_workers=min(_WORKERS, total or 1)) as pool:
        futs = {pool.submit(_pipeline, c): c for c in courses}
        for f in as_completed(futs):
            try:
                f.result()
            except http_client.AuthenticationExpiredError:
                for pending in futs:
                    pending.cancel()
                raise
            except Exception:
                failed.append(futs[f])

    for course in failed:
        try:
            _pipeline(course)
        except http_client.AuthenticationExpiredError:
            raise
        except Exception as exc:
            course.collection_errors.append(f"출석 정보 수집 실패: {exc}")

    if progress_callback:
        progress_callback(total, total, "분석 완료")

    return courses


def full_scan_sessions(sessions, progress_callback=None,
                       with_activities=True, courses=None) -> list[Course]:
    """Scan with independent Moodle sessions to avoid its per-session lock."""
    clients = list(sessions)
    if not clients:
        raise ValueError("하나 이상의 로그인 세션이 필요합니다.")
    courses = (course_scraper.fetch_courses(clients[0])
               if courses is None else list(courses))
    total = len(courses)
    if not courses:
        if progress_callback:
            progress_callback(0, 0, "분석 완료")
        return courses

    available = queue.Queue()
    for client in clients:
        available.put(client)

    def leased(callable_, *args):
        client = available.get()
        try:
            return callable_(client, *args)
        finally:
            available.put(client)

    homes = [None] * total
    with ThreadPoolExecutor(max_workers=min(len(clients), total)) as pool:
        home_futures = {
            pool.submit(leased, course_scraper.fetch_course_home, course.course_id): index
            for index, course in enumerate(courses)
        }
        for future in as_completed(home_futures):
            index = home_futures[future]
            try:
                homes[index] = future.result()
            except http_client.AuthenticationExpiredError:
                for pending in home_futures:
                    pending.cancel()
                raise
            except Exception:
                # Fall back to the legacy module discovery for this course.
                homes[index] = None

    jobs = []
    remaining = [0] * total
    activity_results = [{"quiz": None, "assign": None} for _ in courses]
    zoom_results = [None for _ in courses]
    successful_activity_kinds = [set() for _ in courses]
    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
        for index, (course, home) in enumerate(zip(courses, homes)):
            modules = home.get("modules") if home else None
            jobs.append((
                pool.submit(leased, scanner.scan_one, course, home),
                index, "vod", "출석 정보",
            ))
            remaining[index] += 1
            if with_activities:
                specs = (
                    ("quiz", act_scrape.fetch_quizzes, "퀴즈"),
                    ("assign", act_scrape.fetch_assignments, "과제"),
                    ("zoom", act_scrape.fetch_zooms, "Zoom 일정"),
                )
                for kind, fetcher, label in specs:
                    if modules is not None and kind not in modules:
                        if kind in ("quiz", "assign"):
                            activity_results[index][kind] = []
                        else:
                            zoom_results[index] = []
                        successful_activity_kinds[index].add(kind)
                        continue
                    jobs.append((
                        pool.submit(leased, fetcher, course.course_id),
                        index, kind, label,
                    ))
                    remaining[index] += 1

        future_map = {future: (index, kind, label)
                      for future, index, kind, label in jobs}
        completed = 0
        for future in as_completed(future_map):
            index, kind, label = future_map[future]
            course = courses[index]
            try:
                result = future.result()
                if kind in ("quiz", "assign"):
                    activity_results[index][kind] = result
                elif kind == "zoom":
                    zoom_results[index] = result
                successful_activity_kinds[index].add(kind)
            except http_client.AuthenticationExpiredError:
                for pending in future_map:
                    pending.cancel()
                raise
            except Exception as exc:
                message = f"{label} 수집 실패: {exc}"
                if message not in course.collection_errors:
                    course.collection_errors.append(message)
                log_error(f"{message} [{course.name}]")
            finally:
                remaining[index] -= 1
                if remaining[index] == 0:
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total, course.name)

    error_prefixes = {
        "vod": "출석 정보 수집 실패:",
        "quiz": "퀴즈 수집 실패:",
        "assign": "과제 수집 실패:",
        "zoom": "Zoom 일정 수집 실패:",
    }
    for index, course in enumerate(courses):
        if with_activities:
            replaced_kinds = {
                kind for kind in ("quiz", "assign")
                if activity_results[index][kind] is not None
            }
            activities = []
            for kind in ("quiz", "assign"):
                result = activity_results[index][kind]
                if result is not None:
                    activities += result
            _apply_course_home_completion(activities, homes[index])
            course.activities = [
                item for item in course.activities
                if item.type not in replaced_kinds
            ] + activities
            if zoom_results[index] is not None:
                course.schedules = [item for item in course.schedules
                                    if item.type != "zoom"] + zoom_results[index]
            course.enriched = True
        successful_prefixes = tuple(
            error_prefixes[kind]
            for kind in successful_activity_kinds[index]
        )
        if successful_prefixes:
            course.collection_errors = [
                message for message in course.collection_errors
                if not message.startswith(successful_prefixes)
            ]
    if progress_callback:
        progress_callback(total, total, "분석 완료")
    return courses


def _collect_activities(session, course: Course, modules=None):
    if not course.course_id:
        return [], [], []
    enabled = {"quiz", "assign", "zoom"} if modules is None else set(modules)
    specs = {
        "quiz": (act_scrape.fetch_quizzes, "퀴즈"),
        "assign": (act_scrape.fetch_assignments, "과제"),
        "zoom": (act_scrape.fetch_zooms, "Zoom 일정"),
    }
    selected = {kind: spec for kind, spec in specs.items() if kind in enabled}
    if not selected:
        return [], [], []
    activities = []
    zooms = []
    errors = []
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {
            kind: pool.submit(
                _call_with_clone, session, fetcher, course.course_id)
            for kind, (fetcher, _label) in selected.items()
        }
        for kind, future in futures.items():
            label = selected[kind][1]
            try:
                result = future.result()
                if kind == "zoom":
                    zooms += result
                else:
                    activities += result
            except http_client.AuthenticationExpiredError:
                raise
            except Exception as exc:
                errors.append(f"{label} 수집 실패: {exc}")
                log_error(f"{label} 수집 실패 [{course.name}]: {exc}")
    return activities, zooms, errors


def _apply_course_home_completion(activities, home) -> None:
    """Merge automatic Moodle completion into ambiguous quiz-index rows."""

    completed = set(
        home.get("completed_activity_cmids", ())
        if isinstance(home, dict) else ()
    )
    if not completed:
        return
    for activity in activities:
        if (activity.type == QUIZ and not activity.completed
                and activity.cmid in completed):
            activity.completed = True
            activity.status = "응시완료"


def _apply_activity_results(course: Course, result, home=None) -> None:
    activities, zooms, errors = result
    _apply_course_home_completion(activities, home)
    course.activities += activities
    course.schedules = [item for item in course.schedules if item.type != "zoom"] + zooms
    course.collection_errors += errors
    course.enriched = True


def _fetch_activities(session, course: Course, modules=None, home=None) -> None:
    if home is None and modules is None:
        try:
            home = course_scraper.fetch_course_home(session, course.course_id)
        except http_client.AuthenticationExpiredError:
            raise
        except Exception:
            home = None
    if modules is None and home is not None:
        modules = home.get("modules")
    _apply_activity_results(
        course,
        _collect_activities(session, course, modules),
        home=home,
    )


def enrich(driver, course: Course) -> Course:
    if course.enriched:
        return course
    session = http_client.session_from_driver(driver)
    try:
        _fetch_activities(session, course)
        return course
    finally:
        session.close()


def enrich_session(session, course: Course) -> Course:
    if course.enriched:
        return course
    _fetch_activities(session, course)
    return course


def fetch_vod_status(driver, course: Course):
    session = http_client.session_from_driver(driver)
    try:
        return course_scraper.fetch_vod_status(session, course.course_id)
    finally:
        session.close()


def fetch_vod_status_session(session, course: Course):
    return course_scraper.fetch_vod_status(session, course.course_id)


def rescan_vods(driver, courses: list[Course]) -> None:
    session = http_client.session_from_driver(driver)
    try:
        rescan_vods_session(session, courses)
    finally:
        session.close()


def rescan_vods_session(session, courses: list[Course]) -> None:
    targets = [c for c in courses if c.course_id]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(targets))) as pool:
        futs = {
            pool.submit(_call_with_clone, session, scanner.scan_one, course): course
            for course in targets
        }
        for f, course in futs.items():
            try:
                f.result()
                course.collection_errors[:] = [
                    error for error in course.collection_errors
                    if not error.startswith("수강 상태 갱신 실패:")
                ]
            except http_client.AuthenticationExpiredError:
                raise
            except Exception as exc:
                message = f"수강 상태 갱신 실패: {exc}"
                if message not in course.collection_errors:
                    course.collection_errors.append(message)
                log_error(f"{message} [{course.name}]")


def prefetch_vod_durations(session, courses: list[Course]) -> int:
    targets = [c for c in courses if c.course_id and c.watchable_count > 0]
    if not targets:
        return 0

    def _course_total(course: Course) -> int:
        worker_session = http_client.clone_session(session)
        try:
            source_weeks = (course.uncompleted_weeks
                            if course.available_weeks is None else course.available_weeks)
            weeks = [str(w) for w in source_weeks]
            try:
                secs = count_uncompleted_vod_seconds(
                    worker_session, course.course_id, weeks)
            except Exception:
                secs = 0
            if secs > 0:
                return secs
            return _vod_total_from_views(
                worker_session, course.course_id, set(weeks))
        finally:
            worker_session.close()

    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
        return sum(pool.map(_course_total, targets))


def _vod_total_from_views(session, course_id: str, weeks: set[str]) -> int:
    try:
        vods = fetch_vod_list(session, course_id)
    except Exception:
        return 0
    targets = [v for v in vods if str(v["week"]) in weeks]
    if not targets:
        return 0

    def _duration(vod) -> int:
        return _call_with_clone(session, fetch_vod_duration, vod["url"])

    with ThreadPoolExecutor(max_workers=min(10, len(targets))) as pool:
        durations = list(pool.map(_duration, targets))
    return sum(durations)


def open_url(url: str, driver=None, headless: bool = True) -> bool:
    """Open a safe LMS link inside an already authenticated browser."""
    from urllib.parse import urlparse

    from .selectors import BASE, absolute_lms_url

    safe_url = absolute_lms_url(url)
    if not safe_url or driver is None:
        return False
    try:
        driver.get(safe_url)
        current = urlparse(str(driver.current_url or ""))
        expected = urlparse(BASE)
        logged_out = (current.path == "/login"
                      or current.path.startswith("/login/"))
        return (current.scheme == expected.scheme
                and current.netloc == expected.netloc
                and not logged_out)
    except Exception:
        return False


def open_activity(activity, driver=None, headless: bool = True) -> bool:
    return open_url(activity.url, driver=driver, headless=headless)


def watch_course_vods(driver, wait, course: Course, stop_event,
                      user_id, password, log_cb=None, headless=True):
    from . import watch

    return watch.process_course_with_recovery(
        driver, wait, course.to_dict(), stop_event,
        user_id, password, log_cb=log_cb, headless=headless,
    )
