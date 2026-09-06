from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

KST = "+09:00"

_KOR = re.compile(
    r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"
    r"(?:\([^)]*\))?\s*,?\s*"
    r"(?:(오전|오후)\s*)?(\d{1,2}):(\d{2})"
)

_NUM = re.compile(
    r"(\d{4})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})"
    r"(?:\([^)]*\))?\s*"
    r"(\d{1,2}):(\d{2})"
)

_DATE_ONLY = re.compile(
    r"(\d{4})(?:[-./]|\s*년\s*)(\d{1,2})(?:[-./]|\s*월\s*)(\d{1,2})"
)


def _apply_ampm(hour: int, ampm: Optional[str]) -> int:
    if ampm == "오전":
        return 0 if hour == 12 else hour
    if ampm == "오후":
        return 12 if hour == 12 else hour + 12
    return hour


def _iso(y, mo, d, h, mi) -> str:
    value = datetime(int(y), int(mo), int(d), int(h), int(mi))
    return value.isoformat() + KST


def parse_kor_datetime(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = text.strip()

    try:
        m = _KOR.search(text)
        if m:
            y, mo, d, ampm, h, mi = m.groups()
            return _iso(y, mo, d, _apply_ampm(int(h), ampm), mi)

        m = _NUM.search(text)
        if m:
            y, mo, d, h, mi = m.groups()
            return _iso(y, mo, d, h, mi)

        m = _DATE_ONLY.search(text)
        if m:
            y, mo, d = m.groups()
            return _iso(y, mo, d, 23, 59)
    except ValueError:
        return None

    return None
