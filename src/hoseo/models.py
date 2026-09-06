from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as _dt
from typing import Optional
from urllib.parse import parse_qs, urlparse

QUIZ = "quiz"
ASSIGN = "assign"
VOD = "vod"
ZOOM = "zoom"


@dataclass
class Activity:
    type: str
    title: str
    cmid: str = ""
    url: str = ""
    status: str = ""
    score: Optional[str] = None
    deadline: Optional[str] = None
    completed: bool = False
    # A local tracking preference.  This must never be folded into
    # ``completed`` because that field represents the authoritative LMS state.
    excluded: bool = False

    @property
    def is_overdue(self) -> bool:
        if self.completed or not self.deadline:
            return False
        try:
            dt = _dt.fromisoformat(self.deadline)
            return _dt.now(dt.tzinfo) > dt
        except Exception:
            return False


@dataclass
class ScheduleItem:
    type: str
    title: str
    url: str = ""
    week: str = ""
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    status: str = ""
    completed: bool = False


@dataclass
class Course:
    course_id: str
    name: str
    professor: str = ""
    url: str = ""
    activities: list[Activity] = field(default_factory=list)
    uncompleted_count: int = 0
    total_count: int = 0
    uncompleted_weeks: list[str] = field(default_factory=list)
    enriched: bool = False
    collection_errors: list[str] = field(default_factory=list)
    available_count: Optional[int] = None
    available_weeks: Optional[list[str]] = None
    schedules: list[ScheduleItem] = field(default_factory=list)
    syllabus_url: str = ""
    # Presentation-only name chosen after the complete course list is known.
    # The authoritative LMS name in ``name`` is retained for exports and
    # browser automation.
    display_name: str = field(default="", repr=False, compare=False)

    def pending(self, kind: Optional[str] = None) -> list[Activity]:
        return [a for a in self.activities
                if not a.completed
                and not a.excluded
                and not a.is_overdue
                and (kind is None or a.type == kind)]

    def pending_count(self, kind: Optional[str] = None) -> int:
        return len(self.pending(kind))

    @property
    def watchable_count(self) -> int:
        return self.uncompleted_count if self.available_count is None else self.available_count

    @property
    def upcoming_count(self) -> int:
        vods = [item for item in self.schedules
                if item.type == VOD and not item.completed]
        if vods:
            return sum(1 for item in vods if item.status == "upcoming")
        return max(0, self.uncompleted_count - self.watchable_count)

    @property
    def expired_vod_count(self) -> int:
        return sum(1 for item in self.schedules
                   if item.type == VOD and not item.completed
                   and item.status == "expired")

    def to_dict(self) -> dict:
        available_vod_urls = list(dict.fromkeys(
            item.url for item in self.schedules
            if item.type == VOD and item.status == "available"
            and not item.completed and item.url
        ))
        available_vod_cmids = []
        for url in available_vod_urls:
            try:
                cmid = parse_qs(urlparse(url).query).get("id", [""])[0]
            except Exception:
                cmid = ""
            if cmid and cmid not in available_vod_cmids:
                available_vod_cmids.append(cmid)
        return {
            "class_name": self.name,
            "url": self.url,
            "uncompleted_count": self.uncompleted_count,
            "uncompleted_weeks": self.uncompleted_weeks,
            "available_count": self.watchable_count,
            "available_weeks": (self.uncompleted_weeks
                                if self.available_weeks is None else self.available_weeks),
            "available_vod_urls": available_vod_urls,
            "available_vod_cmids": available_vod_cmids,
            "schedules": [item.__dict__.copy() for item in self.schedules],
            "syllabus_url": self.syllabus_url,
            "collection_errors": self.collection_errors,
        }
