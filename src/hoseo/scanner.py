from .scrape import courses as course_scraper


def scan_one(session, course, home=None):
    modules = None if home is None else home.get("modules")
    modules_known = bool(home and home.get("modules_known"))
    if modules_known and modules is not None and "vod" not in modules:
        course.uncompleted_count = 0
        course.available_count = 0
        course.total_count = 0
        course.uncompleted_weeks = []
        course.available_weeks = []
        course.schedules = [item for item in course.schedules if item.type != "vod"]
        return course
    if modules_known and home.get("active_weeks_known"):
        weeks = home.get("active_weeks", [])
    else:
        weeks = course_scraper.get_active_weeks(session, course.course_id)
    if not weeks:
        course.uncompleted_count = 0
        course.available_count = 0
        course.total_count = 0
        course.uncompleted_weeks = []
        course.available_weeks = []
        course.schedules = [item for item in course.schedules if item.type != "vod"]
        return course
    count, uncompleted_weeks, total, available, available_weeks, schedules = (
        course_scraper.collect_vod_progress(
            session, course.course_id, weeks,
            vod_windows=home.get("vod_windows", {}) if home else None))
    course.uncompleted_count = count
    course.available_count = available
    course.total_count = total
    course.uncompleted_weeks = uncompleted_weeks
    course.available_weeks = available_weeks
    course.schedules = [item for item in course.schedules if item.type != "vod"] + schedules
    return course
