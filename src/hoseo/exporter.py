from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import Course


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_csv_cell(value):
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _course_record(course: Course) -> dict:
    return {
        "course_id": course.course_id,
        "name": course.name,
        "professor": course.professor,
        "uncompleted_count": course.uncompleted_count,
        "available_count": course.watchable_count,
        "upcoming_count": course.upcoming_count,
        "expired_vod_count": course.expired_vod_count,
        "total_count": course.total_count,
        "uncompleted_weeks": course.uncompleted_weeks,
        "available_weeks": (course.uncompleted_weeks
                            if course.available_weeks is None else course.available_weeks),
        "partial": bool(course.collection_errors),
        "collection_errors": course.collection_errors,
        "syllabus_url": course.syllabus_url,
        "schedules": [{
            "type": item.type,
            "title": item.title,
            "status": item.status,
            "week": item.week,
            "starts_at": item.starts_at,
            "ends_at": item.ends_at,
            "completed": item.completed,
            "url": item.url,
        } for item in course.schedules],
        "activities": [{
            "type": activity.type,
            "title": activity.title,
            "status": activity.status,
            "score": activity.score,
            "deadline": activity.deadline,
            "completed": activity.completed,
            "url": activity.url,
        } for activity in course.activities],
    }


def _csv_rows(courses: list[Course]):
    for course in courses:
        base = {
            "course_id": course.course_id,
            "course_name": course.name,
            "professor": course.professor,
            "uncompleted_count": course.uncompleted_count,
            "available_count": course.watchable_count,
            "upcoming_count": course.upcoming_count,
            "expired_vod_count": course.expired_vod_count,
            "total_count": course.total_count,
            "uncompleted_weeks": ",".join(course.uncompleted_weeks),
            "available_weeks": ",".join(course.uncompleted_weeks
                                         if course.available_weeks is None
                                         else course.available_weeks),
            "partial": course.collection_errors != [],
            "collection_errors": " | ".join(course.collection_errors),
            "syllabus_url": course.syllabus_url,
        }
        if not course.activities and not course.schedules:
            yield {**base, "activity_type": "", "activity_title": "", "activity_status": "",
                   "score": "", "deadline": "", "completed": "", "activity_url": "",
                   "schedule_week": "", "schedule_start": "", "schedule_end": ""}
        for activity in course.activities:
            yield {
                **base,
                "activity_type": activity.type,
                "activity_title": activity.title,
                "activity_status": activity.status,
                "score": activity.score or "",
                "deadline": activity.deadline or "",
                "completed": activity.completed,
                "activity_url": activity.url,
                "schedule_week": "",
                "schedule_start": "",
                "schedule_end": "",
            }
        for item in course.schedules:
            yield {
                **base,
                "activity_type": item.type,
                "activity_title": item.title,
                "activity_status": item.status,
                "score": "",
                "deadline": item.ends_at or "",
                "completed": item.completed,
                "activity_url": item.url,
                "schedule_week": item.week,
                "schedule_start": item.starts_at or "",
                "schedule_end": item.ends_at or "",
            }


def export_courses(courses: list[Course], destination, output_format: str, overwrite=False) -> Path:
    path = Path(destination).expanduser().resolve()
    if path.exists() and not overwrite:
        raise FileExistsError(f"이미 존재하는 파일입니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8-sig" if output_format == "csv" else "utf-8",
                newline="", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp",
                delete=False) as stream:
            temp_path = Path(stream.name)
            if output_format == "json":
                json.dump({
                    "schema_version": 2,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "courses": [_course_record(course) for course in courses],
                }, stream, ensure_ascii=False, indent=2)
            elif output_format == "csv":
                rows = list(_csv_rows(courses))
                fields = [
                    "course_id", "course_name", "professor", "uncompleted_count",
                    "available_count", "upcoming_count", "expired_vod_count", "total_count",
                    "uncompleted_weeks", "available_weeks", "partial", "collection_errors",
                    "syllabus_url", "activity_type",
                    "activity_title", "activity_status", "score", "deadline", "completed",
                    "activity_url", "schedule_week", "schedule_start", "schedule_end",
                ]
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows({key: _safe_csv_cell(value) for key, value in row.items()}
                                 for row in rows)
            else:
                raise ValueError(f"지원하지 않는 출력 형식입니다: {output_format}")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        return path
    except Exception:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
