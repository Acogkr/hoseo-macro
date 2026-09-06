import os
import json
import base64
import ctypes
import hashlib
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from ctypes import wintypes
from cryptography.fernet import Fernet

_SESSION_CACHE_MAX_AGE = 30 * 60
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_SESSION_PAYLOAD_BYTES = 512 * 1024
_MAX_SESSION_SETS = 8
_MAX_COOKIES_PER_SESSION = 64
_MAX_COOKIE_FIELD_CHARS = 8 * 1024
_MAX_SELECTED_COURSES = 256
_MAX_COURSE_ID_CHARS = 128
_MAX_ASSIGNMENT_EXCLUSION_ACCOUNTS = 16
_MAX_ASSIGNMENT_EXCLUSIONS_PER_ACCOUNT = 512
_MAX_ACCOUNT_ID_CHARS = 128
_ACCOUNT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSIGNMENT_EXCLUSION_KEY_RE = re.compile(
    r"^[0-9]{1,128}:[0-9]{1,128}$"
)
_CONFIG_LOCK_FILENAME = ".config.lock"
_CONFIG_THREAD_LOCK = threading.RLock()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _windows_protect(value):
    raw, buffer = _blob(value.encode("utf-8"))
    protected = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(raw), None, None, None, None, 0, ctypes.byref(protected)):
        raise ctypes.WinError()
    try:
        data = ctypes.string_at(protected.pbData, protected.cbData)
        return base64.b64encode(data).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(protected.pbData)


def _windows_unprotect(value):
    raw, buffer = _blob(base64.b64decode(value))
    clear = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(raw), None, None, None, None, 0, ctypes.byref(clear)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(clear.pbData, clear.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(clear.pbData)


def _restrict_permissions(path):
    if os.name != 'nt':
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _hide_on_windows(path):
    if os.name == 'nt':
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 2)
        except Exception:
            pass


@contextmanager
def _config_write_lock():
    """Serialize read-modify-write updates across CLI processes."""

    directory = get_config_dir()
    lock_path = directory / _CONFIG_LOCK_FILENAME
    with _CONFIG_THREAD_LOCK:
        with lock_path.open("a+b") as stream:
            _restrict_permissions(lock_path)
            _hide_on_windows(lock_path)
            if os.name == 'nt':
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                    os.fsync(stream.fileno())
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_config(path):
    with path.open('rb') as stream:
        payload = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(payload) > _MAX_CONFIG_BYTES:
        raise ValueError("설정 파일이 허용된 크기를 초과했습니다.")
    value = json.loads(payload.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError("설정 파일 형식이 올바르지 않습니다.")
    return value


def _selected_courses(values):
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        values = [values]
    try:
        iterator = iter(values)
    except TypeError:
        return []
    result = []
    seen = set()
    for value in iterator:
        text = str(value or '').strip()[:_MAX_COURSE_ID_CHARS]
        if text and text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) >= _MAX_SELECTED_COURSES:
            break
    return result


def _assignment_account_hash(user_id):
    """Return a non-plaintext namespace for one LMS account."""

    if not isinstance(user_id, str):
        return ""
    normalized = user_id.strip()
    if not normalized or len(normalized) > _MAX_ACCOUNT_ID_CHARS:
        return ""
    return hashlib.sha256(
        ("hoseo-macro:assignment-exclusions:" + normalized).encode("utf-8")
    ).hexdigest()


def _assignment_exclusion_key(value):
    """Accept only the stable ``course_id:cmid`` representation."""

    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized if _ASSIGNMENT_EXCLUSION_KEY_RE.fullmatch(normalized) else ""


def _assignment_exclusion_keys(values):
    if not isinstance(values, (list, tuple)):
        return []
    result = []
    seen = set()
    for value in values:
        key = _assignment_exclusion_key(value)
        if key and key not in seen:
            seen.add(key)
            result.append(key)
        if len(result) >= _MAX_ASSIGNMENT_EXCLUSIONS_PER_ACCOUNT:
            break
    return result


def _assignment_exclusions(value):
    """Bound and sanitize the account-hash-to-assignment-key mapping."""

    if not isinstance(value, dict):
        return {}
    result = {}
    for raw_account, raw_keys in value.items():
        account = raw_account if isinstance(raw_account, str) else ""
        if not _ACCOUNT_HASH_RE.fullmatch(account):
            continue
        keys = _assignment_exclusion_keys(raw_keys)
        if keys:
            result[account] = keys
        if len(result) >= _MAX_ASSIGNMENT_EXCLUSION_ACCOUNTS:
            break
    return result


def _valid_cookie_sets(cookie_sets):
    if (not isinstance(cookie_sets, list) or not cookie_sets
            or len(cookie_sets) > _MAX_SESSION_SETS):
        return False
    for records in cookie_sets:
        if (not isinstance(records, list) or not records
                or len(records) > _MAX_COOKIES_PER_SESSION):
            return False
        for record in records:
            if not isinstance(record, dict):
                return False
            name = record.get("name")
            value = record.get("value")
            if not isinstance(name, str) or not name or value is None:
                return False
            for field in (name, value, record.get("domain", ""), record.get("path", "/")):
                if len(str(field)) > _MAX_COOKIE_FIELD_CHARS:
                    return False
    return True


def _empty_config(error=None):
    config = {
        "remember_me": False,
        "user_id": "",
        "password": "",
        "selected_courses": None
    }
    if error:
        config["config_error"] = error
    return config


def _write_config(path, config):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix="config-", suffix=".tmp",
                delete=False) as stream:
            # Record the path before serialization.  json.dump can fail after
            # writing a prefix, and delete=False otherwise leaves that partial
            # settings file behind without giving the caller its name.
            temp_path = Path(stream.name)
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        _restrict_permissions(temp_path)
        os.replace(temp_path, path)
        _restrict_permissions(path)
        return True
    except Exception:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False

def get_config_dir():
    if os.name == 'nt':
        appdata = os.getenv('APPDATA') or os.getenv('LOCALAPPDATA') or str(Path.home())
        config_dir = Path(appdata) / 'HoseoMacro'
    else:
        config_dir = Path.home() / '.hoseo_macro'

    config_dir.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            os.chmod(config_dir, 0o700)
        except OSError:
            pass
    return config_dir

def get_config_path():
    return get_config_dir() / 'config.json'

def get_key_path():
    return get_config_dir() / '.key'

def get_or_create_key():
    key_path = get_key_path()

    if key_path.exists():
        with open(key_path, 'rb') as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(key_path, 'wb') as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        _restrict_permissions(key_path)
        _hide_on_windows(key_path)

    return key

def encrypt_password(password):
    if not password:
        return ""

    key = get_or_create_key()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(password.encode())
    return encrypted.decode()

def decrypt_password(encrypted_password):
    if not encrypted_password:
        return ""

    try:
        key = get_or_create_key()
        fernet = Fernet(key)
        decrypted = fernet.decrypt(encrypted_password.encode())
        return decrypted.decode()
    except Exception:
        return ""

def save_config(user_id, password, remember_me, selected_courses=None):
    try:
        with _config_write_lock():
            current_config = {}
            config_path = get_config_path()
            if config_path.exists():
                try:
                    current_config = _read_config(config_path)
                except Exception:
                    pass

            chosen_courses = (
                current_config.get("selected_courses", [])
                if selected_courses is None else selected_courses
            )
            config = {
                "remember_me": bool(remember_me),
                "selected_courses": _selected_courses(chosen_courses),
            }
            exclusions = _assignment_exclusions(
                current_config.get("assignment_exclusions"))
            if exclusions:
                config["assignment_exclusions"] = exclusions
            same_credentials = False
            try:
                if (os.name == 'nt'
                        and current_config.get("user_id_protected")
                        and current_config.get("password_protected")):
                    same_credentials = (
                        _windows_unprotect(current_config["user_id_protected"])
                        == user_id
                        and _windows_unprotect(current_config["password_protected"])
                        == password
                    )
                elif (current_config.get("user_id_encrypted")
                      and current_config.get("password_encrypted")):
                    same_credentials = (
                        decrypt_password(current_config["user_id_encrypted"])
                        == user_id
                        and decrypt_password(current_config["password_encrypted"])
                        == password
                    )
            except Exception:
                # A damaged legacy protection blob must invalidate only the
                # old session-cache comparison.  The newly entered credentials
                # still need to replace the broken configuration.
                same_credentials = False
            if os.name == 'nt':
                config["user_id_protected"] = _windows_protect(user_id) if remember_me else ""
                config["password_protected"] = _windows_protect(password) if remember_me else ""
            else:
                config["user_id_encrypted"] = encrypt_password(user_id) if remember_me else ""
                config["password_encrypted"] = encrypt_password(password) if remember_me else ""
            # A cache belongs to the complete credential set, not just the
            # account name.  Retaining it after a password edit can make a
            # mistyped new password look valid until the old session expires.
            if remember_me and same_credentials:
                for key in ("session_cache_protected", "session_cache_encrypted"):
                    if current_config.get(key):
                        config[key] = current_config[key]
            return _write_config(config_path, config)
    except Exception:
        return False

def load_config():
    config_path = get_config_path()

    if not config_path.exists():
        return _empty_config()

    try:
        config = _read_config(config_path)

        if os.name == 'nt' and (config.get("user_id_protected") or config.get("password_protected")):
            config["user_id"] = _windows_unprotect(config.get("user_id_protected", ""))
            config["password"] = _windows_unprotect(config.get("password_protected", ""))
        else:
            config["user_id"] = decrypt_password(config.get("user_id_encrypted", ""))
            config["password"] = decrypt_password(config.get("password_encrypted", ""))

        config.setdefault("remember_me", False)
        if "selected_courses" not in config:
            config["selected_courses"] = None
        elif config["selected_courses"] is not None:
            config["selected_courses"] = _selected_courses(config["selected_courses"])
        exclusions = _assignment_exclusions(
            config.get("assignment_exclusions"))
        if exclusions:
            config["assignment_exclusions"] = exclusions
        else:
            config.pop("assignment_exclusions", None)

        return config
    except Exception:
        return _empty_config("저장된 로그인 설정을 읽을 수 없습니다.")


def save_session_cache(user_id: str, cookie_sets: list[list[dict]]) -> bool:
    try:
        if not _valid_cookie_sets(cookie_sets):
            return False
        payload = json.dumps({
            "user_id": str(user_id or "")[:_MAX_COOKIE_FIELD_CHARS],
            "saved_at": time.time(),
            "cookie_sets": cookie_sets,
        }, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > _MAX_SESSION_PAYLOAD_BYTES:
            return False
        with _config_write_lock():
            config_path = get_config_path()
            config = _read_config(config_path)
            if os.name == 'nt':
                config["session_cache_protected"] = _windows_protect(payload)
                config.pop("session_cache_encrypted", None)
            else:
                config["session_cache_encrypted"] = encrypt_password(payload)
                config.pop("session_cache_protected", None)
            return _write_config(config_path, config)
    except Exception:
        return False


def load_session_cache(user_id: str, max_age: int = _SESSION_CACHE_MAX_AGE):
    config_path = get_config_path()
    if not config_path.exists():
        return None
    try:
        config = _read_config(config_path)
        if os.name == 'nt':
            protected = config.get("session_cache_protected", "")
            if not protected:
                return None
            raw = _windows_unprotect(protected)
        else:
            encrypted = config.get("session_cache_encrypted", "")
            if not encrypted:
                return None
            raw = decrypt_password(encrypted)
        if len(raw.encode("utf-8")) > _MAX_SESSION_PAYLOAD_BYTES:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        age = time.time() - float(payload["saved_at"])
        cookie_sets = payload.get("cookie_sets")
        if payload.get("user_id") != user_id or age < 0 or age > max_age:
            return None
        if not _valid_cookie_sets(cookie_sets):
            return None
        return cookie_sets
    except Exception:
        return None


def clear_session_cache() -> bool:
    try:
        with _config_write_lock():
            config_path = get_config_path()
            if not config_path.exists():
                return True
            config = _read_config(config_path)
            config.pop("session_cache_protected", None)
            config.pop("session_cache_encrypted", None)
            return _write_config(config_path, config)
    except Exception:
        return False


def save_selected_courses(course_ids) -> bool:
    try:
        with _config_write_lock():
            config_path = get_config_path()
            if not config_path.exists():
                return False
            config = _read_config(config_path)
            config["selected_courses"] = _selected_courses(course_ids)
            return _write_config(config_path, config)
    except Exception:
        return False


def load_assignment_exclusions(user_id: str) -> set[str]:
    """Load locally excluded assignments for exactly one LMS account."""

    account = _assignment_account_hash(user_id)
    if not account:
        return set()
    config_path = get_config_path()
    if not config_path.exists():
        return set()
    try:
        config = _read_config(config_path)
        exclusions = _assignment_exclusions(
            config.get("assignment_exclusions"))
        return set(exclusions.get(account, ()))
    except Exception:
        return set()


def set_assignment_excluded(
        user_id: str, assignment_key: str, excluded: bool) -> bool:
    """Persist one account-scoped local assignment tracking preference."""

    account = _assignment_account_hash(user_id)
    key = _assignment_exclusion_key(assignment_key)
    if not account or not key:
        return False
    try:
        with _config_write_lock():
            config_path = get_config_path()
            if not config_path.exists():
                return False
            config = _read_config(config_path)
            exclusions = _assignment_exclusions(
                config.get("assignment_exclusions"))
            keys = list(exclusions.get(account, ()))
            if excluded:
                if key in keys:
                    return True
                if len(keys) >= _MAX_ASSIGNMENT_EXCLUSIONS_PER_ACCOUNT:
                    return False
                if (account not in exclusions
                        and len(exclusions) >= _MAX_ASSIGNMENT_EXCLUSION_ACCOUNTS):
                    return False
                keys.append(key)
                exclusions[account] = keys
            else:
                if key not in keys:
                    return True
                keys.remove(key)
                if keys:
                    exclusions[account] = keys
                else:
                    exclusions.pop(account, None)
            if exclusions:
                config["assignment_exclusions"] = exclusions
            else:
                config.pop("assignment_exclusions", None)
            return _write_config(config_path, config)
    except Exception:
        return False


def clear_saved_account(preserve_preferences: bool = True) -> bool:
    try:
        with _config_write_lock():
            config_path = get_config_path()
            selected = []
            exclusions = {}
            if preserve_preferences and config_path.exists():
                try:
                    current = _read_config(config_path)
                    selected = _selected_courses(
                        current.get("selected_courses") or [])
                    exclusions = _assignment_exclusions(
                        current.get("assignment_exclusions"))
                except Exception:
                    selected = []
                    exclusions = {}
            config = {
                "remember_me": False,
                "selected_courses": selected,
            }
            if exclusions:
                config["assignment_exclusions"] = exclusions
            return _write_config(config_path, config)
    except Exception:
        return False
