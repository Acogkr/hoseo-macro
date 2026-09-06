"""Read-only diagnostics for the LMS integration and local Chrome setup.

The doctor deliberately limits LMS access to login and GET requests.  It does
not open VOD players, submit activities, or mutate course data.  The returned
dataclasses are presentation-agnostic so the CLI can render them however it
likes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import time
from typing import Any, Callable, Optional

from . import browser_locator, config_manager, driver_utils, http_client, selectors as S
from .scrape import activities as activity_scraper
from .scrape import courses as course_scraper


PASS = "pass"
WARNING = "warning"
FAIL = "fail"
SKIPPED = "skipped"


@dataclass
class DiagnosticCheck:
    """One independently actionable diagnostic result."""

    key: str
    label: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status != FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
            "duration_ms": self.duration_ms,
        }


@dataclass
class DiagnosticReport:
    """Complete diagnostic run, suitable for CLI or JSON rendering."""

    checks: list[DiagnosticCheck] = field(default_factory=list)
    course_count: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def status(self) -> str:
        if any(check.status == FAIL for check in self.checks):
            return FAIL
        if any(check.status in (WARNING, SKIPPED) for check in self.checks):
            return WARNING
        return PASS

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(check.status == status for check in self.checks)
            for status in (PASS, WARNING, FAIL, SKIPPED)
        }

    def get(self, key: str) -> Optional[DiagnosticCheck]:
        return next((check for check in self.checks if check.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
            "course_count": self.course_count,
            "counts": self.counts,
            "checks": [check.to_dict() for check in self.checks],
        }


ProgressCallback = Callable[[DiagnosticCheck], None]


def _load_browser_session():
    """Load Selenium-backed helpers only for an explicit driver probe."""
    from . import session as browser_session

    return browser_session


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def _emit(report: DiagnosticReport, check: DiagnosticCheck,
          callback: Optional[ProgressCallback]) -> None:
    report.checks.append(check)
    if callback is not None:
        try:
            callback(check)
        except Exception:
            # Rendering progress must never change diagnostic outcomes.
            pass


def _skip(key: str, label: str, message: str) -> DiagnosticCheck:
    return DiagnosticCheck(key, label, SKIPPED, message)


def _configuration_check(config: dict[str, Any], path: Path) -> DiagnosticCheck:
    details = {
        "path": str(path),
        "config_exists": path.exists(),
        "remember_me": bool(config.get("remember_me")),
        "credentials_saved": bool(config.get("user_id") and config.get("password")),
        "selected_course_count": len(config.get("selected_courses") or []),
    }
    error = config.get("config_error")
    if error:
        return DiagnosticCheck(
            "config", "로그인 설정", FAIL, str(error), details)
    if details["remember_me"] and details["credentials_saved"]:
        return DiagnosticCheck(
            "config", "로그인 설정", PASS,
            "저장된 로그인 정보를 안전하게 읽었습니다.", details)
    if details["config_exists"]:
        message = "설정 파일은 있지만 저장된 로그인 정보가 없습니다."
    else:
        message = "저장된 로그인 설정이 없습니다."
    return DiagnosticCheck("config", "로그인 설정", WARNING, message, details)


def diagnose_chrome_installation() -> DiagnosticCheck:
    """Locate Chrome without starting it."""
    started = time.perf_counter()
    try:
        path = browser_locator.find_chrome()
    except Exception as exc:
        return DiagnosticCheck(
            "chrome", "Chrome 설치", FAIL,
            f"Chrome 설치 경로를 확인하지 못했습니다: {exc}",
            duration_ms=_elapsed_ms(started),
        )
    if not path:
        return DiagnosticCheck(
            "chrome", "Chrome 설치", FAIL,
            "Google Chrome 또는 Chromium을 찾을 수 없습니다.",
            duration_ms=_elapsed_ms(started),
        )
    return DiagnosticCheck(
        "chrome", "Chrome 설치", PASS, "Chrome 설치를 확인했습니다.",
        {"path": str(path)}, _elapsed_ms(started),
    )


def diagnose_chromedriver(enabled: bool = True,
                          chrome_available: bool = True) -> DiagnosticCheck:
    """Start and close a headless driver to verify the complete browser stack."""
    if not enabled:
        return _skip("chromedriver", "ChromeDriver 준비", "브라우저 구동 검사를 생략했습니다.")
    if not chrome_available:
        return _skip("chromedriver", "ChromeDriver 준비", "Chrome이 없어 구동 검사를 생략했습니다.")

    started = time.perf_counter()
    driver = None
    previous_stdout = driver_utils.PRINT_STDOUT
    driver_utils.set_stdout(False)
    try:
        browser_session = _load_browser_session()
        driver, _wait = browser_session.init_driver(headless=True)
        capabilities = getattr(driver, "capabilities", {}) or {}
        chrome = capabilities.get("chrome", {}) or {}
        driver_version = str(chrome.get("chromedriverVersion", "")).split(" ", 1)[0]
        details = {
            "browser": capabilities.get("browserName", "chrome"),
            "browser_version": capabilities.get("browserVersion", ""),
            "driver_version": driver_version,
        }
        return DiagnosticCheck(
            "chromedriver", "ChromeDriver 준비", PASS,
            "Headless Chrome을 정상적으로 시작하고 제어했습니다.",
            details, _elapsed_ms(started),
        )
    except Exception as exc:
        return DiagnosticCheck(
            "chromedriver", "ChromeDriver 준비", FAIL,
            f"ChromeDriver를 시작하지 못했습니다: {exc}",
            duration_ms=_elapsed_ms(started),
        )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        driver_utils.set_stdout(previous_stdout)


def _module_check(client, course, kind: str) -> DiagnosticCheck:
    specs = {
        "vod": (
            "attendance", "온라인 출석부", "attendance", course_scraper.parse_vod_status,
        ),
        "quiz": (
            "quiz", "퀴즈", "quiz_index", activity_scraper.parse_quiz_index,
        ),
        "assign": (
            "assign", "과제", "assign_index", activity_scraper.parse_assignment_index,
        ),
        "zoom": (
            "zoom", "Zoom 일정", "zoom_index", activity_scraper.parse_zoom_index,
        ),
    }
    key_prefix, label, url_name, parser = specs[kind]
    key = f"{key_prefix}:{course.course_id}"
    full_label = f"{label} · {course.name}"
    started = time.perf_counter()
    try:
        soup = http_client.fetch_soup(
            client, S.url(url_name, course_id=course.course_id))
        items = parser(soup)
        count = len(items)
    except Exception as exc:
        return DiagnosticCheck(
            key, full_label, FAIL, f"페이지 또는 DOM 파서 검사에 실패했습니다: {exc}",
            {"course_id": course.course_id, "module": kind}, _elapsed_ms(started),
        )
    details = {
        "course_id": course.course_id,
        "module": kind,
        "parsed_item_count": count,
    }
    if count == 0:
        return DiagnosticCheck(
            key, full_label, WARNING,
            "페이지는 열렸지만 인식 가능한 항목이 없습니다.",
            details, _elapsed_ms(started),
        )
    return DiagnosticCheck(
        key, full_label, PASS, f"{count}개 항목의 DOM 구조를 정상적으로 해석했습니다.",
        details, _elapsed_ms(started),
    )


def run_diagnostics(user_id: Optional[str] = None,
                    password: Optional[str] = None, *,
                    config: Optional[dict[str, Any]] = None,
                    session=None,
                    check_driver: bool = True,
                    timeout: int = 20,
                    progress_callback: Optional[ProgressCallback] = None) -> DiagnosticReport:
    """Run all safe diagnostics and return presentation-neutral results.

    ``session`` may be an already authenticated ``requests.Session``.  Caller-
    owned sessions are never closed; a session created here is always closed.
    Explicit credentials take precedence over values loaded from configuration.
    """
    run_started = time.perf_counter()
    report = DiagnosticReport()
    client = session
    owns_client = False

    try:
        try:
            config_path = config_manager.get_config_path()
            loaded_config = config_manager.load_config() if config is None else dict(config)
            config_check = _configuration_check(loaded_config, config_path)
        except Exception as exc:
            loaded_config = {}
            config_check = DiagnosticCheck(
                "config", "로그인 설정", FAIL,
                f"로그인 설정을 검사하지 못했습니다: {exc}")
        _emit(report, config_check, progress_callback)

        resolved_user = user_id if user_id is not None else loaded_config.get("user_id", "")
        resolved_password = (password if password is not None
                             else loaded_config.get("password", ""))

        login_started = time.perf_counter()
        if client is not None:
            try:
                # A cheap authenticated GET catches an expired caller-owned session.
                http_client.fetch_html(client, S.url("course_list"), timeout=timeout)
                login_check = DiagnosticCheck(
                    "http_session", "빠른 HTTP 로그인", PASS,
                    "전달된 LMS 세션이 유효합니다.",
                    {"source": "provided"}, _elapsed_ms(login_started),
                )
            except Exception as exc:
                login_check = DiagnosticCheck(
                    "http_session", "빠른 HTTP 로그인", FAIL,
                    f"전달된 LMS 세션을 사용할 수 없습니다: {exc}",
                    {"source": "provided"}, _elapsed_ms(login_started),
                )
                client = None
        elif not resolved_user or not resolved_password:
            login_check = _skip(
                "http_session", "빠른 HTTP 로그인",
                "검사에 사용할 로그인 정보가 없습니다.")
        else:
            try:
                client = http_client.login_session(
                    str(resolved_user), str(resolved_password), timeout=timeout)
                owns_client = True
                login_check = DiagnosticCheck(
                    "http_session", "빠른 HTTP 로그인", PASS,
                    "LMS 로그인 토큰과 HTTP 세션이 정상입니다.",
                    {"source": "login"}, _elapsed_ms(login_started),
                )
            except Exception as exc:
                client = None
                login_check = DiagnosticCheck(
                    "http_session", "빠른 HTTP 로그인", FAIL,
                    f"빠른 HTTP 로그인에 실패했습니다: {exc}",
                    {"source": "login"}, _elapsed_ms(login_started),
                )
        _emit(report, login_check, progress_callback)

        courses = []
        if client is None:
            _emit(report, _skip(
                "courses", "강의 목록", "유효한 LMS 세션이 없어 검사를 생략했습니다."),
                progress_callback)
        else:
            courses_started = time.perf_counter()
            try:
                courses = course_scraper.fetch_courses(client)
                report.course_count = len(courses)
                status = PASS if courses else WARNING
                message = (f"강의 목록 {len(courses)}개를 정상적으로 해석했습니다."
                           if courses else "강의 목록 DOM은 열렸지만 등록 강의를 찾지 못했습니다.")
                courses_check = DiagnosticCheck(
                    "courses", "강의 목록", status, message,
                    {"course_count": len(courses)}, _elapsed_ms(courses_started),
                )
            except Exception as exc:
                courses_check = DiagnosticCheck(
                    "courses", "강의 목록", FAIL,
                    f"강의 목록 페이지 또는 DOM 파서 검사에 실패했습니다: {exc}",
                    duration_ms=_elapsed_ms(courses_started),
                )
            _emit(report, courses_check, progress_callback)

        for course in courses:
            home_started = time.perf_counter()
            try:
                home = course_scraper.fetch_course_home(client, course.course_id)
                raw_modules = home.get("modules")
                if not isinstance(raw_modules, (set, frozenset, list, tuple)):
                    raise ValueError("과목 홈 분석 결과에 모듈 목록이 없습니다.")
                modules = set(raw_modules)
                known_modules = modules.intersection({"vod", "quiz", "assign", "zoom"})
                unknown_modules = sorted(modules - known_modules)
                details = {
                    "course_id": course.course_id,
                    "modules": sorted(known_modules),
                    "active_weeks": list(home.get("active_weeks") or []),
                    "active_weeks_known": bool(home.get("active_weeks_known")),
                }
                if unknown_modules:
                    details["unknown_modules"] = unknown_modules
                home_check = DiagnosticCheck(
                    f"course_home:{course.course_id}", f"과목 홈 · {course.name}", PASS,
                    ("활성 모듈을 정상적으로 판별했습니다."
                     if known_modules else "과목 홈을 해석했으며 활성 모듈은 없습니다."),
                    details, _elapsed_ms(home_started),
                )
            except Exception as exc:
                known_modules = set()
                home_check = DiagnosticCheck(
                    f"course_home:{course.course_id}", f"과목 홈 · {course.name}", FAIL,
                    f"과목 홈 또는 모듈 탐지 검사에 실패했습니다: {exc}",
                    {"course_id": course.course_id}, _elapsed_ms(home_started),
                )
            _emit(report, home_check, progress_callback)

            for kind in ("vod", "quiz", "assign", "zoom"):
                if kind in known_modules:
                    _emit(report, _module_check(client, course, kind), progress_callback)
    finally:
        if owns_client and client is not None:
            try:
                client.close()
            except Exception:
                pass

        chrome_check = diagnose_chrome_installation()
        _emit(report, chrome_check, progress_callback)
        driver_check = diagnose_chromedriver(
            enabled=check_driver, chrome_available=chrome_check.status == PASS)
        _emit(report, driver_check, progress_callback)
        report.duration_ms = _elapsed_ms(run_started)

    return report


__all__ = [
    "PASS", "WARNING", "FAIL", "SKIPPED",
    "DiagnosticCheck", "DiagnosticReport",
    "diagnose_chrome_installation", "diagnose_chromedriver", "run_diagnostics",
]
