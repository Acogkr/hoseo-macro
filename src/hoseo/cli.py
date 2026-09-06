from __future__ import annotations

import os
import sys
import logging
import threading
import argparse
import re
from dataclasses import dataclass
from importlib.metadata import version as package_version

os.environ.setdefault("WDM_LOG", "0")
for _n in ("WDM", "selenium", "urllib3"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

import time

import questionary
from questionary import Choice, Separator, Style
from prompt_toolkit import Application
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.input.typeahead import get_typeahead as _consume_typeahead
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text
from rich.padding import Padding
from rich.spinner import Spinner
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.markup import escape as _escape_markup

from datetime import datetime as _dt, timedelta as _timedelta

from . import config_manager, driver_utils, automation, exporter, http_client
from .models import Activity, Course, ScheduleItem, QUIZ, ASSIGN, VOD, ZOOM
from .http_client import AuthenticationExpiredError

BRAND_RED = "#A21D21"
RED = "#f85149"
GREEN = "#3fb950"
AMBER = "#d29922"
MUTED = "#8b8b8b"
console = Console(highlight=False)

STYLE = Style([
    ("qmark", f"fg:{RED} bold"),
    ("question", "bold"),
    ("pointer", f"fg:{RED} bold"),
    ("highlighted", f"fg:{RED} bold"),
    ("answer", f"fg:{RED} bold"),
    ("instruction", f"fg:{MUTED}"),
    ("disabled", f"fg:{MUTED} italic"),
    ("separator", f"fg:{MUTED}"),
])

OK = f"[bold {GREEN}]OK[/]"
CHK_ON = "■"
CHK_OFF = "□"

ACTION_WIDTH = 18
COURSE_VIDEO_WIDTH = 20
COURSE_QUIZ_WIDTH = 9
COURSE_ASSIGN_WIDTH = 9
COURSE_DEADLINE_WIDTH = 9
COURSE_GAP = "  "
COMPACT_WIDTH = 76
WATCH_STOP_TIMEOUT = 8.0
WATCH_FORCE_STOP_TIMEOUT = 2.0
_COURSE_SECTION_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\s+\((?P<section>\d{2})\)$")


@dataclass(frozen=True)
class _LoginContext:
    """Validated login choices and the HTTP-fetched dashboard snapshot."""

    courses: list[Course]
    user_id: str
    password: str
    headless: bool
    remember: bool

try:
    from wcwidth import wcswidth as _wcswidth
except ImportError:
    _wcswidth = None


def _w(s: str) -> int:
    # Terminal escape sequences are zero-width.  Measure the visible payload so
    # an injected style code cannot make otherwise identical LMS text truncate.
    s = _ANSI_RE.sub("", str(s))
    if _wcswidth:
        n = _wcswidth(s)
        return n if n >= 0 else len(s)
    return len(s)


_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def _clean_text(value) -> str:
    """Keep LMS-provided text from changing terminal layout or control state."""
    if value is None:
        return ""
    value = _ANSI_RE.sub("", str(value))
    return "".join(
        " " if ord(ch) < 32 or 127 <= ord(ch) < 160 else ch
        for ch in value
    ).strip()


def _prepare_course_display_names(courses) -> None:
    """Hide numeric section suffixes only when the base name is unambiguous."""

    parsed = []
    counts: dict[str, int] = {}
    for course in courses:
        original = _clean_text(course.name)
        match = _COURSE_SECTION_SUFFIX_RE.fullmatch(original)
        base = match.group("base").strip() if match else original
        key = re.sub(r"\s+", " ", base).casefold()
        parsed.append((course, original, base, bool(match)))
        counts[key] = counts.get(key, 0) + 1

    for course, original, base, has_section in parsed:
        key = re.sub(r"\s+", " ", base).casefold()
        course.display_name = (
            base if has_section and counts.get(key, 0) == 1 else original
        )


def _course_display_name(course: Course) -> str:
    return _clean_text(course.display_name or course.name)


def _pad(s: str, width: int) -> str:
    s = _clean_text(s)
    return s + " " * max(0, width - _w(s))


def _menu_row(label: str, detail: str = "", width: int = ACTION_WIDTH,
              terminal_width: int | None = None) -> str:
    """Align menu details without relying on font-dependent symbol widths."""
    label, detail = _clean_text(label), _clean_text(detail)
    if not detail:
        row = label
    elif _w(label) < width:
        row = f"{_pad(label, width)}{detail}"
    else:
        row = f"{label}  {detail}"
    limit = max(12, (terminal_width or console.width) - 4)
    if not detail:
        return _trunc_middle(label, limit)
    if _w(row) <= limit:
        return _trunc(row, limit)
    detail_width = min(_w(detail), max(8, limit // 2))
    label_width = max(4, limit - detail_width - 2)
    if _w(label) < label_width:
        label_width = _w(label)
        detail_width = max(4, limit - label_width - 2)
    return (f"{_pad(_trunc_middle(label, label_width), label_width)}  "
            f"{_trunc_middle(detail, detail_width)}")


def _menu_label_width(labels, minimum: int = 10, maximum: int = 20) -> int:
    """Choose one compact alignment column from the labels actually shown."""

    natural = max((_w(_clean_text(label)) for label in labels), default=minimum)
    return max(minimum, min(maximum, natural + 4))


def _fit_menu_text(value: str, terminal_width: int | None = None) -> str:
    """Guarantee that one selectable row stays inside the terminal viewport."""

    return _trunc(value, max(12, (terminal_width or console.width) - 4))


def _fit_display_widths(values, budget: int, minimums):
    """Shrink only fields that prevent a row from fitting its display budget."""
    widths = [value if isinstance(value, int) else _w(_clean_text(value))
              for value in values]
    floors = [min(width, minimum) for width, minimum in zip(widths, minimums)]
    while sum(widths) > max(0, budget):
        candidates = [index for index, width in enumerate(widths)
                      if width > floors[index]]
        if not candidates:
            break
        index = max(candidates, key=lambda item: widths[item])
        widths[index] -= 1
    return widths


def _compact_detail_row(leading: str, middle: str, trailing: str,
                        terminal_width: int) -> str:
    """Keep the identity and state of a menu row visible on narrow terminals."""
    limit = max(12, terminal_width - 4)
    leading = _clean_text(leading)
    middle = _clean_text(middle)
    trailing = _clean_text(trailing)
    fields = [field for field in (leading, middle, trailing) if field]
    whole = " · ".join(fields)
    if _w(whole) <= limit:
        return whole
    if not trailing:
        return _trunc_middle(leading, limit)

    if not middle:
        trailing_width = min(_w(trailing), max(4, limit - 9))
        leading_width = max(6, limit - trailing_width - 3)
        row = (f"{_trunc_middle(leading, leading_width)} · "
               f"{_trunc_middle(trailing, trailing_width)}")
        return _trunc(row, limit)
    trailing_width = min(_w(trailing), max(7, limit // 2))
    leading_width, middle_width = _fit_display_widths(
        (leading, middle), limit - 6 - trailing_width, (6, 4))
    row = (f"{_trunc_middle(leading, leading_width)} · "
           f"{_trunc_middle(middle, middle_width)} · "
           f"{_trunc_middle(trailing, trailing_width)}")
    return _trunc(row, limit)


def _bounded_display_lines(value: str, width: int, max_lines: int) -> str:
    """Wrap plain terminal text to a small, display-width-aware line budget."""

    remaining = _clean_text(value)
    width = max(1, int(width))
    max_lines = max(1, int(max_lines))
    lines: list[str] = []
    while remaining and len(lines) < max_lines:
        if _w(remaining) <= width:
            lines.append(remaining)
            break
        if len(lines) == max_lines - 1:
            lines.append(_trunc(remaining, width))
            break

        end = 0
        last_space = -1
        for index, character in enumerate(remaining):
            # Width can change at the grapheme-sequence level (for example,
            # U+2665 plus VS16 is two cells although each code point measured
            # alone sums to one).  Measure the whole candidate prefix.
            if _w(remaining[:index + 1]) > width:
                break
            end = index + 1
            if character.isspace():
                last_space = end
        if end <= 0:
            end = 1
        # Prefer a natural word boundary, but do not create a tiny first line
        # merely because the text contained an early space.
        if last_space >= max(1, end // 3):
            end = last_space
        line = remaining[:end].rstrip()
        lines.append(line or remaining[:end])
        remaining = remaining[end:].lstrip()
    return "\n".join(lines)


def _check_label(enabled: bool, label: str) -> str:
    return f"{CHK_ON if enabled else CHK_OFF} {label}"


def _bar_text(pct: int, width: int = 20) -> Text:
    pct = max(0, min(100, pct))
    fill = int(round(pct / 100 * width))
    bar = Text()
    bar.append("█" * fill, style=RED)
    bar.append("░" * (width - fill), style="dim")
    return bar


def _fmt_sec(sec: int) -> str:
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _fmt_dur(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_preview_duration(sec: int) -> str:
    if sec <= 0:
        return "미확인"
    hours, rem = divmod(int(sec), 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}시간 {minutes:02d}분"
    if minutes:
        return f"{minutes}분 {seconds:02d}초"
    return f"{seconds}초"


def _format_weeks(weeks) -> str:
    """Format week labels compactly without hiding long consecutive ranges."""
    values = []
    for raw in weeks or []:
        label = _clean_text(raw)
        number = label.removesuffix("주차").removesuffix("주").strip()
        try:
            values.append((int(number), None))
        except (TypeError, ValueError):
            if label and not label.endswith("주차"):
                label = f"{label}차" if label.endswith("주") else f"{label}주차"
            values.append((None, label))

    numeric = sorted({number for number, label in values if label is None})
    labels = []
    if numeric:
        start = previous = numeric[0]
        for number in numeric[1:] + [None]:
            if number is not None and number == previous + 1:
                previous = number
                continue
            labels.append(
                f"{start}주차" if start == previous else f"{start}~{previous}주차")
            if number is not None:
                start = previous = number
    labels.extend(label for _number, label in values if label)
    return ", ".join(dict.fromkeys(labels))


def banner(subtitle: str | None = None) -> None:
    title = Text()
    title.append(" HOSEO ", style=f"bold white on {BRAND_RED}")
    title.append("  LMS", style="bold")
    header = Table.grid(expand=True, padding=0)
    header.add_column(no_wrap=True)
    header.add_column(justify="right", no_wrap=True)
    header.add_row(
        title, Text(_clean_text(subtitle or "학습 대시보드"), style="dim"))
    console.print(Padding(header, (1, 2, 0, 2)))


def rule(title: str, subtitle: str | None = None) -> None:
    line_width = max(4, console.width - 4)
    console.print()
    console.print(Padding(Text(
        _trunc_middle(_clean_text(title or ""), line_width), style="bold"
    ), (0, 2)))
    if subtitle:
        console.print(Padding(Text(
            _trunc_middle(_clean_text(subtitle), line_width), style="dim"
        ), (0, 2)))


def _clear() -> None:
    if not sys.stdout.isatty():
        return
    # Clear only the visible viewport.  ``ESC[3J`` also erases scrollback,
    # which made an error or confirmation impossible to review after moving to
    # the next screen in Windows Terminal.
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def _bell() -> None:
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


def screen(title: str | None = None, subtitle: str | None = None, brand: bool = False, **_) -> None:
    _clear()
    if brand:
        banner(subtitle)
    else:
        rule(title, subtitle)
    console.print()


def info(markup: str, blank_after: bool = True) -> None:
    console.print()
    console.print(Padding(Text.from_markup(markup), (0, 2)))
    if blank_after:
        console.print()


def _todo_count(courses) -> int:
    """Count the same unfinished activities shown by the todo board."""

    return sum(
        1
        for course in courses
        for activity in course.activities
        if not activity.completed and not activity.excluded
    )


def _dashboard_overview(courses, refreshed_at) -> None:
    playable = sum(course.watchable_count for course in courses)
    unopened = sum(_this_week_unopened(course) for course in courses)
    pending = _todo_count(courses)
    partial = sum(bool(course.collection_errors) for course in courses)
    cards = (
        f"[bold {RED}]{playable}[/]\n[dim]지금 수강[/]",
        f"[bold]{unopened}[/]\n[dim]이번 주 열람 전[/]",
        f"[bold]{pending}[/]\n[dim]남은 할 일[/]",
        (f"[bold {AMBER}]{partial}[/]\n[dim]일부 확인[/]"
         if partial else f"[bold {GREEN}]정상[/]\n[dim]수집 상태[/]"),
    )
    grid = Table.grid(expand=True, padding=(0, 2))
    if console.width <= COMPACT_WIDTH:
        for _ in range(2):
            grid.add_column(justify="center", ratio=1)
        grid.add_row(*cards[:2])
        grid.add_row("", "")
        grid.add_row(*cards[2:])
    else:
        for _ in range(4):
            grid.add_column(justify="center", ratio=1)
        grid.add_row(*cards)
    stamp = refreshed_at.strftime("%H:%M:%S") if refreshed_at else "—"
    console.print(Padding(Panel(
        grid, title="오늘의 학습", subtitle=f"마지막 확인 {stamp}",
        border_style="grey37", box=box.ROUNDED, padding=(1, 0)), (0, 2)))


def _ask_question(question):
    """Run a Questionary prompt without leaking buffered keys to the next UI."""

    try:
        return question.ask()
    finally:
        try:
            _consume_typeahead(question.application.input)
        except Exception:
            pass


def pause() -> None:
    try:
        _ask_question(questionary.press_any_key_to_continue(
            "  계속하려면 아무 키나 누르세요...",
            style=STYLE,
            erase_when_done=True,
        ))
    except Exception:
        pass


def notice(title: str, message: str, subtitle: str | None = None) -> None:
    screen(title, subtitle)
    console.print(Padding(Text.from_markup(message), (0, 2)))
    console.print()
    pause()


class _Spin:
    def __init__(self, message: str):
        self._sp = Spinner("dots", text=Text(message), style=RED)
        self._live = Live(Padding(self._sp, (0, 0, 0, 2)), console=console,
                          refresh_per_second=12, transient=True)

    def update(self, message: str) -> None:
        self._sp.update(text=Text(message))

    def __enter__(self) -> "_Spin":
        self._live.start()
        return self

    def __exit__(self, *exc) -> bool:
        self._live.stop()
        return False


def spin(message: str) -> _Spin:
    return _Spin(message)


_PTSTYLE = PTStyle.from_dict({
    "": "#d7d7d7",
    "pointer": f"fg:{RED} bold",
    # The pointer is enough to show focus.  Bolding the complete row made the
    # label and its status read like one dense block in Windows Terminal.
    "hl": "#ffffff",
    "sep": f"fg:{MUTED}",
    "foot": f"fg:{MUTED}",
})


def _rows(choices):
    rows = []
    for c in choices:
        if isinstance(c, Separator):
            rows.append((_clean_text(getattr(c, "title", " ")), None, False))
        elif isinstance(c, Choice):
            rows.append((_clean_text(c.title), c.value, True))
        else:
            rows.append((_clean_text(c), c, True))
    return rows


def _run_select_application(app):
    """Run a menu and repair a first frame lost by Windows Terminal.

    Rich, Questionary and our prompt-toolkit menu share one ConPTY screen.
    Directly after an erase-on-finish prompt, Windows Terminal can occasionally
    drop prompt-toolkit's first paint.  The application then waits for input on
    an apparently empty screen because no later invalidation is scheduled.
    Resetting the renderer once, on its own event loop, forces a complete second
    paint without bringing back the persistent menu trails that
    ``erase_when_done=False`` caused.
    """

    if os.name != "nt" or not hasattr(app, "renderer"):
        return app.run()

    repaint_handle = None

    def pre_run() -> None:
        nonlocal repaint_handle
        import asyncio

        loop = asyncio.get_running_loop()

        def repaint() -> None:
            if not getattr(app, "is_running", False):
                return
            try:
                app.renderer.reset(
                    _scroll=True,
                    leave_alternate_screen=False,
                )
            except Exception:
                # Invalidation is still useful if a future prompt-toolkit
                # version changes Renderer.reset's private compatibility arg.
                pass
            try:
                app.invalidate()
            except RuntimeError:
                # The menu may have completed between the running-state check
                # and this callback.  A late repaint must never surface as an
                # event-loop error after a valid selection.
                pass

        repaint_handle = loop.call_later(0.15, repaint)

    try:
        return app.run(pre_run=pre_run)
    finally:
        if repaint_handle is not None:
            repaint_handle.cancel()


def select(message: str, choices: list, default=None, page: int = 0, escape: bool = True,
           multi: bool = False, shortcuts=None, hotkey_hint: str | None = None,
           multi_submit: str = "확인", back_label: str = "뒤로"):
    rows = _rows(choices)
    pickable = [i for i, r in enumerate(rows) if r[2]]
    if not pickable:
        return None
    cur = pickable[0]
    if default is not None:
        for i, r in enumerate(rows):
            if r[2] and r[1] == default:
                cur = i
                break
    chk = {i for i, c in enumerate(choices)
           if isinstance(c, Choice) and getattr(c, "checked", False)} if multi else set()
    st = {"cur": cur, "top": 0}
    if page <= 0:
        page = max(6, console.height - 13)
    win = min(page, len(rows))

    def render():
        n = len(rows)
        if st["cur"] < st["top"]:
            st["top"] = st["cur"]
        elif st["cur"] >= st["top"] + win:
            st["top"] = st["cur"] - win + 1
        st["top"] = max(0, min(st["top"], n - win))
        frags = []
        for i in range(st["top"], st["top"] + win):
            text, _v, pick = rows[i]
            mark = (f"{CHK_ON if i in chk else CHK_OFF} "
                    if (multi and pick) else "")
            text_width = max(4, console.width - 2 - _w(mark))
            text = (_trunc_middle(text, text_width) if pick
                    else _trunc(text, text_width))
            if i == st["cur"]:
                frags.append(("class:pointer", "> "))
                frags.append(("class:hl", mark + text + "\n"))
            elif pick:
                frags.append(("", "  " + mark + text + "\n"))
            else:
                frags.append(("class:sep", "  " + text + "\n"))
        pos = pickable.index(st["cur"]) + 1
        footer = _footer_text(
            message, multi=multi, escape=escape, hotkey_hint=hotkey_hint,
            pos=pos, total=len(pickable), selected=len(chk),
            has_above=st["top"] > 0, has_below=st["top"] + win < n,
            multi_submit=multi_submit,
            back_label=back_label,
        )
        frags.append(("class:foot", f"\n  {footer}"))
        return frags

    def move(d):
        i = pickable.index(st["cur"])
        st["cur"] = pickable[max(0, min(len(pickable) - 1, i + d))]

    kb = KeyBindings()
    kb.add("up")(lambda e: move(-1))
    kb.add("down")(lambda e: move(1))
    kb.add("pageup")(lambda e: move(-win))
    kb.add("pagedown")(lambda e: move(win))
    kb.add("home")(lambda e: st.update(cur=pickable[0]))
    kb.add("end")(lambda e: st.update(cur=pickable[-1]))
    def toggle():
        i = st["cur"]
        chk.discard(i) if i in chk else chk.add(i)

    if multi:
        kb.add("space")(lambda e: toggle())
        kb.add("enter")(lambda e: e.app.exit(result=[rows[i][1] for i in pickable if i in chk]))
    else:
        kb.add("enter")(lambda e: e.app.exit(result=rows[st["cur"]][1]))
    kb.add("c-c")(lambda e: e.app.exit(result=None))
    if escape:
        kb.add("escape")(lambda e: e.app.exit(result=None))
    for key, value in (shortcuts or {}).items():
        def choose(e, selected=value):
            e.app.exit(result=selected)
        kb.add(key)(choose)

    app = Application(
        layout=Layout(Window(FormattedTextControl(render, focusable=True),
                             always_hide_cursor=True, wrap_lines=False)),
        key_bindings=kb, style=_PTSTYLE, full_screen=False, mouse_support=False,
        # Menus are redrawn in place.  Leaving prompt_toolkit's rendered rows
        # behind makes every nested menu look as if it was appended to the
        # previous one, especially in Windows Terminal.
        erase_when_done=True,
    )
    try:
        return _run_select_application(app)
    except (KeyboardInterrupt, EOFError):
        return None
    finally:
        # prompt_toolkit intentionally carries unprocessed typeahead into the
        # next Application.  That is useful for REPLs, but in a menu workflow a
        # double Enter can otherwise confirm an unrelated next screen.
        try:
            _consume_typeahead(app.input)
        except Exception:
            pass


def _footer_text(message: str, *, multi: bool, escape: bool,
                 hotkey_hint: str | None, pos: int, total: int,
                 selected: int = 0, has_above: bool = False,
                 has_below: bool = False, terminal_width: int | None = None,
                 multi_submit: str = "확인", back_label: str = "뒤로") -> str:
    """Build a footer that keeps navigation and hidden-row hints visible."""
    count = f"{selected}/{total} 선택" if multi else f"{pos}/{total}"
    if has_above and has_below:
        more = "위/아래 더 있음"
    elif has_above:
        more = "위에 더 있음"
    elif has_below:
        more = "아래 더 있음"
    else:
        more = ""

    # Screen titles already identify the current menu.  Repeating ``message``
    # here made every footer use a slightly different grammar.
    del message
    verbose = (f"방향키 이동 · Space 선택/해제 · Enter {multi_submit}" if multi
               else "방향키 이동 · Enter 선택")
    compact = (f"이동 · Space 선택 · Enter {multi_submit}" if multi
               else "이동 · Enter 선택")
    essential = (f"Space 선택 · Enter {multi_submit}" if multi
                 else "Enter 선택")
    back_target = _clean_text(back_label) or "뒤로"
    back = f"Esc {back_target}" if escape else ""
    short_more = (
        "양쪽" if has_above and has_below
        else "위" if has_above
        else "아래" if has_below
        else ""
    )

    def joined(*parts):
        return " · ".join(part for part in parts if part)

    narrow = (
        joined("Space", f"Enter {multi_submit}", back, short_more)
        if multi else joined("Enter 선택", back, short_more)
    )
    short_hotkey = next(
        (part.strip() for part in reversed((hotkey_hint or "").split("|"))
         if "?" in part),
        "",
    )

    hotkeys = (hotkey_hint or "").replace(" | ", " · ")
    candidates = [
        joined(verbose, back, hotkeys, count, more),
        joined(compact, back, hotkeys, count, more),
        (joined(compact, back, short_hotkey, count, short_more)
         if short_hotkey else ""),
        (joined(essential, back, short_hotkey, count, short_more)
         if short_hotkey else ""),
        joined(compact, back, count, more),
        joined(essential, back, count, short_more),
        narrow,
        joined(essential, back),
        essential,
    ]
    limit = max(20, (terminal_width or console.width) - 2)
    for candidate in candidates:
        if candidate and _w(candidate) <= limit:
            return candidate
    return _trunc(candidates[-1], limit)


def _deadline_sort_key(iso: str | None) -> str:
    return iso or "~"


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = _dt.fromisoformat(iso)
        return (dt.date() - _dt.now(dt.tzinfo).date()).days
    except Exception:
        return None


def _nearest_deadline_days(course: Course) -> int | None:
    days = [_days_until(a.deadline) for a in course.pending() if a.deadline]
    future = [d for d in days if d is not None and d >= 0]
    return min(future) if future else None


def _dday_label(days: int | None) -> str:
    if days is None:
        return ""
    if days < 0:
        return "지남"
    if days == 0:
        return "오늘"
    if days == 1:
        return "내일"
    return f"D-{days}"


def _imminent_summary(courses: list[Course]) -> tuple[int, int, int]:
    today = tomorrow = week = 0
    for c in courses:
        for a in c.pending():
            d = _days_until(a.deadline)
            if d is None or d < 0:
                continue
            if d == 0:
                today += 1
            elif d == 1:
                tomorrow += 1
            elif d <= 7:
                week += 1
    return today, tomorrow, week


def _fmt_deadline(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = _dt.fromisoformat(iso)
        when = f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"
        return f"{_dday_label(_days_until(iso))} · {when}"
    except Exception:
        return "~" + iso[5:16].replace("T", " ")


def _activity_status_label(activity: Activity) -> str:
    if activity.completed:
        return "응시 완료" if activity.type == QUIZ else "제출 완료"
    if activity.is_overdue:
        return "기간 만료"
    return activity.status or ("미응시" if activity.type == QUIZ else "미제출")


def _assignment_exclusion_key(course: Course, activity: Activity) -> str:
    """Return a stable, non-personal key for one Moodle assignment."""

    if activity.type != ASSIGN:
        return ""
    course_id = _clean_text(course.course_id)
    cmid = _clean_text(activity.cmid)
    if not course_id or not cmid:
        return ""
    return f"{course_id}:{cmid}"


def _apply_assignment_exclusions(courses, user_id: str) -> None:
    """Apply local todo exclusions without changing LMS completion facts."""

    excluded = config_manager.load_assignment_exclusions(user_id)
    for course in courses:
        for activity in course.activities:
            key = _assignment_exclusion_key(course, activity)
            if key and activity.completed and key in excluded:
                # A successful LMS submission makes the local exception stale.
                # Remove it so a future LMS status correction cannot silently
                # re-exclude the assignment after a refresh.
                if config_manager.set_assignment_excluded(
                        user_id, key, False):
                    excluded.discard(key)
            activity.excluded = bool(
                key and not activity.completed and key in excluded)


def _set_assignment_excluded(course: Course, activity: Activity,
                             user_id: str, excluded: bool) -> bool:
    key = _assignment_exclusion_key(course, activity)
    if not key:
        return False
    if not config_manager.set_assignment_excluded(user_id, key, excluded):
        return False
    activity.excluded = bool(excluded and not activity.completed)
    return True


def _dispatch_activity(driver, wait, course: Course, activity,
                       uid: str, pw: str, headless: bool,
                       *, notice_title: str):
    """Open one activity consistently regardless of the originating screen."""

    del course, headless
    driver, wait, opened = _open_authenticated_url(
        driver, wait, activity.url, uid, pw)
    if opened:
        notice(
            notice_title,
            f"{OK} 로그인된 Chrome에서 열었습니다 · "
            f"{_escape_markup(_clean_text(activity.title))}",
        )
    else:
        notice(notice_title, f"[{RED}]링크를 열 수 없습니다.[/]")
    return driver, wait


def _collection_failed(course: Course, *keywords: str) -> bool:
    return any(any(keyword in error for keyword in keywords)
               for error in course.collection_errors)


def _parse_datetime(iso: str | None):
    if not iso:
        return None
    try:
        return _dt.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _week_bounds(now=None):
    current = now or _dt.now().astimezone()
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    start -= _timedelta(days=start.weekday())
    return start, start + _timedelta(days=7)


def _is_this_week(iso: str | None, now=None) -> bool:
    value = _parse_datetime(iso)
    if value is None:
        return False
    if now is None:
        current = _dt.now(value.tzinfo) if value.tzinfo else _dt.now().astimezone()
    else:
        current = now
    start, end = _week_bounds(current)
    if value.tzinfo is None and start.tzinfo is not None:
        value = value.replace(tzinfo=start.tzinfo)
    elif value.tzinfo is not None and start.tzinfo is None:
        start = start.replace(tzinfo=value.tzinfo)
        end = end.replace(tzinfo=value.tzinfo)
    return start <= value < end


def _this_week_unopened(course: Course, now=None) -> int:
    return sum(1 for item in course.schedules
               if item.type == VOD and item.status == "upcoming"
               and _is_this_week(item.starts_at, now=now))


def _fmt_calendar_time(iso: str | None) -> str:
    value = _parse_datetime(iso)
    if value is None:
        return "날짜 미정"
    weekdays = "월화수목금토일"
    return f"{value.month}/{value.day}({weekdays[value.weekday()]}) {value.hour:02d}:{value.minute:02d}"


def _vod_waiting_status(starts_at: str | None, now=None) -> str:
    if _is_this_week(starts_at, now=now):
        return "이번 주 대상 · 아직 열람 전"
    return "열람 예정"


def _enter_credentials(uid: str, pw: str):
    new_uid = _ask_question(questionary.text(
        "학번", default=uid, qmark=">", style=STYLE,
        erase_when_done=True,
    ))
    if new_uid is None:
        return uid, pw
    new_pw = _ask_question(questionary.password(
        "비밀번호", qmark=">", style=STYLE, erase_when_done=True,
    ))
    if new_pw is None:
        return uid, pw
    candidate_uid = new_uid.strip() or uid
    changed_account = bool(candidate_uid and candidate_uid != uid)
    uid = candidate_uid
    if new_pw:
        pw = new_pw
    elif changed_account:
        pw = ""
    return uid, pw


def _close_http_sessions(clients) -> None:
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


def _cached_http_sessions(uid: str):
    return http_client.sessions_from_cookie_sets(
        config_manager.load_session_cache(uid))


def _http_session_cache_enabled(uid: str) -> bool:
    """Use the encrypted cookie cache only for the remembered account."""

    config = config_manager.load_config()
    return bool(
        config.get("remember_me")
        and config.get("user_id")
        and config.get("user_id") == uid
    )


def _run_authenticated_sessions(uid: str, pw: str, action, *,
                                pooled: bool, cache_sessions: bool,
                                persist: bool = False,
                                on_reauthenticate=None):
    """Run one bounded action with a restored or freshly logged-in session.

    Session restoration, one-time expiry recovery, persistence, and cleanup
    are centralized here so login, refresh, and retry flows cannot drift while
    each caller still owns its progress UI.
    """

    clients = _cached_http_sessions(uid) if cache_sessions else []
    restored = bool(clients)

    def fresh_sessions():
        if pooled:
            return http_client.login_session_pool(uid, pw)
        return [http_client.login_session(uid, pw)]

    if not clients:
        clients = fresh_sessions()
    try:
        try:
            result = action(clients)
        except AuthenticationExpiredError:
            if not restored:
                raise
            _close_http_sessions(clients)
            clients = []
            config_manager.clear_session_cache()
            if on_reauthenticate is not None:
                on_reauthenticate()
            clients = fresh_sessions()
            result = action(clients)
        if persist and cache_sessions:
            config_manager.save_session_cache(
                uid, http_client.session_cookie_sets(clients))
        return result
    finally:
        _close_http_sessions(clients)


def _http_read(uid: str, pw: str, action):
    cache_sessions = _http_session_cache_enabled(uid)
    return _run_authenticated_sessions(
        uid,
        pw,
        lambda clients: action(clients[0]),
        pooled=False,
        cache_sessions=cache_sessions,
        persist=cache_sessions,
    )


def _browser_is_headless(driver) -> bool:
    marker = getattr(driver, "_hoseo_headless", None)
    if isinstance(marker, bool):
        return marker
    try:
        options = driver.capabilities.get("goog:chromeOptions", {})
        arguments = options.get("args", []) if isinstance(options, dict) else []
        return any(str(argument).startswith("--headless")
                   for argument in arguments)
    except Exception:
        return False


def _close_browser(driver) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


def _browser_session_active(driver) -> bool:
    """Verify that an existing Chrome session still reaches authenticated LMS."""

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    from .selectors import url as lms_url

    try:
        driver.get(lms_url("course_list"))
        WebDriverWait(driver, 10).until(
            lambda current: (
                "/login/" not in str(current.current_url or "")
                and bool(current.find_elements(
                    By.CSS_SELECTOR, "a[href*='/login/logout.php']"))
            )
        )
        return True
    except Exception:
        return False


def _ensure_browser(driver, wait, uid, pw, headless, *,
                    verify_existing: bool = False):
    if driver is not None:
        try:
            has_windows = bool(driver.window_handles)
            same_mode = _browser_is_headless(driver) == bool(headless)
            if has_windows and same_mode:
                if not verify_existing or _browser_session_active(driver):
                    return driver, wait
        except Exception:
            pass
        # A driver with no windows is already unusable.  Release it before a
        # replacement attempt so a failed Chrome lookup cannot orphan the old
        # chromedriver process.
        _close_browser(driver)
        driver = wait = None
    from .browser_locator import find_chrome

    if find_chrome() is None:
        notice("Chrome 필요",
               f"[{RED}]링크 열기와 자동수강에는 Google Chrome이 필요합니다.[/]",
               subtitle="목록 조회 기능은 Chrome 없이 계속 사용할 수 있습니다")
        return None, None

    clients = []
    new_driver = None
    try:
        from . import session as browser_session

        purpose = "자동수강용" if headless else "로그인된"
        with spin(f"{purpose} Chrome 준비 중..."):
            new_driver, new_wait = browser_session.init_driver(headless=headless)
            clients = _cached_http_sessions(uid)
            if not clients:
                clients = [http_client.login_session(uid, pw)]
            authenticated = browser_session.adopt_authenticated_session(
                new_driver, clients[0], verify=True)
            if not authenticated:
                authenticated = browser_session.login(new_driver, new_wait, uid, pw)
            if not authenticated:
                raise RuntimeError("Chrome 로그인에 실패했습니다.")
            return new_driver, new_wait
    except Exception as exc:
        if new_driver is not None:
            try:
                new_driver.quit()
            except Exception:
                pass
        notice("Chrome 준비 실패", f"[{RED}]{_escape_markup(_clean_text(exc))}[/]")
        return None, None
    finally:
        _close_http_sessions(clients)


def _open_authenticated_url(driver, wait, url, uid, pw):
    """Open one LMS URL in a visible browser carrying the CLI login session."""
    from .selectors import absolute_lms_url

    safe_url = absolute_lms_url(url)
    if not safe_url:
        return driver, wait, False
    driver, wait = _ensure_browser(driver, wait, uid, pw, headless=False)
    if driver is None:
        return driver, wait, False
    if automation.open_url(safe_url, driver=driver, headless=False):
        return driver, wait, True

    # The managed Chrome session may have expired while the CLI stayed open.
    # Recreate and authenticate it once; never loop indefinitely.
    _close_browser(driver)
    driver, wait = _ensure_browser(None, None, uid, pw, headless=False)
    if driver is None:
        return driver, wait, False
    return (driver, wait,
            automation.open_url(safe_url, driver=driver, headless=False))


def _scan(uid, pw, cache_sessions=True):
    _clear()
    banner()
    console.print()
    ok = False
    courses = []
    driver_utils.set_stdout(False)
    try:
        with spin("로그인 및 강의 분석 준비 중...") as st:
            def progress(cur, total, _m):
                st.update(f"강의 분석 중 {cur}/{total}")

            courses = _run_authenticated_sessions(
                uid,
                pw,
                lambda clients: automation.full_scan_sessions(
                    clients,
                    progress_callback=progress,
                    with_activities=True,
                ),
                pooled=True,
                cache_sessions=cache_sessions,
                persist=True,
                on_reauthenticate=lambda: st.update("로그인 세션 갱신 중..."),
            )
            ok = True
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        driver_utils.error(f"로그인 후 강의 분석에 실패했습니다: {exc}")
        ok = False
    finally:
        driver_utils.set_stdout(False)
    errors = driver_utils.flush_errors()
    if errors:
        for e in errors:
            _warning_line(e)
        console.print()
    if not ok:
        console.print(
            "  학번·비밀번호와 네트워크 상태를 확인한 뒤 다시 시도하세요.",
            style=AMBER,
        )
        console.print("  계속 실패하면 터미널에서 ‘hoseo-macro doctor’를 실행하세요.", style="dim")
        console.print()
        pause()
        return None
    if errors:
        pause()
    return courses


def _login():
    cfg = config_manager.load_config()
    if cfg.get("config_error"):
        error = _escape_markup(_clean_text(cfg["config_error"]))
        notice("로그인 설정", f"[{AMBER}]{error}[/] 다시 입력해 주세요.")
    uid = cfg.get("user_id") or ""
    pw = cfg.get("password") or ""
    remember = bool(cfg.get("remember_me"))
    show = False
    cursor = None

    while True:
        sub = f"학번 {uid}" if (uid and pw) else "로그인"
        screen(subtitle=sub, brand=True)
        ready = bool(uid and pw)
        choices = []
        if ready:
            choices.append(Choice("로그인 시작", value="start"))
            choices.append(Choice(f"다른 계정으로 로그인   현재 {uid}", value="cred"))
        else:
            choices.append(Choice("학번·비밀번호 입력", value="cred"))
        choices += [
            Separator(" "),
            Choice(_check_label(show, "자동수강 Chrome 창 표시"), value="t_show"),
            Choice(_check_label(remember, "로그인 정보 암호화 저장"), value="t_save"),
            Separator(" "),
            Choice("종료", value="quit"),
        ]
        sel = select("로그인", choices, default=cursor, escape=False)

        if sel in (None, "quit"):
            return None
        if sel == "cred":
            uid, pw = _enter_credentials(uid, pw)
            cursor = "start" if (uid and pw) else "cred"
            continue
        if sel == "t_show":
            show = not show
            cursor = "t_show"
            continue
        if sel == "t_save":
            remember = not remember
            cursor = "t_save"
            continue
        if sel == "start":
            if not config_manager.save_config(uid, pw, remember):
                notice("로그인 설정", f"[{AMBER}]로그인 정보를 저장하지 못했습니다. 로그인은 계속 진행합니다.[/]")
            courses = _scan(uid, pw, cache_sessions=remember)
            if courses is None:
                cursor = "cred"
                continue
            return _LoginContext(
                courses=courses,
                user_id=uid,
                password=pw,
                headless=not show,
                remember=remember,
            )


SPIN = "-\\|/"


def _run_watch(driver, wait, courses, uid, pw, headless, stop_event, title, total_sec: int = 0):
    screen(title, subtitle="재생이 끝나면 다음 영상으로 자동 이동합니다 · Ctrl+C 중단")
    states = {id(c): "pending" for c in courses}
    prog = {"cur": 0, "dur": 0, "title": ""}
    status = {"msg": ""}
    start = time.time()
    total_vods = sum(c.watchable_count for c in courses)
    acc = {
        "done_sec": 0, "known_sec": 0, "done_n": 0,
        "last_dur": 0, "last_cur": 0, "last_title": None, "last_marked": False,
    }

    def render():
        now = time.time()
        el = int(now - start)
        spin = SPIN[int(now * 10) % len(SPIN)]
        done_n = sum(1 for v in states.values() if v == "done")
        current_sec = (prog["cur"] if prog["dur"] > 0 and not acc["last_marked"] else 0)
        total_done_sec = acc["done_sec"] + current_sec
        el_str = f"{el//60}:{el%60:02d}"
        if total_sec > 0:
            pct = min(100, int(total_done_sec / total_sec * 100))
            remaining = max(0, total_sec - total_done_sec)
            eta_str = _dt.fromtimestamp(time.time() + remaining).strftime("%H:%M")
            tail = f"전체 {_fmt_dur(total_sec)} · 예상 종료 {eta_str}"
        else:
            pct = None
            tail = "전체 재생 시간 확인 중"

        panel_width = min(92, max(20, console.width - 4))
        compact = console.width <= COMPACT_WIDTH
        inner_width = max(12, panel_width - 6)
        metrics = Table.grid(expand=True, padding=(0, 1))
        if compact:
            metrics.add_column(ratio=1)
            metrics.add_column(ratio=1)
            metrics.add_row(
                Text(f"과목 {done_n}/{len(courses)}", style="bold"),
                Text(f"영상 {acc['done_n']}/{total_vods}", style="bold"),
            )
            metrics.add_row(
                Text(f"진행률 {pct}%" if pct is not None else "진행률 —",
                     style=RED if pct is not None else "dim"),
                Text(f"경과 {el_str}", style="dim"),
            )
        else:
            for _ in range(4):
                metrics.add_column(justify="center", ratio=1)
            metrics.add_row(
                Text(f"과목 {done_n}/{len(courses)}", style="bold"),
                Text(f"영상 {acc['done_n']}/{total_vods}", style="bold"),
                Text(f"진행률 {pct}%" if pct is not None else "진행률 —",
                     style=RED if pct is not None else "dim"),
                Text(f"경과 {el_str}", style="dim"),
            )

        progress = (_bar_text(pct, min(48, inner_width)) if pct is not None
                    else Text("전체 진행률 계산 중", style="dim"))
        rows = [metrics, progress,
                Text(_trunc(tail, inner_width), style="dim"), Text("")]
        active_courses = [c for c in courses if states[id(c)] == "watching"]
        other_courses = [c for c in courses if states[id(c)] != "watching"]
        ordered = active_courses + other_courses
        visible_limit = max(3, console.height - 16)
        visible = ordered[:visible_limit]
        for c in visible:
            s = states[id(c)]
            line = Text()
            if s == "done":
                state_label, style = "완료", "dim"
            elif s == "fail":
                state_label, style = "실패", RED
            elif s == "watching":
                state_label, style = f"{spin} 재생 중", RED
            else:
                state_label, style = "대기", "dim"
            line.append(_pad(state_label, 10), style=style)
            line.append(_trunc_middle(
                _course_display_name(c), max(4, inner_width - 10)),
                        style="bold" if s == "watching" else style)
            rows.append(line)
            if s == "watching":
                if prog["dur"] > 0:
                    video_pct = min(100, int(prog["cur"] / prog["dur"] * 100))
                    cur_t = _fmt_sec(int(prog["cur"]))
                    dur_t = _fmt_sec(int(prog["dur"]))
                    video_title = _trunc_middle(
                        prog["title"] or "재생 중", max(4, inner_width - 2))
                    rows.append(Text(f"  {video_title}", style="dim"))
                    video_tail = f"  {video_pct}% {cur_t}/{dur_t}"
                    bar_width = max(
                        4, min(20, inner_width - 2 - _w(video_tail)))
                    video_line = Text("  ")
                    video_line.append_text(_bar_text(video_pct, bar_width))
                    video_line.append(video_tail, style="dim")
                    rows.append(video_line)
                elif status["msg"]:
                    rows.append(Text(
                        "  " + _trunc(status["msg"], max(4, inner_width - 2)),
                        style="dim"))
        if len(ordered) > len(visible):
            rows.append(Text(
                f"그 외 {len(ordered) - len(visible)}개 과목", style="dim"))
        return Padding(Panel(
            Group(*rows), title="자동수강 진행", border_style="grey37",
            box=box.ROUNDED, padding=(1, 2), width=panel_width,
        ), (0, 2))

    def vid_cb(cur, dur, t):
        prog.update(cur=cur, dur=dur, title=t)
        if dur > 0:
            new_video = (acc["last_title"] != t
                         or (acc["last_dur"] > 0 and cur + 1 < acc["last_cur"]))
            if new_video:
                acc["known_sec"] += dur
                acc["last_marked"] = False
            acc["last_dur"] = dur
            acc["last_cur"] = cur
            acc["last_title"] = t
            if not acc["last_marked"] and cur >= dur - 1:
                acc["done_sec"] += dur
                acc["done_n"] += 1
                acc["last_marked"] = True
        elif acc["last_dur"] > 0:
            acc["last_dur"] = 0
            acc["last_cur"] = 0
            acc["last_title"] = None
            acc["last_marked"] = False

    def status_cb(msg: str):
        status["msg"] = msg

    driver_utils.set_video_progress_callback(vid_cb)
    driver_utils.set_status_callback(status_cb)
    prev_stdout = driver_utils.PRINT_STDOUT
    driver_utils.set_stdout(False)
    driver_utils.flush_errors()
    done = 0
    th = None
    res = None
    interrupted = False
    try:
        with Live(render(), console=console, refresh_per_second=12,
                  transient=False, vertical_overflow="ellipsis") as live:
            for c in courses:
                if stop_event.is_set():
                    break
                states[id(c)] = "watching"
                prog.update(cur=0, dur=0, title="")
                res = {}

                def run(course=c, sink=res):
                    try:
                        ok, d, w = automation.watch_course_vods(
                            driver, wait, course, stop_event, uid, pw, log_cb=None, headless=headless)
                        sink.update(ok=ok, d=d, w=w)
                    except Exception as exc:
                        message = (f"{_course_display_name(course)} 자동수강 중 "
                                   f"예상하지 못한 오류: {exc}")
                        driver_utils.error(message)
                        sink.update(ok=False, error=message)

                th = threading.Thread(target=run, daemon=True)
                th.start()
                while th.is_alive():
                    live.update(render())
                    time.sleep(0.08)
                driver = res.get("d", driver)
                wait = res.get("w", wait)
                ok = res.get("ok", False)
                states[id(c)] = "done" if ok else "fail"
                done += 1 if ok else 0
                live.update(render())
    except KeyboardInterrupt:
        interrupted = True
        stop_event.set()
    finally:
        forced_stop = False
        if interrupted and th is not None and th.is_alive():
            console.print(f"\n  [{AMBER}]중단하는 중...[/] [dim]진행 중인 영상을 정리합니다[/]")
            deadline = time.monotonic() + WATCH_STOP_TIMEOUT
            while th.is_alive() and time.monotonic() < deadline:
                try:
                    th.join(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
                except KeyboardInterrupt:
                    stop_event.set()
            if th.is_alive():
                forced_stop = True
                driver_utils.error(
                    "자동수강 작업이 제때 중단되지 않아 Chrome을 강제로 정리합니다. "
                    "상태 확인이 끝날 때까지 다시 시작하지 마세요."
                )
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception as exc:
                        driver_utils.error(f"Chrome 강제 종료 실패: {exc}")
                th.join(timeout=WATCH_FORCE_STOP_TIMEOUT)
                if th.is_alive():
                    driver_utils.error(
                        "백그라운드 작업 정리가 끝나지 않았습니다. "
                        "프로그램을 종료한 뒤 LMS 출석 상태를 직접 확인하세요."
                    )
                driver = wait = None
        if interrupted and res and not forced_stop:
            driver = res.get("d", driver)
            wait = res.get("w", wait)
        driver_utils.set_stdout(prev_stdout)
        driver_utils.set_video_progress_callback(None)
        driver_utils.set_status_callback(None)

    errs = driver_utils.flush_errors()
    failed_courses = [c for c in courses if states[id(c)] == "fail"]
    return driver, wait, failed_courses, done, errs


def _watch_list(driver, wait, courses, uid, pw, headless, stop_event, title, total_sec: int = 0):
    original = list(courses)
    targets = list(courses)
    verified_ids = set()
    max_rounds = 4
    rnd = 0
    all_errs = []
    while True:
        rnd += 1
        driver, wait, failed_courses, _done, errs = _run_watch(
            driver, wait, targets, uid, pw, headless, stop_event, title, total_sec)
        failed_ids = {id(course) for course in failed_courses}
        for course in targets:
            if id(course) in failed_ids:
                verified_ids.discard(id(course))
            elif not stop_event.is_set():
                verified_ids.add(id(course))
        all_errs += errs
        # A stopped or failed browser run may still have completed a video just
        # before it ended. Refresh once so the completion summary reflects LMS.
        all_errs += _rescan_after_watch(uid, pw, targets)
        remaining = [c for c in targets if c.watchable_count > 0]
        if stop_event.is_set() or _done == 0:
            break
        if not remaining or rnd >= max_rounds:
            break
        targets = remaining
        title = f"미완료 이어서 자동수강 · {len(remaining)}개 과목"
        screen(title)
        total_sec = _prefetch_total_sec_http(uid, pw, targets)
    _watch_done_screen(
        original, all_errs, stop_event, verified_ids=verified_ids)
    return driver, wait


def _watch_done_screen(courses, errs, stop_event, verified_ids=None) -> None:
    if verified_ids is None:
        completed_courses = [c for c in courses if c.watchable_count == 0]
    else:
        completed_courses = [c for c in courses if id(c) in verified_ids]
    completed_ids = {id(c) for c in completed_courses}
    failed_courses = [c for c in courses if id(c) not in completed_ids]
    done_n = len(completed_courses)
    failed = len(failed_courses)
    _bell()
    if stop_event.is_set():
        title = "자동수강 중단"
        subtitle = "사용자 요청으로 중단했습니다 · LMS에서 최종 상태를 확인하세요"
    elif failed:
        title = "자동수강 일부 미완료"
        subtitle = "완료하지 못한 과목이 있습니다 · 아래 내용을 확인하세요"
    else:
        title = "자동수강 완료"
        subtitle = "선택한 과목의 자동수강이 끝났습니다"
    screen(title, subtitle=subtitle)
    line = Text("  완료 ")
    line.append(str(done_n), style=GREEN)
    line.append(f" / {len(courses)} 과목")
    if failed:
        line.append("   ·   미완료 ")
        line.append(str(failed), style=AMBER)
    console.print(line)
    console.print()
    if failed_courses:
        console.print(Padding(Text("완료하지 못한 과목", style="bold"), (0, 2)))
        for course in failed_courses[:8]:
            console.print(Padding(
                Text(f"- {_course_display_name(course)}"), (0, 4)))
        if len(failed_courses) > 8:
            console.print(Padding(
                Text(f"그 외 {len(failed_courses) - 8}개", style="dim"), (0, 4)))
        console.print()
    if errs:
        for e in errs[:6]:
            warning = Text("주의  ", style=AMBER)
            warning.append(_clean_text(e))
            console.print(Padding(warning, (0, 2)))
        if len(errs) > 6:
            console.print(f"  [dim]그 외 {len(errs) - 6}건[/]")
        console.print()
    pause()


def _prefetch_total_sec_http(uid, pw, courses) -> int:
    try:
        with spin("재생 시간을 계산하는 중..."):
            return _http_read(
                uid, pw,
                lambda client: automation.prefetch_vod_durations(client, courses))
    except Exception:
        return 0


def _watch_preview(courses, total_sec: int) -> bool:
    total_vods = sum(course.watchable_count for course in courses)
    screen("자동수강 시작 전 확인",
           subtitle="재생 대상과 예상 시간을 확인하세요")
    panel_width = min(86, max(20, console.width - 4))
    compact = console.width <= COMPACT_WIDTH
    duration = _fmt_preview_duration(total_sec)
    rows = []
    for course in courses:
        weeks = (course.uncompleted_weeks if course.available_weeks is None
                 else course.available_weeks)
        formatted_weeks = _format_weeks(weeks)
        if formatted_weeks:
            week_text = formatted_weeks
        elif course.available_weeks == []:
            week_text = "주차 미확인"
        else:
            week_text = "현재 열림"
        rows.append((course, week_text))

    # Leave enough vertical room for the title, summary and confirmation menu.
    # Large selections stay readable instead of pushing the action below the
    # visible viewport.
    visible_limit = max(2, console.height - (18 if compact else 16))
    visible_rows = rows[:visible_limit]
    hidden_rows = max(0, len(rows) - len(visible_rows))

    if compact:
        metrics = Table.grid(expand=True)
        metrics.add_column(style="dim")
        metrics.add_column(justify="right")
        metrics.add_row("선택 과목", f"[bold]{len(courses)}개[/]")
        metrics.add_row("재생 영상", f"[bold {RED}]{total_vods}개[/]")
        metrics.add_row("예상 시간", f"[bold]{duration}[/]")

        table = Table.grid(expand=True, padding=(0, 0))
        table.add_column(overflow="ellipsis", no_wrap=True)
        table.add_row(Text("과목별 대상", style="dim"))
        for course, week_text in visible_rows:
            row = _compact_detail_row(
                _course_display_name(course), "",
                f"{course.watchable_count}개 · {week_text}",
                max(16, panel_width - 2),
            )
            table.add_row(Text(row))
    else:
        metrics = Table.grid(expand=True, padding=(0, 2))
        for _ in range(3):
            metrics.add_column(justify="center", ratio=1)
        metrics.add_row(
            f"[bold]{len(courses)}[/]\n[dim]선택 과목[/]",
            f"[bold {RED}]{total_vods}[/]\n[dim]재생 영상[/]",
            f"[bold]{duration}[/]\n[dim]예상 시간[/]",
        )

        table = Table(
            box=None, show_header=True, header_style="dim", expand=True,
            pad_edge=False, padding=(0, 2), collapse_padding=False,
        )
        table.add_column("과목", ratio=4, style="bold",
                         overflow="ellipsis", no_wrap=True)
        table.add_column("영상", width=6, justify="right", no_wrap=True)
        table.add_column("재생 주차", ratio=2, style="dim",
                         overflow="ellipsis", no_wrap=True)
        for course, week_text in visible_rows:
            table.add_row(
                Text(_course_display_name(course), style="bold"),
                Text(f"{course.watchable_count}개"),
                Text(_clean_text(week_text), style="dim"),
            )

    if hidden_rows:
        table.add_row(Text(f"그 외 {hidden_rows}개 과목", style="dim"))

    note = Text()
    note.append("재생 기준  ", style="bold")
    note.append("현재 수강 가능한 미완료 영상만", style=MUTED)
    content = Group(metrics, Text(""), table, Text(""), note)
    console.print(Padding(Panel(
        content, title="재생 대상", border_style="grey37",
        box=box.ROUNDED, padding=(1, 2), width=panel_width,
    ), (0, 2)))
    console.print()
    return select("시작 확인", [
        Choice("자동수강 시작", value="start"),
        Choice("취소", value="back"),
    ], back_label="이전으로") == "start"


def _rescan_after_watch(uid, pw, courses):
    try:
        with spin("수강 상태 갱신 중..."):
            _http_read(
                uid, pw,
                lambda client: automation.rescan_vods_session(client, courses))
    except AuthenticationExpiredError as exc:
        driver_utils.error(str(exc))
    except Exception as exc:
        driver_utils.error(f"수강 상태 갱신 실패: {exc}")
    return driver_utils.flush_errors()


def _watch_course(driver, wait, course, uid, pw, headless, stop_event):
    if course.watchable_count == 0:
        this_week = _this_week_unopened(course)
        if this_week:
            message = f"[dim]이번 주 대상 영상 {this_week}개가 아직 열리지 않았습니다.[/]"
        elif course.upcoming_count:
            message = f"[dim]아직 열리지 않은 영상 {course.upcoming_count}개가 있습니다.[/]"
        elif course.expired_vod_count:
            message = f"[dim]기간이 종료된 미수강 영상 {course.expired_vod_count}개가 있습니다.[/]"
        else:
            message = "[dim]현재 수강 가능한 미수강 동영상이 없습니다.[/]"
        notice(f"동영상 · {_course_display_name(course)}", message)
        return driver, wait
    total_sec = _prefetch_total_sec_http(uid, pw, [course])
    if not _watch_preview([course], total_sec):
        return driver, wait
    driver, wait = _ensure_browser(
        driver, wait, uid, pw, headless, verify_existing=True)
    if driver is None:
        return driver, wait
    title = f"동영상 자동수강 · {_course_display_name(course)}"
    # A force-stopped daemon worker can outlive this action briefly.  Give each
    # action its own cancellation event so a later run can never clear and
    # reactivate the old worker's stop signal.
    action_stop_event = threading.Event()
    driver, wait = _watch_list(
        driver, wait, [course], uid, pw, headless, action_stop_event,
        title, total_sec=total_sec)
    return driver, wait


def _sync_course_vods(course: Course, videos: list[dict]) -> None:
    """Refresh the playback allowlist without changing active-week totals."""
    existing_vod_urls = {
        item.url for item in course.schedules if item.type == VOD and item.url
    }
    observed_urls = set()
    refreshed_schedules = []
    available_weeks = []
    for video in videos:
        url = _clean_text(video.get("url", ""))
        if url:
            observed_urls.add(url)
        completed = bool(video.get("completed"))
        state = _clean_text(video.get("state") or
                            ("completed" if completed else "unknown"))
        week = _clean_text(video.get("week", ""))
        if completed:
            continue
        # Keep the active-week schedule bounded, but replace every already
        # tracked URL with its current state.  Otherwise an upcoming row that
        # becomes available survives beside a second, available copy.
        if state != "available" and url not in existing_vod_urls:
            continue
        if (state == "available" and week
                and week not in available_weeks):
            available_weeks.append(week)
        refreshed_schedules.append(ScheduleItem(
            VOD, _clean_text(video.get("name") or "이름 없는 영상"),
            url=url, week=week,
            starts_at=video.get("available_from"),
            ends_at=video.get("available_until"), status=state,
        ))
    # Scanner totals intentionally cover the LMS's active-week range. The
    # attendance detail covers the whole term, so only its fresh, playable
    # allowlist replaces stale playable rows here.
    preserved = [
        item for item in course.schedules
        if (item.type != VOD
            or (item.status != "available"
                and (not item.url or item.url not in observed_urls)))
    ]
    course.schedules = preserved + refreshed_schedules
    available_count = sum(
        item.status == "available" for item in refreshed_schedules)
    course.available_count = available_count
    course.available_weeks = available_weeks
    if available_count:
        course.uncompleted_count = max(
            course.uncompleted_count, available_count)
        course.total_count = max(course.total_count, course.uncompleted_count)
        for week in available_weeks:
            if week not in course.uncompleted_weeks:
                course.uncompleted_weeks.append(week)
    course.collection_errors[:] = [
        error for error in course.collection_errors
        if not any(word in error for word in ("동영상", "출석", "수강 상태"))
    ]


def _render_vod_snapshot(groups: list[tuple[str, list[dict], str]]) -> None:
    if not any(videos for _label, videos, _state in groups):
        console.print(Padding(Text("표시할 동영상이 없습니다.", style="dim"), (0, 2)))
        return
    panel_width = min(92, max(20, console.width - 4))
    inner_width = max(12, panel_width - 6)
    compact = console.width <= COMPACT_WIDTH
    total_videos = sum(len(videos) for _label, videos, _state in groups)
    line_cost = 2 if compact else 1
    video_limit = max(3, (console.height - 16) // line_cost)
    rendered_videos = 0
    content = Table.grid(expand=True, padding=0)
    content.add_column(overflow="ellipsis", no_wrap=True)
    first = True
    for label, videos, state in groups:
        if not videos or rendered_videos >= video_limit:
            continue
        visible_videos = videos[:max(0, video_limit - rendered_videos)]
        if not first:
            content.add_row(Text(""))
        first = False
        content.add_row(Text(f"{label}  {len(videos)}개", style="bold"))
        for video in visible_videos:
            week = _format_weeks([video.get("week")]) or "주차 미정"
            if state == "available":
                when = (_fmt_calendar_time(video.get("available_until")) + " 마감"
                        if video.get("available_until") else "")
            elif state == "upcoming":
                when = (_fmt_calendar_time(video.get("available_from")) + " 시작"
                        if video.get("available_from") else "")
            elif state == "expired":
                when = (_fmt_calendar_time(video.get("available_until"))
                        if video.get("available_until") else "")
            else:
                when = ""
            style = "dim" if state in ("expired", "completed") else ""
            name = video.get("name") or "이름 없는 영상"
            if compact:
                content.add_row(Text(_trunc_middle(name, inner_width), style=style))
                detail = week + (f" · {when}" if when else "")
                content.add_row(Text(
                    "  " + _trunc(detail, max(4, inner_width - 2)),
                    style="dim"))
            else:
                row = _compact_detail_row(
                    name, when, week, max(16, panel_width - 2))
                content.add_row(Text(row, style=style))
        rendered_videos += len(visible_videos)
    hidden_videos = total_videos - rendered_videos
    if hidden_videos:
        content.add_row(Text(""))
        content.add_row(Text(f"그 외 {hidden_videos}개 영상", style="dim"))
    console.print(Padding(Panel(
        content, title="동영상 현황", border_style="grey37",
        box=box.ROUNDED, padding=(1, 2), width=panel_width,
    ), (0, 2)))
    console.print()


def _vod_list(driver, wait, course, uid, pw, headless, stop_event):
    vids = None
    while True:
        if vids is None:
            screen(f"동영상 · {_course_display_name(course)}")
            load_error = None
            with spin("동영상 목록 불러오는 중..."):
                try:
                    vids = _http_read(
                        uid, pw,
                        lambda client: automation.fetch_vod_status_session(
                            client, course))
                    if not isinstance(vids, list):
                        load_error = "LMS 응답에서 동영상 목록을 확인하지 못했습니다."
                    else:
                        _sync_course_vods(course, vids)
                except Exception as exc:
                    load_error = str(exc)
            if load_error is not None:
                screen(f"동영상 · {_course_display_name(course)}",
                       subtitle="동영상 목록을 불러오지 못했습니다")
                console.print(Padding(Text(
                    "네트워크 또는 로그인 세션을 확인할 수 없습니다.",
                    style=RED), (0, 2)))
                console.print(Padding(Text(
                    f"원인: {_clean_text(load_error)}"), (0, 2)))
                console.print()
                action = select(
                    "동영상", [Choice("다시 시도", value="retry")],
                    back_label="과목 메뉴로",
                )
                if action == "retry":
                    vids = None
                    continue
                return driver, wait
        available = [v for v in vids if not v.get("completed")
                     and v.get("state") == "available"]
        upcoming = [v for v in vids if not v.get("completed")
                    and v.get("state") == "upcoming"]
        expired = [v for v in vids if not v.get("completed")
                   and v.get("state") == "expired"]
        unknown = [v for v in vids if not v.get("completed")
                   and v.get("state") not in ("available", "upcoming", "expired")]
        done = [v for v in vids if v.get("completed")]
        if not vids:
            sub = "표시할 동영상이 없습니다"
        elif console.width <= COMPACT_WIDTH:
            sub = (f"전체 {len(vids)}개 · 수강 가능 {len(available)}개"
                   + (f" · 확인 필요 {len(unknown)}개" if unknown else ""))
        else:
            sub = (f"전체 {len(vids)}개 · 수강 가능 {len(available)}개"
                   f" · 열람 전 {len(upcoming)}개 · 기간 종료 {len(expired)}개"
                   f" · 상태 미확인 {len(unknown)}개 · 완료 {len(done)}개")
        screen(f"동영상 · {_course_display_name(course)}", subtitle=sub)
        _render_vod_snapshot([
            ("지금 수강 가능", available, "available"),
            ("아직 열리지 않음", upcoming, "upcoming"),
            ("상태 미확인", unknown, "unknown"),
            ("기간 종료", expired, "expired"),
            ("수강 완료", done, "completed"),
        ])
        if not available:
            pause()
            return driver, wait
        action_width = _menu_label_width(["자동수강 시작"])
        choices = [Choice(
            _menu_row(
                "자동수강 시작", f"영상 {len(available)}개",
                width=action_width,
            ),
            value="watch",
        )]
        sel = select("동영상", choices, back_label="과목 메뉴로")
        if sel in (None, "back"):
            return driver, wait
        if sel == "watch":
            driver, wait = _watch_course(driver, wait, course, uid, pw, headless, stop_event)
            vids = None


def _pick_courses(courses):
    while True:
        screen("전체 과목 자동수강", subtitle="수강할 과목을 선택하세요")
        choices = [Choice(
            _compact_detail_row(
                _course_display_name(c), "",
                f"수강 가능 {c.watchable_count}개", console.width),
            value=c, checked=True) for c in courses]
        picked = select(
            "과목 선택", choices, multi=True, multi_submit="다음",
            back_label="메인으로")
        if picked is None:
            return []
        if picked:
            return picked
        notice("과목 선택", f"[{AMBER}]자동수강할 과목을 한 개 이상 선택하세요.[/]")


def _watch_all(driver, wait, courses, uid, pw, headless, stop_event):
    targets = [c for c in courses if c.watchable_count > 0]
    if not targets:
        notice("전체 과목 자동수강",
               "[dim]열려 있는 주차에 미수강 동영상이 있는 과목이 없습니다.[/]",
               subtitle="지금 수강 가능한 동영상이 없습니다")
        return driver, wait
    chosen = _pick_courses(targets)
    if not chosen:
        return driver, wait
    total_sec = _prefetch_total_sec_http(uid, pw, chosen)
    if not _watch_preview(chosen, total_sec):
        return driver, wait
    driver, wait = _ensure_browser(
        driver, wait, uid, pw, headless, verify_existing=True)
    if driver is None:
        return driver, wait
    title = f"동영상 자동수강 · {len(chosen)}개 과목"
    action_stop_event = threading.Event()
    driver, wait = _watch_list(
        driver, wait, chosen, uid, pw, headless, action_stop_event,
        title, total_sec=total_sec)
    return driver, wait


def _activity_loop(driver, wait, course: Course, kind: str,
                   uid: str, pw: str, headless: bool):
    label = "퀴즈" if kind == QUIZ else "과제"
    while True:
        items = [a for a in course.activities if a.type == kind]
        load_failed = _collection_failed(course, label, "활동 정보")
        todo_a = sorted([a for a in items
                         if not a.completed and not a.excluded and not a.is_overdue],
                        key=lambda a: _deadline_sort_key(a.deadline))
        excluded_a = sorted([a for a in items if not a.completed and a.excluded],
                            key=lambda a: _deadline_sort_key(a.deadline))
        done_a = sorted([a for a in items
                         if a.completed or (a.is_overdue and not a.excluded)],
                        key=lambda a: _deadline_sort_key(a.deadline))
        sub = (f"진행 중 {len(todo_a)}개"
               + (f" · 할 일 제외 {len(excluded_a)}개" if excluded_a else "")
               + f" · 완료·종료 {len(done_a)}개"
               if items else (f"{label} 정보 확인 필요" if load_failed
                              else f"등록된 {label}가 없습니다"))
        screen(f"{label} · {_course_display_name(course)}", subtitle=sub)
        if not items:
            console.print(Padding(
                Text((f"{label} 목록을 불러오지 못했습니다. 다시 확인해 주세요."
                      if load_failed else f"표시할 {label} 항목이 없습니다."),
                     style=AMBER if load_failed else "dim"), (0, 2)))
            console.print()
            pause()
            return driver, wait
        def _activity_tail(a: Activity) -> str:
            tail = _activity_status_label(a)
            if a.excluded and not a.completed:
                tail += " · 할 일 제외"
            elif a.completed:
                if a.score:
                    tail += f"   점수 {a.score}"
            else:
                deadline = _fmt_deadline(a.deadline)
                tail += f"   {deadline}" if deadline else ""
            return tail

        natural_title = max((_w(a.title) for a in items), default=0) + 2
        tail_width = max((_w(_activity_tail(a)) for a in items), default=0)
        titlew = min(natural_title,
                     max(8, console.width - 4 - tail_width))

        def _activity_choice(a: Activity) -> Choice:
            status = _activity_status_label(a)
            if console.width <= COMPACT_WIDTH:
                detail = (f"점수 {a.score}" if a.completed and a.score
                          else _fmt_deadline(a.deadline))
                return Choice(_compact_detail_row(
                    a.title, detail, status, console.width), value=a)
            head = _pad(_trunc_middle(a.title, max(1, titlew - 2)), titlew)
            return Choice(
                _fit_menu_text(f"{head}{_activity_tail(a)}"), value=a)

        choices = []
        if todo_a:
            choices.append(Separator("진행 중"))
            choices += [_activity_choice(a) for a in todo_a]
        if excluded_a:
            if choices:
                choices.append(Separator(" "))
            choices.append(Separator("남은 할 일에서 제외됨"))
            choices += [_activity_choice(a) for a in excluded_a]
        if done_a:
            if choices:
                choices.append(Separator(" "))
            choices.append(Separator("완료·종료"))
            choices += [_activity_choice(a) for a in done_a]
        sel = select("항목 선택", choices, back_label="과목 메뉴로")
        if sel in (None, "back"):
            return driver, wait
        driver, wait = _dispatch_activity(
            driver, wait, course, sel, uid, pw, headless,
            notice_title=f"{label} · {_course_display_name(course)}",
        )


def _tracked_incomplete_count(course: Course, kind: str) -> int:
    """Count LMS-incomplete items still shown in this user's todo workflow."""

    return sum(
        1 for activity in course.activities
        if activity.type == kind and not activity.completed
        and not activity.excluded
    )


def _course_loop(driver, wait, course: Course, uid, pw, headless, stop_event):
    if not course.enriched:
        screen(_course_display_name(course))
        with spin("퀴즈·과제 불러오는 중..."):
            try:
                _http_read(
                    uid, pw,
                    lambda client: automation.enrich_session(client, course))
            except AuthenticationExpiredError as exc:
                course.collection_errors.append(str(exc))
                notice("세션 만료", f"[{RED}]로그인 세션이 만료되었습니다. 새로고침하거나 다시 실행하세요.[/]")
                return driver, wait
            except Exception as exc:
                course.collection_errors.append(f"활동 정보 수집 실패: {exc}")
            _apply_assignment_exclusions([course], uid)
    while True:
        nq = sum(1 for a in course.activities if a.type == QUIZ)
        na = sum(1 for a in course.activities if a.type == ASSIGN)
        v = course.uncompleted_count
        q = _tracked_incomplete_count(course, QUIZ)
        a = _tracked_incomplete_count(course, ASSIGN)
        excluded_assignments = sum(
            1 for item in course.activities
            if item.type == ASSIGN and item.excluded and not item.completed)
        vod_unknown = _collection_failed(course, "출석", "동영상", "수강 상태")
        activity_unknown = _collection_failed(course, "활동 정보")
        quiz_unknown = activity_unknown or _collection_failed(course, "퀴즈")
        assign_unknown = activity_unknown or _collection_failed(course, "과제")
        has_vod = bool(
            course.total_count
            or course.uncompleted_count
            or any(item.type == VOD for item in course.schedules)
        )
        if course.collection_errors:
            sub = "일부 정보 확인 필요"
        elif v == 0 and q == 0 and a == 0:
            if excluded_assignments:
                sub = f"LMS 미제출 {excluded_assignments}건 · 남은 할 일에서 제외됨"
            else:
                sub = ("모두 완료" if (course.total_count or nq or na)
                       else "등록된 학습 항목 없음")
        else:
            summary = []
            if has_vod or vod_unknown:
                summary.append(
                    "동영상 미확인" if vod_unknown else
                    ("동영상 완료" if v == 0 else f"동영상 {v}개 남음")
                )
            if nq or quiz_unknown:
                summary.append(
                    "퀴즈 미확인" if quiz_unknown else
                    ("퀴즈 완료" if q == 0 else f"퀴즈 {q}개 남음")
                )
            if na or assign_unknown:
                summary.append(
                    "과제 미확인" if assign_unknown else
                    ("과제 완료" if a == 0 else f"과제 {a}개 남음")
                )
            sub = " · ".join(summary) or "등록된 학습 항목 없음"
        screen(_course_display_name(course), subtitle=sub)
        vtail = ("정보 미확인" if vod_unknown else
                 ("완료" if v == 0 and course.total_count else
                  ("항목 없음" if v == 0 else _vod_text(course))))
        qtail = ("정보 미확인" if quiz_unknown else
                 ("항목 없음" if nq == 0 else
                  ("완료" if q == 0 else f"미완료 {q}/{nq}")))
        atail = ("정보 미확인" if assign_unknown else
                 ("항목 없음" if na == 0 else
                  (f"할 일 제외 {excluded_assignments}/{na}"
                   if a == 0 and excluded_assignments else
                   ("완료" if a == 0 else f"미완료 {a}/{na}"))))
        learning_labels = [
            label for label, visible in (
                ("동영상", has_vod or vod_unknown),
                ("퀴즈", nq or quiz_unknown),
                ("과제", na or assign_unknown),
            ) if visible
        ]
        label_width = _menu_label_width(learning_labels, maximum=14)
        choices = [Separator("학습")]
        if has_vod or vod_unknown:
            choices.append(Choice(
                _menu_row("동영상", vtail, width=label_width), value="vod"))
        if nq or quiz_unknown:
            choices.append(Choice(
                _menu_row("퀴즈", qtail, width=label_width), value="quiz"))
        if na or assign_unknown:
            choices.append(Choice(
                _menu_row("과제", atail, width=label_width), value="assign"))
        if len(choices) == 1:
            choices.append(Separator("진행할 학습 항목 없음"))
        choices.append(Separator(" "))
        if course.syllabus_url or _graded_activities(course):
            choices.append(Choice("성적·강의계획서", value="grade"))
        choices += [
            Choice("LMS 강의실 열기", value="course_home"),
        ]
        sel = select("과목 메뉴", choices, back_label="메인으로")
        if sel in (None, "back"):
            return driver, wait
        if sel == "vod":
            driver, wait = _vod_list(driver, wait, course, uid, pw, headless, stop_event)
        elif sel == "course_home":
            from .selectors import url as lms_url

            driver, wait, opened = _open_authenticated_url(
                driver,
                wait,
                lms_url("course_home", course_id=course.course_id),
                uid,
                pw,
            )
            if opened:
                notice(
                    _course_display_name(course),
                    f"{OK} 로그인된 Chrome에서 LMS 강의실을 열었습니다.",
                )
            else:
                notice(
                    _course_display_name(course),
                    f"[{RED}]LMS 강의실을 열지 못했습니다.[/]",
                )
        elif sel == "grade":
            driver, wait = _grade_course_loop(
                driver, wait, course, uid, pw, headless)
        else:
            driver, wait = _activity_loop(
                driver, wait, course,
                QUIZ if sel == "quiz" else ASSIGN, uid, pw, headless)


def _excluded_assignment_count(course: Course) -> int:
    return sum(
        1 for activity in course.activities
        if activity.type == ASSIGN and activity.excluded
        and not activity.completed
    )


def _course_done(c: Course) -> bool:
    if c.collection_errors:
        return False
    if c.enriched:
        return (c.uncompleted_count == 0
                and not any(
                    activity.type in (QUIZ, ASSIGN)
                    and not activity.completed
                    for activity in c.activities
                ))
    return c.uncompleted_count == 0


def _vod_text(c: Course) -> str:
    if c.total_count:
        text = f"미수강 {c.uncompleted_count}/{c.total_count}"
    else:
        text = f"미수강 {c.uncompleted_count}"
    this_week = _this_week_unopened(c)
    if this_week:
        text += f" · 이번 주 열람 전 {this_week}개"
    elif c.upcoming_count:
        text += (f" · 지금 수강 {c.watchable_count}개"
                 f" · 열람 예정 {c.upcoming_count}개")
    elif c.expired_vod_count:
        text += f" · 기간 종료 {c.expired_vod_count}개"
    return text


def _course_name_width(courses, terminal_width: int | None = None) -> int:
    longest_name = max(
        (_w(_course_display_name(course)) for course in courses),
        default=12,
    )
    # Every dashboard section shares one first-column width.  Reserving the
    # longest section heading here prevents one section from switching to a
    # compact layout while another still renders as a table.
    longest_heading = _w(f"수강 중 과목 ({len(courses)})")
    natural = max(longest_name, longest_heading) + 2
    if terminal_width is not None and natural > max(12, terminal_width - 4):
        fixed = (COURSE_VIDEO_WIDTH + COURSE_QUIZ_WIDTH + COURSE_ASSIGN_WIDTH
                 + COURSE_DEADLINE_WIDTH + _w(COURSE_GAP) * 4)
        natural = min(natural, max(12, terminal_width - 4 - fixed))
    return max(12, natural)


def _course_table_is_wide(namew: int, terminal_width: int) -> bool:
    fixed = (COURSE_VIDEO_WIDTH + COURSE_QUIZ_WIDTH + COURSE_ASSIGN_WIDTH
             + COURSE_DEADLINE_WIDTH + _w(COURSE_GAP) * 4)
    return namew + fixed <= max(12, terminal_width - 4)


def _course_vod_status(course: Course) -> str:
    if course.uncompleted_count == 0:
        return "모두 수강"
    this_week = _this_week_unopened(course)
    if course.watchable_count and this_week:
        return f"수강 {course.watchable_count} / 열람 전 {this_week}"
    if course.watchable_count:
        return f"지금 수강 {course.watchable_count}개"
    if this_week:
        return f"이번 주 열람 전 {this_week}개"
    if course.upcoming_count:
        return f"열람 예정 {course.upcoming_count}개"
    if course.expired_vod_count:
        return f"기간 종료 {course.expired_vod_count}개"
    return f"미수강 {course.uncompleted_count}개"


def _course_deadline_status(course: Course) -> str:
    days = _nearest_deadline_days(course)
    if days is None:
        return "-"
    if days == 0:
        return "오늘"
    if days == 1:
        return "내일"
    return f"{days}일 후"


def _course_header(namew: int, terminal_width: int | None = None,
                   first_column: str = "과목") -> str:
    width = terminal_width or console.width
    if not _course_table_is_wide(namew, width):
        return "과목 · 학습 상태"
    return COURSE_GAP.join((
        _pad(_trunc(first_column, namew), namew),
        _pad("동영상", COURSE_VIDEO_WIDTH),
        _pad("남은 퀴즈", COURSE_QUIZ_WIDTH),
        _pad("남은 과제", COURSE_ASSIGN_WIDTH),
        "다음 마감",
    ))


def _course_label(c: Course, namew: int,
                  terminal_width: int | None = None) -> str:
    width = terminal_width or console.width
    if not _course_table_is_wide(namew, width):
        if c.collection_errors:
            status = "정보 확인 필요"
        else:
            status = _course_vod_status(c)
            if c.enriched:
                pending = (_tracked_incomplete_count(c, QUIZ)
                           + _tracked_incomplete_count(c, ASSIGN))
                if pending:
                    status += f" · 할 일 {pending}개"
                excluded = _excluded_assignment_count(c)
                if excluded:
                    status += f" · 제외 {excluded}개"
                deadline = _course_deadline_status(c)
                if deadline != "-":
                    status += f" · {deadline}"
        return _compact_detail_row(
            _course_display_name(c), "", status, width)
    namew = max(12, namew)
    if c.collection_errors:
        video, quiz, assign, deadline = "정보 확인 필요", "-", "-", "-"
    else:
        video = _course_vod_status(c)
        if c.enriched:
            quiz_n = _tracked_incomplete_count(c, QUIZ)
            assign_n = _tracked_incomplete_count(c, ASSIGN)
            excluded_n = _excluded_assignment_count(c)
            quiz = f"{quiz_n}개" if quiz_n else "없음"
            if excluded_n:
                assign = (f"{assign_n}개·제외{excluded_n}"
                          if assign_n else f"제외 {excluded_n}개")
            else:
                assign = f"{assign_n}개" if assign_n else "없음"
            deadline = _course_deadline_status(c)
        else:
            quiz = assign = "미확인"
            deadline = "-"
    return COURSE_GAP.join((
        _pad(_trunc_middle(_course_display_name(c), namew), namew),
        _pad(_trunc(video, COURSE_VIDEO_WIDTH), COURSE_VIDEO_WIDTH),
        _pad(_trunc(quiz, COURSE_QUIZ_WIDTH), COURSE_QUIZ_WIDTH),
        _pad(_trunc(assign, COURSE_ASSIGN_WIDTH), COURSE_ASSIGN_WIDTH),
        _trunc(deadline, COURSE_DEADLINE_WIDTH),
    ))


def _course_section(title: str, courses, namew: int) -> list:
    width = console.width
    heading = f"{title} ({len(courses)})"
    section_namew = max(namew, _w(heading) + 2)
    if _course_table_is_wide(section_namew, width):
        heading = _course_header(
            section_namew,
            terminal_width=width,
            first_column=heading,
        )
    rows = [Separator(heading)]
    rows.extend(
        Choice(
            _course_label(course, section_namew, terminal_width=width),
            value=course,
        )
        for course in courses
    )
    return rows


def _trunc(s: str, width: int) -> str:
    s = _clean_text(s)
    if width <= 0:
        return ""
    if _w(s) <= width:
        return s
    suffix = "..."
    if width <= len(suffix):
        return suffix[:width]
    out = ""
    for ch in s:
        if _w(out + ch) > width - len(suffix):
            break
        out += ch
    return out + suffix


def _trunc_middle(s: str, width: int) -> str:
    """Keep both the meaning prefix and numeric/status suffix visible."""
    s = _clean_text(s)
    if _w(s) <= width:
        return s
    if width <= 3:
        return "." * max(0, width)
    budget = width - 3
    left_target = (budget + 1) // 2
    left = ""
    for char in s:
        if _w(left + char) > left_target:
            break
        left += char
    right_target = max(0, budget - _w(left))
    right = ""
    for char in reversed(s):
        if _w(char + right) > right_target:
            break
        right = char + right
    return left + "..." + right


def _todo_split(courses: list[Course]):
    active, expired = [], []
    for c in courses:
        for a in c.activities:
            if a.completed or a.excluded:
                continue
            (expired if a.is_overdue else active).append((c, a))
    active.sort(key=lambda ca: _deadline_sort_key(ca[1].deadline))
    expired.sort(key=lambda ca: _deadline_sort_key(ca[1].deadline))
    return active, expired


def _excluded_assignment_items(courses: list[Course]):
    items = [
        (course, activity)
        for course in courses
        for activity in course.activities
        if (activity.type == ASSIGN and activity.excluded
            and not activity.completed)
    ]
    return sorted(items, key=lambda ca: _deadline_sort_key(ca[1].deadline))


def _trackable_assignment_items(courses: list[Course]):
    items = [
        (course, activity)
        for course in courses
        for activity in course.activities
        if activity.type == ASSIGN and not activity.completed
    ]
    return sorted(items, key=lambda ca: _deadline_sort_key(ca[1].deadline))


def _manage_assignment_tracking(courses: list[Course], user_id: str) -> None:
    """Choose in one screen which LMS-incomplete assignments remain visible."""

    items = _trackable_assignment_items(courses)
    excluded_count = sum(activity.excluded for _course, activity in items)
    screen(
        "과제 표시 설정",
        subtitle=(f"전체 {len(items)}건 · 표시 {len(items) - excluded_count}건 · "
                  f"제외 {excluded_count}건"
                  if items else "설정할 미제출 과제가 없습니다"),
    )
    if not items:
        console.print(Padding(
            Text("현재 설정할 수 있는 미제출 과제가 없습니다.", style="dim"),
            (0, 2),
        ))
        console.print()
        pause()
        return

    choices = [Choice(
        _compact_detail_row(
            _course_display_name(course), activity.title,
            _fmt_deadline(activity.deadline), console.width),
        value=(course, activity),
        checked=not activity.excluded,
    ) for course, activity in items]
    selected = select(
        "표시할 과제", choices, multi=True, multi_submit="저장",
        back_label="할 일로",
    )
    if selected is None:
        return

    visible = {id(activity) for _course, activity in selected}
    for course, activity in items:
        should_exclude = id(activity) not in visible
        if activity.excluded == should_exclude:
            continue
        if not _set_assignment_excluded(
                course, activity, user_id, should_exclude):
            _apply_assignment_exclusions(courses, user_id)
            notice(
                "과제 표시 설정",
                f"[{RED}]설정을 저장하지 못했습니다.[/]",
                subtitle="설정 파일 권한을 확인한 뒤 다시 시도하세요",
            )
            return


def _todo_board(driver, wait, courses, uid, pw, headless):
    while True:
        active, expired = _todo_split(courses)
        items = active + expired
        excluded_items = _excluded_assignment_items(courses)
        trackable_assignments = _trackable_assignment_items(courses)
        today, tomorrow, soon = _imminent_summary(courses)
        urg = []
        if today:
            urg.append(f"오늘 {today}건")
        if tomorrow:
            urg.append(f"내일 {tomorrow}건")
        if soon:
            urg.append(f"7일 이내 {soon}건")
        if items:
            sub = f"진행 중 {len(active)}건"
            if urg:
                sub += " · 마감 " + " · ".join(urg)
            if expired:
                sub += f" · 기간 만료 {len(expired)}건"
        else:
            sub = "남은 할 일이 없습니다"
        if excluded_items:
            sub += f" · 제외 {len(excluded_items)}건"
        screen("남은 할 일", subtitle=sub)
        if not items and not trackable_assignments:
            console.print(Padding(Text("현재 남은 할 일이 없습니다.", style="dim"),
                                  (0, 2)))
            console.print()
            pause()
            return driver, wait
        course_natural = max(
            (_w(_course_display_name(c)) for c, _ in items), default=0)
        title_natural = max((_w(a.title) for _, a in items), default=0)
        status_width = max(
            [_w("기간 만료")] +
            [_w(a.status or ("미응시" if a.type == QUIZ else "미제출"))
             for _, a in items]
        )
        coursew, titlew = _fit_display_widths(
            (course_natural, title_natural),
            max(16, console.width - 4 - 18 - status_width), (8, 8))

        def _row(c, a, overdue=False):
            kind = _pad("퀴즈" if a.type == QUIZ else "과제", 8)
            if overdue:
                dday, status = "지남", "기간 만료"
            else:
                dday = _dday_label(_days_until(a.deadline)) or "-"
                status = a.status or ("미응시" if a.type == QUIZ else "미제출")
            if console.width <= COMPACT_WIDTH:
                return _compact_detail_row(
                    f"{kind.strip()} {dday}",
                    f"{_course_display_name(c)} / {a.title}", status,
                    console.width,
                )
            course = _pad(
                _trunc_middle(_course_display_name(c), coursew), coursew)
            ttl = _pad(_trunc_middle(a.title, titlew), titlew)
            return _fit_menu_text(
                f"{kind}{_pad(dday, 6)}{course}  {ttl}  {status}")

        choices = []
        if active:
            choices.append(Separator("진행 중"))
            choices += [Choice(_row(c, a), value=(c, a)) for c, a in active]
        if expired:
            if choices:
                choices.append(Separator(" "))
            choices.append(Separator("기간 만료 (미제출·미응시)"))
            choices += [Choice(_row(c, a, overdue=True), value=(c, a)) for c, a in expired]
        if trackable_assignments:
            if choices:
                choices.append(Separator(" "))
            choices.append(Choice(
                _menu_row(
                    "과제 표시 설정", f"미제출 {len(trackable_assignments)}건",
                    width=_menu_label_width(["과제 표시 설정"]),
                ),
                value="__tracking__",
            ))
        sel = select("할 일", choices, back_label="메인으로")
        if sel in (None, "back"):
            return driver, wait
        if sel == "__tracking__":
            _manage_assignment_tracking(courses, uid)
            continue
        _course, act = sel
        driver, wait = _dispatch_activity(
            driver, wait, _course, act, uid, pw, headless,
            notice_title="남은 할 일",
        )


def _calendar_entries(courses: list[Course], now=None):
    entries = []
    for course in courses:
        for item in course.schedules:
            if item.type == VOD and item.completed:
                continue
            entries.append({
                "course": course,
                "item": item,
                "type": item.type,
                "title": item.title,
                "url": item.url,
                "starts_at": item.starts_at,
                "ends_at": item.ends_at,
                "when": (item.starts_at if item.type == ZOOM or item.status == "upcoming"
                         else item.ends_at or item.starts_at),
            })
        for activity in course.activities:
            # A local exclusion only affects the todo workflow.  Deadlines stay
            # visible in the calendar so the LMS fact is never hidden.
            if not activity.deadline:
                continue
            if activity.completed and not _is_this_week(activity.deadline, now=now):
                continue
            entries.append({
                "course": course,
                "item": activity,
                "type": activity.type,
                "title": activity.title,
                "url": activity.url,
                "starts_at": activity.deadline,
                "ends_at": activity.deadline,
                "when": activity.deadline,
            })
    return sorted(entries, key=lambda entry: _deadline_sort_key(entry["when"]))


def _calendar_group(entry, now=None) -> str:
    current = now or _dt.now().astimezone()
    start, end = _week_bounds(current)
    starts_at = _parse_datetime(entry.get("starts_at"))
    ends_at = _parse_datetime(entry.get("ends_at")) or starts_at
    if starts_at is None:
        return "unknown"
    if starts_at.tzinfo is None and start.tzinfo is not None:
        starts_at = starts_at.replace(tzinfo=start.tzinfo)
    if ends_at and ends_at.tzinfo is None and start.tzinfo is not None:
        ends_at = ends_at.replace(tzinfo=start.tzinfo)
    if starts_at < end and (ends_at is None or ends_at >= start):
        return "week"
    if starts_at >= end:
        return "later"
    return "past"


def _calendar_status(entry, now=None) -> str:
    item = entry["item"]
    kind = entry["type"]
    current = now or _dt.now().astimezone()
    if kind == VOD:
        if item.status == "upcoming":
            return ("이번 주 대상 · 열람 전"
                    if _is_this_week(item.starts_at, now=current) else "열람 예정")
        if item.status == "expired":
            return "기간 종료 · 미수강"
        if item.status == "available":
            return "지금 가능 · 미수강"
        return "상태 미확인"
    if kind == ZOOM:
        starts_at = _parse_datetime(item.starts_at)
        ends_at = _parse_datetime(item.ends_at) or starts_at
        if starts_at is None:
            return "일정 미확인"
        if starts_at and current < starts_at:
            return "Zoom 예정"
        if ends_at and current <= ends_at:
            return "Zoom 진행 중"
        return "Zoom 종료"
    if item.completed:
        return f"완료 · {item.score}" if item.score else "완료"
    if item.is_overdue:
        return "기간 만료"
    return item.status or ("미응시" if kind == QUIZ else "미제출")


def _calendar_time_label(entry) -> str:
    item = entry["item"]
    target = (item.ends_at if entry["type"] == VOD and item.status == "available"
              else entry["when"])
    value = _parse_datetime(target)
    base = (f"{value.month}/{value.day} {value.hour:02d}:{value.minute:02d}"
            if value else "날짜 미정")
    if entry["type"] == VOD and item.status == "available":
        return base + " 마감"
    if entry["type"] == VOD and item.status == "upcoming":
        return base + " 시작"
    return base


def _calendar_board(driver, wait, courses, uid, pw, headless):
    while True:
        entries = _calendar_entries(courses)
        grouped = {name: [entry for entry in entries if _calendar_group(entry) == name]
                   for name in ("week", "later", "unknown", "past")}
        sub = (f"이번 주 {len(grouped['week'])}건 · 이후 {len(grouped['later'])}건"
               f" · 날짜 미정 {len(grouped['unknown'])}건"
               f" · 지난 일정 {len(grouped['past'])}건")
        screen("통합 일정", subtitle=sub)
        if not entries:
            console.print(Padding(Text("표시할 일정이 없습니다.", style="dim"),
                                  (0, 2)))
            console.print()
            pause()
            return driver, wait
        course_natural = max(
            (_w(_course_display_name(entry["course"])) for entry in entries),
            default=0,
        )
        title_natural = max((_w(entry["title"]) for entry in entries), default=0)
        status_width = max(
            (_w(_calendar_status(entry)) for entry in entries), default=0)
        coursew, titlew = _fit_display_widths(
            (course_natural, title_natural),
            max(16, console.width - 4 - 29 - status_width), (8, 8))

        def _row(entry):
            kinds = {VOD: "동영상", QUIZ: "퀴즈", ASSIGN: "과제", ZOOM: "Zoom"}
            kind = _pad(kinds.get(entry["type"], "기타"), 8)
            if console.width <= COMPACT_WIDTH:
                return _compact_detail_row(
                    f"{kind.strip()} {_calendar_time_label(entry)}",
                    f"{_course_display_name(entry['course'])} / {entry['title']}",
                    _calendar_status(entry), console.width,
                )
            course = _pad(_trunc_middle(
                _course_display_name(entry["course"]), coursew), coursew)
            title = _pad(_trunc_middle(entry["title"], titlew), titlew)
            when = _pad(_trunc(_calendar_time_label(entry), 15), 15)
            status = _calendar_status(entry)
            return _fit_menu_text(
                f"{kind}{when}  {course}  {title}  {status}")

        choices = []
        labels = (("week", "이번 주"), ("later", "이후 일정"),
                  ("unknown", "날짜 미정"), ("past", "지난 일정"))
        for key, label in labels:
            if not grouped[key]:
                continue
            if choices:
                choices.append(Separator(" "))
            choices.append(Separator(label))
            choices += [Choice(_row(entry), value=entry) for entry in grouped[key]]
        sel = select("일정 선택", choices, back_label="메인으로")
        if sel in (None, "back"):
            return driver, wait
        driver, wait = _dispatch_activity(
            driver, wait, sel["course"], sel["item"], uid, pw, headless,
            notice_title="통합 일정",
        )


def _graded_activities(course: Course):
    return [activity for activity in course.activities
            if activity.type in (QUIZ, ASSIGN)]


def _grade_summary(course: Course) -> str:
    if not course.enriched:
        return "정보 미확인"
    items = _graded_activities(course)
    if not items:
        if _collection_failed(course, "퀴즈", "과제", "활동 정보"):
            return "정보 확인 필요"
        return "평가 항목 없음"
    scored = [item for item in items if item.score]
    completed = sum(1 for item in items if item.completed)
    if len(scored) == 1:
        score = f"점수 {scored[0].score}"
    elif scored:
        score = f"채점 {len(scored)}건"
    else:
        score = "채점 점수 없음"
    prefix = ("일부 정보 확인 필요 · "
              if _collection_failed(course, "퀴즈", "과제", "활동 정보") else "")
    return f"{prefix}{score} · 완료 {completed}/{len(items)}"


def _grade_course_loop(driver, wait, course: Course,
                       uid: str, pw: str, headless: bool):
    while True:
        items = _graded_activities(course)
        screen(
            f"성적·강의계획서 · {_course_display_name(course)}",
            subtitle=_grade_summary(course),
        )
        choices = ([Choice("강의계획서 열기", value="syllabus")]
                   if course.syllabus_url
                   else [Separator("강의계획서 정보 없음")])
        if not course.syllabus_url and not items:
            console.print(Padding(
                Text("열 수 있는 강의계획서나 평가 항목이 없습니다.", style="dim"),
                (0, 2),
            ))
            console.print()
            pause()
            return driver, wait
        if items:
            choices.append(Separator(" "))
            choices.append(Separator("평가 항목"))
            natural_title = max((_w(item.title) for item in items), default=0)
            detail_width = max((
                _w(_activity_status_label(item))
                + _w(f"점수 {item.score}" if item.score else "미채점")
                for item in items
            ), default=0)
            titlew = min(
                natural_title,
                max(8, console.width - 4 - 15 - detail_width))
            for item in items:
                kind = _pad("퀴즈" if item.type == QUIZ else "과제", 8)
                status = _activity_status_label(item)
                score = f"점수 {item.score}" if item.score else "미채점"
                if console.width <= COMPACT_WIDTH:
                    row = _compact_detail_row(
                        f"{kind.strip()} {item.title}", score, status, console.width)
                else:
                    row = (f"{kind}{_pad(_trunc_middle(item.title, titlew), titlew)}"
                           f"  {status}  ·  {score}")
                choices.append(Choice(_fit_menu_text(row), value=item))
        sel = select("열 항목", choices, back_label="이전으로")
        if sel in (None, "back"):
            return driver, wait
        if sel == "syllabus":
            url = course.syllabus_url
            title = "강의계획서"
        else:
            url = sel.url
            title = sel.title
        driver, wait, opened = _open_authenticated_url(
            driver, wait, url, uid, pw)
        if opened:
            notice(f"성적·강의계획서 · {_course_display_name(course)}",
                   f"{OK} 로그인된 Chrome에서 열었습니다 · "
                   f"{_escape_markup(_clean_text(title))}")
        else:
            notice(
                f"성적·강의계획서 · {_course_display_name(course)}",
                f"[{RED}]링크를 열 수 없습니다.[/]",
            )


def _grade_board(driver, wait, courses, uid, pw, headless):
    while True:
        screen("성적 요약 · 강의계획서", subtitle="과목을 선택하면 점수와 강의계획서를 확인합니다")
        choices = []
        for course in courses:
            summary = _grade_summary(course)
            row = _compact_detail_row(
                _course_display_name(course), "", summary, console.width)
            choices.append(Choice(row, value=course))
        if not choices:
            console.print(Padding(Text("표시할 과목이 없습니다.", style="dim"),
                                  (0, 2)))
            console.print()
            pause()
            return driver, wait
        sel = select("과목 선택", choices, back_label="메인으로")
        if sel in (None, "back"):
            return driver, wait
        driver, wait = _grade_course_loop(
            driver, wait, sel, uid, pw, headless)


def _refresh(uid, pw, cache_sessions=True):
    screen("새로고침", subtitle="강의 정보를 다시 불러옵니다")
    driver_utils.set_stdout(False)
    courses = None
    try:
        with spin("강의 분석 중...") as st:
            def progress(cur, total, _m):
                st.update(f"강의 분석 중 {cur}/{total}")
            courses = _run_authenticated_sessions(
                uid,
                pw,
                lambda clients: automation.full_scan_sessions(
                    clients,
                    progress_callback=progress,
                    with_activities=True,
                ),
                pooled=True,
                cache_sessions=cache_sessions,
                persist=True,
                on_reauthenticate=lambda: st.update("로그인 세션 갱신 중..."),
            )
    except KeyboardInterrupt:
        raise
    except AuthenticationExpiredError as exc:
        driver_utils.error(str(exc))
        courses = None
    except Exception as exc:
        driver_utils.error(f"강의 새로고침 실패: {exc}")
        courses = None
    finally:
        driver_utils.set_stdout(False)
    errors = driver_utils.flush_errors()
    if errors:
        for e in errors:
            _warning_line(e)
        console.print()
        pause()
    return courses


def _retry_failed_courses(courses, uid, pw, cache_sessions=True):
    failed = [course for course in courses if course.collection_errors]
    if not failed:
        return courses, 0, None
    try:
        def shells():
            return [Course(
                course_id=course.course_id,
                name=course.name,
                professor=course.professor,
                url=course.url,
                syllabus_url=course.syllabus_url,
            ) for course in failed]
        with spin(f"오류 과목 {len(failed)}개 다시 확인 중..."):
            rescanned = _run_authenticated_sessions(
                uid,
                pw,
                lambda clients: automation.full_scan_sessions(
                    clients, with_activities=True, courses=shells()),
                pooled=True,
                cache_sessions=cache_sessions,
                persist=True,
            )
        replacements = {course.course_id: course for course in rescanned
                        if not course.collection_errors}
        merged = [replacements.get(course.course_id, course) for course in courses]
        _apply_assignment_exclusions(merged, uid)
        return merged, len(replacements), None
    except Exception as exc:
        return courses, 0, str(exc)


def _search_courses(courses):
    screen("과목 검색", subtitle="과목명 또는 교수명으로 찾습니다")
    query = _ask_question(questionary.text(
        "검색어", qmark="/", style=STYLE, erase_when_done=True,
    ))
    if not query:
        return None
    needle = query.strip().casefold()
    matches = [course for course in courses
               if needle in course.name.casefold()
               or needle in course.professor.casefold()]
    if not matches:
        safe_query = _escape_markup(_clean_text(query.strip()))
        notice("과목 검색", f"[dim]‘{safe_query}’ 검색 결과가 없습니다.[/]")
        return None
    compact = console.width <= COMPACT_WIDTH

    def _search_row(course: Course) -> str:
        professor = (f"교수 {course.professor}" if course.professor
                     else "교수 미표기")
        if compact:
            return _compact_detail_row(
                _course_display_name(course), "", professor, console.width)
        status = ("정보 확인 필요" if course.collection_errors
                  else _course_vod_status(course))
        return _compact_detail_row(
            _course_display_name(course), professor, status, console.width)

    choices = [Separator("과목 · 교수" if compact else "과목 · 교수 · 학습 상태")]
    choices += [Choice(_search_row(course), value=course) for course in matches]
    selected = select(
        f"검색 결과 {len(matches)}개", choices, back_label="메인으로")
    return None if selected in (None, "back") else selected


def _manage_pinned_courses(courses, pinned_ids):
    screen("고정 과목", subtitle="메인 상단에 항상 표시할 과목을 선택합니다")
    choices = [Choice(
        _compact_detail_row(
            _course_display_name(course), "",
            (f"교수 {course.professor}" if course.professor
                              else "교수 미표기"), console.width),
        value=course.course_id,
        checked=course.course_id in pinned_ids,
    ) for course in courses]
    selected = select(
        "고정 과목", choices, multi=True, multi_submit="저장",
        back_label="설정으로")
    if selected is None:
        return pinned_ids
    updated = set(selected)
    if not config_manager.save_selected_courses(selected):
        notice("고정 과목", f"[{AMBER}]고정 설정을 저장하지 못했습니다.[/]")
        return pinned_ids
    return updated


def _confirm_account_clear() -> bool:
    screen("저장 계정 삭제",
           subtitle="저장된 학번·비밀번호와 로그인 세션을 이 PC에서 제거합니다")
    console.print(
        f"  [{AMBER}]고정 과목과 과제 제외 설정도 삭제하고 로그인 화면으로 돌아갑니다.[/]"
    )
    console.print()
    return select("계정 삭제", [
        Choice("취소", value="back"),
        Separator(" "),
        Choice("저장 계정과 세션 모두 삭제", value="clear"),
    ]) == "clear"


def _settings_menu(courses, pinned_ids, show_completed):
    while True:
        screen("설정", subtitle="화면 구성과 이 PC에 저장된 로그인 정보를 관리합니다")
        sel = select("설정", [
            Choice("고정 과목 관리", value="pins"),
            Choice(("완료 과목 접기" if show_completed
                    else "완료 과목 펼치기"), value="completed"),
            Choice("현재 로그인 세션 초기화", value="session"),
            Choice("저장 계정과 세션 삭제", value="account"),
        ], back_label="메인으로")
        if sel in (None, "back"):
            return pinned_ids, show_completed, None
        if sel == "pins":
            pinned_ids = _manage_pinned_courses(courses, pinned_ids)
        elif sel == "completed":
            show_completed = not show_completed
        elif sel == "session":
            if config_manager.clear_session_cache():
                notice("로그인 세션", f"{OK} 저장된 세션을 초기화했습니다. 다음 조회에서 다시 로그인합니다.")
                return pinned_ids, show_completed, "session"
            notice(
                "로그인 세션",
                f"[{RED}]저장된 세션을 초기화하지 못했습니다.[/]",
                subtitle="설정 파일 권한을 확인한 뒤 다시 시도하세요",
            )
        elif sel == "account" and _confirm_account_clear():
            if config_manager.clear_saved_account(preserve_preferences=False):
                return pinned_ids, show_completed, "logout"
            notice("저장 계정 삭제", f"[{RED}]저장 정보를 삭제하지 못했습니다.[/]")


def _tools_menu():
    screen("도구 및 설정", subtitle="자주 쓰지 않는 기능을 한곳에 모았습니다")
    return select("도구", [
        Choice("과목 검색", value="__search__"),
        Choice("성적·강의계획서", value="__grades__"),
        Choice("전체 새로고침", value="__refresh__"),
        Separator(" "),
        Choice("설정", value="__settings__"),
        Choice("환경 진단", value="__doctor__"),
    ], back_label="메인으로")


def _show_help() -> None:
    screen("키보드 도움말", subtitle="메인 화면에서 바로 실행할 수 있습니다")
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column(style=f"bold {RED}", width=4)
    table.add_column()
    for key, label in (
        ("A", "자동수강 준비"), ("/", "과목 검색"),
        ("R", "전체 새로고침"), ("F", "오류 과목만 재확인"),
        ("C", "완료 과목 펼치기·접기"), ("X", "계정 및 세션 설정"),
        ("?", "이 도움말"), ("Q", "종료"),
    ):
        table.add_row(key, label)
    console.print(Padding(table, (0, 2)))
    console.print()
    pause()


def _warning_line(message) -> None:
    warning = Text("주의  ", style=AMBER)
    warning.append(_clean_text(message))
    console.print(Padding(warning, (0, 2)))


def _show_course_errors(course) -> None:
    screen(
        f"일부 확인 · {_course_display_name(course)}",
        subtitle="수집하지 못한 항목",
    )
    for message in course.collection_errors:
        _warning_line(message)
    console.print()
    pause()


def _render_doctor(report) -> None:
    from . import doctor

    counts = report.counts
    subtitle = (f"정상 {counts[doctor.PASS]} · 주의 {counts[doctor.WARNING]}"
                f" · 실패 {counts[doctor.FAIL]} · 생략 {counts[doctor.SKIPPED]}"
                f" · {report.duration_ms / 1000:.1f}초")
    screen("환경 진단", subtitle=subtitle)
    symbols = {
        doctor.PASS: ("정상", GREEN),
        doctor.WARNING: ("주의", AMBER),
        doctor.FAIL: ("실패", RED),
        doctor.SKIPPED: ("생략", "dim"),
    }
    if console.width <= COMPACT_WIDTH:
        for index, check in enumerate(report.checks):
            if index:
                console.print()
            symbol, style = symbols[check.status]
            heading = Text()
            heading.append(_pad(symbol, 6), style=style)
            heading.append(_clean_text(check.label), style="bold")
            console.print(Padding(heading, (0, 2)))
            console.print(Padding(
                Text(_clean_text(check.message), style="dim"), (0, 4)))
    else:
        table = Table(box=box.SIMPLE_HEAD, show_header=True, pad_edge=False)
        table.add_column("상태", width=6, no_wrap=True)
        table.add_column("검사", ratio=2)
        table.add_column("결과", ratio=5)
        for check in report.checks:
            symbol, style = symbols[check.status]
            table.add_row(
                Text(symbol, style=style), Text(_clean_text(check.label)),
                Text(_clean_text(check.message)))
        console.print(Padding(table, (0, 2)))
    console.print()
    console.print(Padding(Text(
        "진단은 조회 요청만 수행하며 영상을 재생하거나 제출하지 않습니다.",
        style="dim"), (0, 2)))
    console.print()


def _run_doctor(uid=None, pw=None, interactive=True):
    from . import doctor

    with spin("LMS와 로컬 환경을 진단하는 중...") as st:
        report = doctor.run_diagnostics(
            uid, pw, check_driver=False,
            progress_callback=lambda check: st.update(f"진단 중 · {check.label}"),
        )
    _render_doctor(report)
    if interactive:
        pause()
    return report


def _run_dashboard_session(login: _LoginContext) -> bool:
    """Run one authenticated dashboard and own its browser lifetime.

    The return value says whether the caller should show the login screen again.
    HTTP-only login does not create Chrome; any browser opened by dashboard
    actions is kept for subsequent actions and released exactly once here.
    """

    driver = wait = None
    courses = login.courses
    uid = login.user_id
    pw = login.password
    headless = login.headless
    remember = login.remember
    stop_event = threading.Event()
    last_refreshed = _dt.now().astimezone()
    show_completed = False
    pinned_ids = set(config_manager.load_config().get("selected_courses") or [])
    _apply_assignment_exclusions(courses, uid)

    try:
        while True:
            # Section suffix visibility depends on the complete collection,
            # including courses split between active and completed sections.
            _prepare_course_display_names(courses)
            screen(subtitle=f"학번 {uid}", brand=True)
            _dashboard_overview(courses, last_refreshed)
            console.print()
            namew = _course_name_width(courses, terminal_width=console.width)
            active = [c for c in courses if not _course_done(c)]
            completed = [c for c in courses if _course_done(c)]
            pinned = [c for c in courses if c.course_id in pinned_ids]
            pinned_keys = {c.course_id for c in pinned}
            active = [c for c in active if c.course_id not in pinned_keys]
            completed_hidden = [c for c in completed if c.course_id not in pinned_keys]
            partial = sum(bool(c.collection_errors) for c in courses)
            pending = _todo_count(courses)
            week_events = sum(1 for entry in _calendar_entries(courses)
                              if _calendar_group(entry) == "week")

            quick_labels = ["자동수강 준비", "통합 일정", "남은 할 일"]
            if partial:
                quick_labels.append("오류 과목 재확인")
            quick_width = _menu_label_width(quick_labels)
            choices = [Separator("바로가기"),
                Choice(
                    _menu_row(
                        "자동수강 준비",
                        f"수강 가능한 영상 {sum(c.watchable_count for c in courses)}개",
                        width=quick_width,
                    ),
                    value="__all__",
                ),
                Choice(
                    _menu_row(
                        "통합 일정", f"이번 주 일정 {week_events}건",
                        width=quick_width,
                    ),
                    value="__calendar__",
                ),
                Choice(
                    _menu_row(
                        "남은 할 일", f"미완료 퀴즈·과제 {pending}건",
                        width=quick_width,
                    ),
                    value="__todo__",
                ),
            ]
            if partial:
                choices.append(Choice(
                    _menu_row(
                        "오류 과목 재확인", f"{partial}개",
                        width=quick_width,
                    ),
                    value="__retry__",
                ))
            if pinned:
                choices += [Separator(" ")]
                choices += _course_section("고정 과목", pinned, namew)
            if active:
                choices += [Separator(" ")]
                choices += _course_section("수강 중 과목", active, namew)
            if completed_hidden:
                toggle = ("완료 과목 접기" if show_completed
                          else f"완료 과목 보기 ({len(completed_hidden)}과목)")
                choices += [Separator(" "), Choice(toggle, value="__completed__")]
            if show_completed and completed_hidden:
                choices += [Separator(" ")]
                choices += _course_section("완료 과목", completed_hidden, namew)
            choices += [Separator(" "),
                        Choice("도구 및 설정", value="__tools__"),
                        Choice("종료", value="__quit__")]
            shortcuts = {
                "a": "__all__", "/": "__search__", "r": "__refresh__",
                "c": "__completed__", "x": "__settings__",
                "?": "__help__", "q": "__quit__",
            }
            if partial:
                shortcuts["f"] = "__retry__"
            sel = select(
                "메뉴 선택", choices, escape=False, shortcuts=shortcuts,
                hotkey_hint="A 자동수강 | / 검색 | ? 도움말")

            if sel == "__tools__":
                sel = _tools_menu()
                if sel in (None, "__back__"):
                    continue

            if sel in (None, "__quit__"):
                return False
            if sel == "__all__":
                driver, wait = _watch_all(
                    driver, wait, courses, uid, pw, headless, stop_event)
            elif sel == "__calendar__":
                driver, wait = _calendar_board(
                    driver, wait, courses, uid, pw, headless)
            elif sel == "__todo__":
                driver, wait = _todo_board(
                    driver, wait, courses, uid, pw, headless)
            elif sel == "__grades__":
                driver, wait = _grade_board(
                    driver, wait, courses, uid, pw, headless)
            elif sel == "__search__":
                found = _search_courses(courses)
                if isinstance(found, Course):
                    driver, wait = _course_loop(
                        driver, wait, found, uid, pw, headless, stop_event)
            elif sel == "__doctor__":
                _run_doctor(uid, pw)
            elif sel == "__completed__":
                show_completed = not show_completed
            elif sel == "__retry__":
                courses, recovered, retry_error = _retry_failed_courses(
                    courses, uid, pw, cache_sessions=remember)
                if retry_error:
                    notice(
                        "오류 과목 재확인",
                        f"[{RED}]다시 확인하지 못했습니다: "
                        f"{_escape_markup(_clean_text(retry_error))}[/]",
                        subtitle="네트워크와 로그인 상태를 확인한 뒤 다시 시도하세요",
                    )
                elif recovered:
                    notice("오류 과목 재확인", f"{OK} {recovered}개 과목을 정상적으로 갱신했습니다.")
                else:
                    notice(
                        "오류 과목 재확인",
                        f"[{AMBER}]다시 확인했지만 복구된 과목이 없습니다.[/]",
                        subtitle="환경 진단 또는 LMS 원본 화면에서 상태를 확인하세요",
                    )
            elif sel == "__refresh__":
                new = _refresh(uid, pw, cache_sessions=remember)
                if new is not None:
                    courses = new
                    _apply_assignment_exclusions(courses, uid)
                    last_refreshed = _dt.now().astimezone()
            elif sel == "__settings__":
                pinned_ids, show_completed, action = _settings_menu(
                    courses, pinned_ids, show_completed)
                if action in ("session", "logout"):
                    _close_browser(driver)
                    driver = wait = None
                if action == "logout":
                    return True
            elif sel == "__help__":
                _show_help()
            else:
                if not isinstance(sel, Course):
                    continue
                if sel.collection_errors:
                    _show_course_errors(sel)
                driver, wait = _course_loop(
                    driver, wait, sel, uid, pw, headless, stop_event)
    finally:
        _close_browser(driver)


def main():
    parser = argparse.ArgumentParser(
        prog="hoseo-macro",
        description="호서대학교 LMS 자동수강·통합일정·성적 확인 대화형 도구",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version('hoseo-macro')}")
    parser.add_argument("--export", metavar="PATH", help="저장된 로그인 정보로 강의 정보를 파일에 저장")
    parser.add_argument("--format", choices=("json", "csv"), default="json", help="내보내기 형식")
    parser.add_argument("--force", action="store_true", help="기존 출력 파일 덮어쓰기")
    parser.add_argument("command", nargs="?", choices=("doctor",),
                        help="doctor: LMS 연결과 파서 상태 진단")
    args = parser.parse_args()
    if args.command and args.export:
        parser.error("doctor와 --export는 동시에 사용할 수 없습니다")
    if args.force and not args.export:
        parser.error("--force는 --export와 함께 사용해야 합니다")
    if args.export:
        return _export_courses(args.export, args.format, args.force)
    if args.command == "doctor":
        cfg = config_manager.load_config()
        report = _run_doctor(
            cfg.get("user_id") or None, cfg.get("password") or None,
            interactive=False)
        return 0 if report.ok else 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print("대화형 터미널에서 실행하세요 (PowerShell · Windows Terminal · macOS/Linux 터미널).", style=RED)
        return 2
    driver_utils.set_stdout(False)

    try:
        while True:
            login = _login()
            if login is None:
                break
            if not _run_dashboard_session(login):
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            automation.close_temp_drivers()
        except Exception:
            pass
        info("[dim]종료합니다.[/]", blank_after=False)
    return 0


def _export_courses(destination, output_format, overwrite):
    cfg = config_manager.load_config()
    if cfg.get("config_error") or not (cfg.get("user_id") and cfg.get("password")):
        console.print("저장된 로그인 정보를 사용할 수 없습니다.", style=RED)
        return 1
    try:
        remember = bool(cfg.get("remember_me"))
        courses = _run_authenticated_sessions(
            cfg["user_id"],
            cfg["password"],
            lambda clients: automation.full_scan_sessions(
                clients, with_activities=True),
            pooled=True,
            cache_sessions=remember,
            persist=remember,
        )
        output = exporter.export_courses(courses, destination, output_format, overwrite)
        console.print(Text(f"저장 완료: {_clean_text(output)}"))
        if any(course.collection_errors for course in courses):
            console.print("일부 항목은 수집에 실패했습니다. 출력의 partial 값을 확인하세요.", style=AMBER)
            return 3
        return 0
    except FileExistsError as exc:
        console.print(Text(_clean_text(exc), style=RED))
        return 2
    except Exception as exc:
        console.print(Text(f"내보내기 실패: {_clean_text(exc)}", style=RED))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
