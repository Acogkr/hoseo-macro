from driver_utils import (
    set_verbose,
    set_log_callback,
    set_video_progress_callback,
    info,
    error,
    debug,
    random_sleep,
    human_like_delay,
    typing_delay,
    click_delay,
    random_mouse_movement,
    random_scroll,
    simulate_human_behavior,
    LMS_URL,
)

from auth import (
    init_driver,
    login,
)

from course_scanner import (
    get_course_list,
    get_uncompleted_lectures_by_week,
    scan_courses,
    get_active_weeks,
)

from video_watcher import (
    watch_lecture,
    process_course,
    process_course_with_recovery,
)
