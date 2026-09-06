from __future__ import annotations

import time
import threading
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from .selectors import BASE

_RETRY_DELAYS = (1, 3, 8)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_MAX_INFLIGHT = 8
_inflight = threading.BoundedSemaphore(_MAX_INFLIGHT)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_SAME_SITE_VALUES = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
}


def _cookie_rest_value(cookie, name: str):
    """Read a requests/http.cookiejar extension attribute case-insensitively."""

    for key, value in getattr(cookie, "_rest", {}).items():
        if str(key).casefold() == name.casefold():
            return True, value
    return False, None


def _normalized_same_site(value):
    if not isinstance(value, str):
        return None
    return _SAME_SITE_VALUES.get(value.strip().casefold())


def _configure_session(session: requests.Session) -> requests.Session:
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": BASE,
    })
    return session


class AuthenticationExpiredError(requests.RequestException):
    pass


class LoginError(requests.RequestException):
    pass


def _prepare_login(timeout: int = 20):
    session = _configure_session(requests.Session())
    session.headers["Referer"] = f"{BASE}/login/index.php"
    try:
        page = session.get(f"{BASE}/login/index.php", timeout=timeout)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "lxml")
        token = soup.select_one("input[name='logintoken']")
        if token is None or not token.get("value"):
            raise LoginError("LMS 로그인 토큰을 찾을 수 없습니다.")
        return session, token.get("value")
    except LoginError:
        session.close()
        raise
    except requests.RequestException as exc:
        session.close()
        raise LoginError(f"LMS 빠른 로그인 준비에 실패했습니다: {exc}") from exc


def _complete_login(session: requests.Session, token: str, user_id: str,
                    password: str, timeout: int = 20) -> requests.Session:
    try:
        response = session.post(
            f"{BASE}/login/index.php",
            data={
                "anchor": "",
                "logintoken": token,
                "username": user_id,
                "password": password,
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LoginError(f"LMS 빠른 로그인에 실패했습니다: {exc}") from exc
    if "/login/" in response.url or "/login/logout.php" not in response.text:
        raise LoginError("LMS 로그인에 실패했습니다. 자격증명을 확인하세요.")
    session.headers["Referer"] = BASE
    return session


def login_session(user_id: str, password: str, timeout: int = 20) -> requests.Session:
    """Create an authenticated LMS session through Moodle's login token flow."""
    session, token = _prepare_login(timeout=timeout)
    try:
        return _complete_login(session, token, user_id, password, timeout=timeout)
    except Exception:
        session.close()
        raise


def login_session_pool(user_id: str, password: str, size: int = 8,
                       timeout: int = 20) -> list[requests.Session]:
    """Create independent read sessions without multiplying bad-password attempts.

    Moodle/PHP serializes requests that share one session cookie.  Prepare a
    bounded set of login tokens, validate the credentials with one of them,
    then authenticate the rest concurrently so bad passwords are tried once.
    """
    size = max(1, min(8, int(size)))
    prepared = []
    with ThreadPoolExecutor(max_workers=size) as pool:
        futures = [pool.submit(_prepare_login, timeout) for _ in range(size)]
        for future in as_completed(futures):
            try:
                prepared.append(future.result())
            except LoginError:
                pass
    if not prepared:
        raise LoginError("LMS 로그인 페이지에 연결할 수 없습니다.")

    primary, token = prepared.pop(0)
    try:
        _complete_login(primary, token, user_id, password, timeout=timeout)
    except Exception:
        primary.close()
        for session, _token in prepared:
            session.close()
        raise

    sessions = [primary]
    if not prepared:
        return sessions
    with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
        futures = {
            pool.submit(_complete_login, session, secondary_token,
                        user_id, password, timeout): session
            for session, secondary_token in prepared
        }
        for future in as_completed(futures):
            secondary = futures[future]
            try:
                sessions.append(future.result())
            except LoginError:
                secondary.close()
    return sessions


def session_cookie_sets(sessions) -> list[list[dict]]:
    result = []
    for session in sessions:
        cookies = []
        for cookie in session.cookies:
            domain = cookie.domain or ""
            if domain and domain.lstrip(".").lower() != "learn.hoseo.ac.kr":
                continue
            http_only, http_only_value = _cookie_rest_value(cookie, "HttpOnly")
            _same_site_present, same_site_value = _cookie_rest_value(
                cookie, "SameSite")
            record = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
                "http_only": bool(http_only and http_only_value is not False),
            }
            same_site = _normalized_same_site(same_site_value)
            if same_site:
                record["same_site"] = same_site
            cookies.append(record)
        if cookies:
            result.append(cookies)
    return result


def sessions_from_cookie_sets(cookie_sets) -> list[requests.Session]:
    sessions = []
    for records in cookie_sets or []:
        if not isinstance(records, list):
            continue
        session = _configure_session(requests.Session())
        for record in records:
            if not isinstance(record, dict):
                continue
            name = record.get("name")
            value = record.get("value")
            domain = str(record.get("domain") or "")
            if (not name or value is None
                    or (domain and domain.lstrip(".").lower() != "learn.hoseo.ac.kr")):
                continue
            kwargs = {
                "path": str(record.get("path") or "/"),
                "secure": bool(record.get("secure")),
            }
            if domain:
                kwargs["domain"] = domain
            expires = record.get("expires")
            if isinstance(expires, (int, float)):
                kwargs["expires"] = int(expires)
            rest = {}
            if record.get("http_only") is True:
                rest["HttpOnly"] = None
            same_site = _normalized_same_site(record.get("same_site"))
            if same_site:
                rest["SameSite"] = same_site
            # requests.create_cookie defaults to an HttpOnly extension even
            # when none was supplied.  Passing an explicit empty mapping keeps
            # legacy cache records from being upgraded accidentally.
            kwargs["rest"] = rest
            session.cookies.set(str(name), str(value), **kwargs)
        if session.cookies:
            sessions.append(session)
        else:
            session.close()
    return sessions


def _is_login_response(requested_url: str, response) -> bool:
    final_url = getattr(response, "url", "") or ""
    redirected = "/login/" in final_url and "/login/" not in requested_url
    body = getattr(response, "text", "") or ""
    login_form = "input-username" in body and "input-password" in body
    return redirected or (login_form and "/login/" not in requested_url)


def session_from_driver(driver) -> requests.Session:
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    try:
        user_agent = driver.execute_script("return navigator.userAgent") or DEFAULT_UA
    except Exception:
        user_agent = DEFAULT_UA
    session.headers.update({
        "User-Agent": user_agent,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": BASE,
    })
    return session


def clone_session(source: requests.Session) -> requests.Session:
    session = requests.Session()
    session.headers.update(source.headers)
    session.cookies.update(source.cookies)
    session.auth = source.auth
    session.proxies.update(source.proxies)
    session.verify = source.verify
    session.cert = source.cert
    return session


def fetch_html(session: requests.Session, url: str, timeout: int = 20) -> str:
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            with _inflight:
                resp = session.get(url, timeout=timeout)
            if resp.status_code in _RETRYABLE_STATUS and attempt < len(_RETRY_DELAYS):
                wait = (_retry_after_seconds(resp.headers.get("Retry-After"), _RETRY_DELAYS[attempt])
                        if resp.status_code == 429 else _RETRY_DELAYS[attempt])
                time.sleep(wait)
                continue
            resp.raise_for_status()
            if _is_login_response(url, resp):
                raise AuthenticationExpiredError(
                    "LMS 로그인 세션이 만료되었습니다. 다시 로그인하세요.", response=resp)
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.exceptions.RequestException as e:
            if isinstance(e, AuthenticationExpiredError):
                raise
            status = getattr(e.response, "status_code", None)
            if status is not None and status not in _RETRYABLE_STATUS:
                raise
            last_exc = e
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    raise last_exc


def _retry_after_seconds(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return max(0, min(60, int(value)))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, min(60, int((retry_at - datetime.now(timezone.utc)).total_seconds())))
        except (TypeError, ValueError, OverflowError):
            return default


def fetch_soup(session: requests.Session, url: str, timeout: int = 20) -> BeautifulSoup:
    return BeautifulSoup(fetch_html(session, url, timeout=timeout), "lxml")
