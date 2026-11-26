"""The sjobs view: job tables as a Textual app that refreshes itself.

Slurm access goes through `sjobs.fetch_snapshot`, so every refresh still makes
one process (or SSH connection) and one squeue call. Cancelling a job and
reading its log are extra calls, made only when the user asks.
"""

import os
import re
import select
import sys
import time
from typing import ClassVar

import click
from rich.segment import Segment, Segments
from rich.style import Style
from rich.terminal_theme import TerminalTheme
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBar, ScrollBarRender
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    SelectionList,
    Static,
    Tab,
    Tabs,
)

from . import sjobs

# A manual refresh right after another one is ignored, to spare the controller.
MIN_MANUAL_REFRESH = 5
LOG_LINES = 200
FILTER_KEYS = ("job_id", "name", "user", "cwd", "reason", "partition", "state")
# The default: a theme made of the terminal's own colors.
AUTO_THEME = "auto"
TERMINAL_THEME = "terminal"


COLOR_REPORT_RE = re.compile(
    rb"\x1b\](10|11|4;(\d+));rgb:([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)", re.IGNORECASE
)
# Device attributes: every terminal answers, so its reply ends the color query.
ATTRIBUTES_REPLY_RE = re.compile(rb"\x1b\[\?[\d;]*c")


def parse_color_reports(data):
    """Terminal color reports -> {"foreground", "background", "palette"}.

    Colors are `#rrggbb`; palette entries the terminal did not report are None.
    """
    colors = {"foreground": None, "background": None, "palette": [None] * 16}
    for match in COLOR_REPORT_RE.finditer(data):
        code, index, *channels = match.groups()
        value = "#" + "".join(
            f"{round(int(part, 16) / (16 ** len(part) - 1) * 255):02x}"
            for part in channels
        )
        if code == b"10":
            colors["foreground"] = value
        elif code == b"11":
            colors["background"] = value
        elif int(index) < 16:
            colors["palette"][int(index)] = value
    return colors


def query_terminal_colors(timeout=0.5):
    """Ask the terminal for its colors (OSC 10, 11 and 4); None if it cannot.

    Runs before the app takes over the terminal.
    """
    try:
        import termios
        import tty
    except ImportError:
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    queries = b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\"
    queries += b"".join(b"\x1b]4;%d;?\x1b\\" % index for index in range(16))
    fd = sys.stdin.fileno()
    data = b""
    try:
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            os.write(sys.stdout.fileno(), queries + b"\x1b[c")
            deadline = time.monotonic() + timeout
            while not ATTRIBUTES_REPLY_RE.search(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    break
                data += os.read(fd, 1024)
        finally:
            # Drop a late or partial answer so it never arrives as key presses.
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
    except (OSError, termios.error):
        return None
    return parse_color_reports(data)


def luminance(color):
    """Relative brightness of `#rrggbb`, 0 (black) to 1 (white)."""
    red, green, blue = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def rgb(color):
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))


def terminal_themes(colors):
    """A Textual theme and a Rich ANSI palette made of the terminal's colors.

    Either is None when the terminal did not report enough to build it.
    """
    if not colors or not (colors["background"] and colors["foreground"]):
        return None, None
    background, foreground = colors["background"], colors["foreground"]
    palette = colors["palette"]
    defaults = ("#e06c75", "#98c379", "#d19a66", "#61afef", "#c678dd", "#56b6c2")
    red, green, yellow, blue, magenta, cyan = (
        palette[index] or default for index, default in enumerate(defaults, start=1)
    )
    theme = Theme(
        name=TERMINAL_THEME,
        dark=luminance(background) < 0.5,
        background=background,
        surface=background,
        panel=background,
        foreground=foreground,
        primary=blue,
        secondary=cyan,
        accent=magenta,
        warning=yellow,
        error=red,
        success=green,
    )
    ansi = None
    if all(palette):
        colors16 = [rgb(color) for color in palette]
        ansi = TerminalTheme(
            rgb(background), rgb(foreground), colors16[:8], colors16[8:]
        )
    return theme, ansi


def matches(job, text):
    """Whether a job contains the filter text in any filterable field."""
    text = text.lower()
    return any(text in str(job.get(key, "")).lower() for key in FILTER_KEYS)


def detail_sections(job):
    """The fields worth reading, grouped, as [(title, [(label, value)])]."""
    raw = job.get("_raw") or {}

    def value(key):
        return sjobs.format_value(raw.get(key))

    why = job["reason"]
    slurm_reason = value("state_reason")
    if slurm_reason and slurm_reason not in ("None", why):
        why = f"{why} (Slurm: {slurm_reason})" if why else slurm_reason
    nodes = value("nodes") or value("scheduled_nodes")
    errors = job["_errors"]
    running = bool(job["progress"])
    sections = [
        (
            "Job",
            [
                ("ID", job["job_id"]),
                ("Name", job["name"]),
                ("User", job["user"]),
                ("Account", value("account")),
                ("QOS", value("qos")),
                ("Partition", job["partition"]),
                ("State", job["state_label"]),
                ("Why", why),
                ("Exit", job["exit"]),
            ],
        ),
        (
            "Resources",
            [
                ("CPUs", job["cpus"]),
                ("Memory", job["mem"]),
                ("GPUs", job["gpus"]),
                ("Nodes", nodes),
                ("Constraint", value("features")),
            ],
        ),
        (
            "Time",
            [
                ("Submitted", job["submitted"]),
                (
                    "Est. start",
                    f"{job['est_start_at']} ({job['est_start']})"
                    if job["est_start_at"]
                    else "",
                ),
                ("Running for", job["elapsed"] if running else ""),
                ("Ran for", job["ran"]),
                ("Time limit", job["time_limit"]),
                ("Left", job["time_left"] if running else ""),
                ("Ends", job["end_time"] if running else ""),
                ("Ended", job["ended"]),
            ],
        ),
        ("Priority", [("Priority", job["priority"])]),
        (
            "Files",
            [
                ("Directory", value("current_working_directory")),
                ("Output", job["_output"]),
                ("Errors", "" if errors == job["_output"] else errors),
                ("Script", value("command")),
            ],
        ),
    ]
    return [
        (title, [(label, text) for label, text in rows if text])
        for title, rows in sections
    ]


def detail_text(job):
    """detail_sections as styled text."""
    if job.get("_tasks"):
        job = job["_tasks"][0]
    text = Text()
    sections = [(title, rows) for title, rows in detail_sections(job) if rows]
    width = max(len(label) for _title, rows in sections for label, _value in rows)
    for title, rows in sections:
        text.append(f"{title}\n", style="bold")
        for label, value in rows:
            text.append(f"  {label.ljust(width)}  ", style="dim")
            text.append(f"{value}\n")
        text.append("\n")
    text.rstrip()
    return text


class ThinScrollBarRender(ScrollBarRender):
    """Scrollbars drawn as a thin line on an invisible track."""

    @classmethod
    def render_bar(
        cls,
        size=25,
        virtual_size=50,
        window_size=20,
        position=0,
        thickness=1,
        vertical=True,
        back_color=None,
        bar_color=None,
    ):
        size = int(size)
        width = thickness if vertical else 1
        before = Segment(
            " " * width, Style(bgcolor=back_color, meta={"@mouse.down": "scroll_up"})
        )
        after = Segment(
            " " * width, Style(bgcolor=back_color, meta={"@mouse.down": "scroll_down"})
        )
        segments = [before] * size
        if window_size and size and virtual_size and virtual_size > window_size:
            thumb = max(1, round(size * window_size / virtual_size))
            start = round((size - thumb) * position / (virtual_size - window_size))
            line = Segment(
                ("┃" if vertical else "━") * width,
                Style(
                    color=bar_color, bgcolor=back_color, meta={"@mouse.down": "grab"}
                ),
            )
            segments[start : start + thumb] = [line] * thumb
            segments[start + thumb :] = [after] * (size - start - thumb)
        if vertical:
            return Segments(segments, new_lines=True)
        return Segments((segments + [Segment.line()]) * thickness, new_lines=False)


class JobTable(DataTable):
    """A category's table; ↓ on its last row and ↑ on its first move on to the
    next or previous table, so the stacked tables read as one list."""

    def action_cursor_down(self):
        at_end = self.cursor_row >= self.row_count - 1
        if not (at_end and self.app.move_between_tables(self, 1)):
            super().action_cursor_down()

    def action_cursor_up(self):
        if not (self.cursor_row <= 0 and self.app.move_between_tables(self, -1)):
            super().action_cursor_up()


class TextScreen(ModalScreen):
    """A scrollable page with a title; Esc closes it."""

    BINDINGS: ClassVar = [Binding("escape,q", "dismiss", "Close")]

    def __init__(self, title, body):
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.title_text, style="bold"), id="dialog-title")
            with VerticalScroll():
                yield Static(self.body, id="body")
            yield Static(Text("Esc to close", style="dim"), classes="hint")


class JobDetail(TextScreen):
    """The fields of one job worth reading."""

    BINDINGS: ClassVar = [Binding("escape,q,enter,d", "dismiss", "Close")]

    def __init__(self, job):
        super().__init__(f"Job {job['job_id']}  {job['name']}", detail_text(job))
        self.job = job


class HelpScreen(TextScreen):
    """What sjobs shows and which keys it has."""

    BINDINGS: ClassVar = [Binding("escape,q,question_mark", "dismiss", "Close")]

    def __init__(self, keys, refresh):
        body = Text()
        body.append("Keys\n", style="bold")
        for key, description in keys:
            body.append(f"  {key:<13}", style="bold")
            body.append(f"{description}\n")
        body.append("\nWhat you see\n", style="bold")
        for mark, style, description in (
            ("●", "green", "running; yellow when 90% of its time limit is used"),
            ("○", "dim", "pending"),
            ("!", "yellow", "held, or waiting on something outside the queue"),
            ("✗", "bold red", "failed, or pending and will never start: act on it"),
            ("✓", "green", "completed"),
            ("·", "dim", "other states, such as completing"),
            ("×4", "", "array job folded into one row; Space shows its tasks"),
        ):
            body.append("  ")
            body.append(mark, style=style)
            body.append(" " * (13 - len(mark)) + f"{description}\n")
        body.append("\n")
        body.append(
            f"The tables refresh every {refresh} s. Finished jobs stay for a few "
            "minutes, as long as Slurm keeps them. When the tables do not fit the "
            "window they become tabs. Pending jobs that will never start are also "
            "listed under the header. Ctrl+P changes the color theme."
        )
        super().__init__("sjobs help", body)


class LogScreen(ModalScreen):
    """The end of a job's output file, read when opened."""

    BINDINGS: ClassVar = [
        Binding("escape,q,l", "dismiss", "Close"),
        Binding("r", "reload", "Reload"),
        Binding("e", "toggle_stream", "Output/errors"),
    ]

    def __init__(self, job, host):
        super().__init__()
        self.job = job
        self.host = host
        self.paths = [
            job["_output"],
            job["_errors"],
        ]
        if self.paths[1] in ("", self.paths[0]):
            self.paths.pop()
        self.stream = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(id="dialog-title")
            with VerticalScroll(id="log-scroll"):
                yield Static(Text("Loading…", style="dim"), id="body")
            hint = f"last {LOG_LINES} lines · r to reload"
            if len(self.paths) > 1:
                hint += " · e for output/errors"
            yield Static(Text(f"{hint} · Esc to close", style="dim"), classes="hint")

    def on_mount(self):
        self.action_reload()

    def action_toggle_stream(self):
        if len(self.paths) > 1:
            self.stream = 1 - self.stream
            self.action_reload()

    def action_reload(self):
        path = self.paths[self.stream]
        label = "Errors" if self.stream else "Output"
        self.query_one("#dialog-title", Static).update(
            Text.assemble(
                (f"{label} of job {self.job['job_id']}", "bold"), (f"  {path}", "dim")
            )
        )
        self.load(path)

    @work(thread=True, exclusive=True)
    def load(self, path):
        try:
            text = Text(sjobs.run_tail(path, self.host, LOG_LINES) or "(empty)")
        except click.ClickException as exc:
            text = Text(exc.format_message(), style="red")
        self.app.call_from_thread(self.show, text)

    def show(self, text):
        self.query_one("#body", Static).update(text)
        self.query_one("#log-scroll", VerticalScroll).scroll_end(animate=False)


class ColumnChooser(ModalScreen):
    """Pick optional columns; closing applies the choice."""

    BINDINGS: ClassVar = [
        Binding("escape,o", "close", "Done"),
        # Enter would otherwise toggle the highlighted entry.
        Binding("enter", "close", "Done", priority=True),
    ]

    def __init__(self, columns):
        super().__init__()
        self.columns = columns

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="small"):
            yield Static(Text("Optional columns", style="bold"), id="dialog-title")
            yield SelectionList(
                *(
                    (f"{key:<10} {description}", key, key in self.columns)
                    for key, description in sjobs.OPTIONAL_COLUMNS.items()
                )
            )
            yield Static(
                Text("Space to toggle · Enter when done", style="dim"), classes="hint"
            )

    def on_mount(self):
        self.query_one(SelectionList).focus()

    def action_close(self):
        self.dismiss(tuple(self.query_one(SelectionList).selected))


class ConfirmCancel(ModalScreen[bool]):
    """Asks before running scancel."""

    BINDINGS: ClassVar = [
        Binding("y", "answer(True)", "Cancel the job"),
        Binding("n,escape,q", "answer(False)", "Keep it"),
    ]

    def __init__(self, job):
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="small"):
            yield Static(
                Text.assemble(
                    "Cancel job ",
                    (self.job["job_id"], "bold"),
                    f" ({self.job['name'] or 'unnamed'})? ",
                    ("y", "bold red"),
                    "/",
                    ("n", "bold"),
                )
            )

    def action_answer(self, answer):
        self.dismiss(answer)


class SjobsApp(App):
    """Job tables that refresh themselves; see BINDINGS for the keys."""

    TITLE = "sjobs"
    CSS = """
    /* Thin, faint scrollbars that only stand out while used. Only on what
       scrolls: the footer, for one, keeps its hidden scrollbar. */
    DataTable, VerticalScroll, SelectionList {
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-color: $foreground 30%;
        scrollbar-color-hover: $foreground 50%;
        scrollbar-color-active: $primary 60%;
        scrollbar-corner-color: $background;
    }
    #summary { padding: 0 1; }
    #filter { display: none; margin: 0 1; }
    #filter.shown { display: block; }
    #tabs { display: none; margin: 1 1 0 1; }
    #tabs.shown { display: block; }
    /* Only the tables scroll sideways, not the page around them. */
    #tables { height: 1fr; overflow-x: hidden; }
    .category { padding: 1 1 0 1; }
    .tabbed .category { display: none; }
    DataTable { height: auto; max-height: 100%; margin: 0 1; }
    /* Flat tables on the theme's background. The focused table marks its
       cursor row with a tint of the primary color; other tables show none. */
    DataTable, DataTable:focus { background: $background; background-tint: 0%; }
    DataTable > .datatable--header, DataTable:focus > .datatable--header {
        background: $background; background-tint: 0%;
        color: $foreground; text-style: bold;
    }
    DataTable > .datatable--even-row, DataTable > .datatable--hover {
        background: $background;
    }
    DataTable > .datatable--cursor {
        background: $background; color: $foreground; text-style: none;
    }
    DataTable:focus > .datatable--cursor {
        background: $primary 25%; text-style: none;
    }
    SelectionList, SelectionList:focus { background: $background; background-tint: 0%; }
    SelectionList > .option-list--option-highlighted,
    SelectionList:focus > .option-list--option-highlighted {
        background: $primary 25%; color: $foreground; text-style: none;
    }
    TextScreen, LogScreen, ColumnChooser, ConfirmCancel { align: center middle; }
    #dialog {
        width: 90%; height: 85%; padding: 1 2;
        border: round $primary; background: $background;
    }
    #dialog.small { width: auto; height: auto; }
    #dialog-title, .hint { height: auto; }
    #dialog-title { padding-bottom: 1; }
    .hint { padding-top: 1; }
    SelectionList { height: auto; width: auto; border: none; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "help", "Help"),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("d", "details", "Details"),
        Binding("l", "log", "Log"),
        Binding("space", "toggle_array", "Expand", show=False),
        Binding("o", "columns", "Columns"),
        Binding("a", "toggle_all", "Mine/all"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("c", "cancel_job", "Cancel job"),
        Binding("1", "show_category('RUNNING')", "Running", show=False),
        Binding("2", "show_category('PENDING')", "Pending", show=False),
        Binding("3", "show_category('OTHER')", "Finished", show=False),
    ]
    KEYS = (
        ("↑ ↓", "move through the jobs, across tables"),
        ("1 2 3", "jump to running, pending, finished"),
        ("Enter, d", "details of the selected job"),
        ("Space", "expand or fold the tasks of an array job"),
        ("l", "end of the selected job's output file"),
        ("/", "filter by ID, name, user, path, partition or reason"),
        ("Esc", "clear the filter, close a window"),
        ("o", "choose optional columns"),
        ("a", "your jobs or all users' jobs"),
        ("r", "refresh now"),
        ("c", "cancel the selected job (asks first)"),
        ("Ctrl+P", "commands, including other color themes"),
        ("q", "quit"),
    )
    # Rows above which the tables switch to tabs, one category at a time.
    TAB_THRESHOLD_MARGIN = 4

    def __init__(
        self,
        show_all=False,
        username=None,
        host=None,
        config=None,
        interval=sjobs.DEFAULT_INTERVAL,
        columns=(),
        terminal_colors=None,
    ):
        super().__init__()
        self.show_all = show_all
        self.username = username
        self.host = host
        self.config = config or {}
        self.interval = interval
        self.columns = tuple(columns)
        self.terminal_colors = terminal_colors
        self.snapshot = None
        self.error = None
        self.fetched_at = None
        self.last_request = 0.0
        self.filter_text = ""
        self.expanded = set()
        self.tabbed = False
        self.active = "RUNNING"
        # Rows in display order per category, so a cursor row maps to its job.
        self.rows = {name: [] for name in sjobs.CATEGORIES}

    @property
    def show_user(self):
        return bool(self.show_all or self.username)

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        yield Input(placeholder="filter by ID, name, user, path, reason…", id="filter")
        yield Tabs(
            *(
                Tab(spec["title"], id=f"tab-{name}")
                for name, spec in sjobs.CATEGORIES.items()
            ),
            id="tabs",
        )
        with VerticalScroll(id="tables"):
            for name in sjobs.CATEGORIES:
                yield Static(id=f"label-{name}", classes="category")
                yield JobTable(id=f"table-{name}", cursor_type="row")
        yield Footer()

    def check_action(self, action, parameters):
        """While a dialog is open, only quitting reaches the app."""
        return action == "quit" or not isinstance(self.screen, ModalScreen)

    def on_mount(self):
        ScrollBar.renderer = ThinScrollBarRender
        self.apply_theme(self.config.get("theme") or AUTO_THEME)
        self.redraw()
        self.timer = self.set_interval(self.interval, self.refresh_jobs)
        self.refresh_jobs()

    def apply_theme(self, wanted):
        """The configured theme; by default one made of the terminal's colors,
        else Textual's light or dark theme to match the terminal."""
        theme, ansi = terminal_themes(self.terminal_colors)
        if theme is not None:
            self.register_theme(theme)
        if ansi is not None:
            # ANSI colors in cells (red, green, …) then use the terminal's own.
            self.ansi_theme_dark = self.ansi_theme_light = ansi
        if wanted != AUTO_THEME and wanted not in self.available_themes:
            self.notify(f"Unknown theme {wanted!r}; using the terminal's colors.")
            wanted = AUTO_THEME
        if wanted == AUTO_THEME:
            background = (self.terminal_colors or {}).get("background")
            if theme is not None:
                wanted = TERMINAL_THEME
            elif background is not None and luminance(background) > 0.5:
                wanted = "textual-light"
            else:
                wanted = "textual-dark"
        self.theme = wanted

    def on_resize(self, _event):
        # Tabs or stacked tables depend on the height; decide once sizes settle.
        if self.snapshot is not None:
            self.call_after_refresh(self.redraw)

    # Fetching -----------------------------------------------------------------

    def refresh_jobs(self):
        """Start one fetch in a worker thread; a newer one replaces a running one."""
        self.last_request = time.monotonic()
        self.fetch()

    @work(thread=True, exclusive=True, group="fetch")
    def fetch(self):
        now = time.time()
        try:
            snapshot = sjobs.fetch_snapshot(
                show_all=self.show_all,
                username=self.username,
                host=self.host,
                config=self.config,
                now=now,
            )
            error = None
        except click.ClickException as exc:
            snapshot, error = None, exc.format_message()
        self.call_from_thread(self.apply_snapshot, snapshot, error, now)

    def apply_snapshot(self, snapshot, error, now):
        """Show a fetch result; a failed fetch keeps the last good tables."""
        self.error = error
        if snapshot is not None:
            if self.snapshot is not None:
                self.announce(
                    sjobs.job_changes(self.snapshot["jobs"], snapshot["jobs"])
                )
            self.snapshot = snapshot
            self.fetched_at = now
        self.redraw()

    def announce(self, changes, limit=3):
        """Tell the user which jobs started or ended since the last refresh."""
        messages = [
            (sjobs.describe_change(job, event), severity)
            for job, event, severity in changes
        ]
        for message, severity in messages[:limit]:
            self.notify(
                message,
                severity="error" if severity == "error" else "information",
                timeout=10,
                markup=False,
            )
        if len(messages) > limit:
            self.notify(f"and {len(messages) - limit} more jobs changed", timeout=10)
        if messages and self.config.get("desktop_notifications"):
            text = (
                messages[0][0]
                if len(messages) == 1
                else (f"{len(messages)} jobs changed: {messages[0][0]}, …")
            )
            self.desktop_notify(text)

    def desktop_notify(self, text):
        """A desktop notification through the terminal (OSC 9), where supported."""
        driver = getattr(self, "_driver", None)
        if driver is not None:
            clean = "".join(char for char in text if char.isprintable())
            driver.write(f"\x1b]9;sjobs: {clean}\x07")

    # Drawing ------------------------------------------------------------------

    def visible_jobs(self):
        jobs = self.snapshot["jobs"] if self.snapshot else []
        if self.filter_text:
            jobs = [job for job in jobs if matches(job, self.filter_text)]
        return jobs

    def redraw(self):
        self.redraw_summary()
        all_grouped = sjobs.group_jobs(self.snapshot["jobs"] if self.snapshot else [])
        grouped = sjobs.group_jobs(self.visible_jobs())
        focused = self.focused
        rows = {
            name: sjobs.group_tasks(
                name, sjobs.sort_jobs(name, grouped[name]), self.expanded
            )
            for name in sjobs.CATEGORIES
        }
        self.update_layout(rows)
        fields = {
            name: sjobs.category_fields(name, self.show_user, self.columns)
            for name in sjobs.CATEGORIES
        }
        if self.tabbed:
            widths = {
                name: sjobs.fit_widths(fields[name], rows[name])
                for name in sjobs.CATEGORIES
            }
        else:
            # Stacked tables share the widths of their common leading columns.
            shown = {name: (fields[name], rows[name]) for name in rows if rows[name]}
            widths = sjobs.align_widths(shown)
        for name in sjobs.CATEGORIES:
            self.redraw_table(
                name, rows[name], len(all_grouped[name]), fields[name], widths.get(name)
            )
        if not isinstance(focused, DataTable | Input) or not focused.display:
            self.focus_first_table()

    def update_layout(self, rows):
        """Stacked tables while they fit, else tabs with one category each."""
        needed = sum(len(r) + 3 for r in rows.values() if r)
        height = self.query_one("#tables").size.height or self.size.height
        self.tabbed = self.show_user or needed > height + self.TAB_THRESHOLD_MARGIN
        tabs = self.query_one("#tabs", Tabs)
        tabs.set_class(self.tabbed, "shown")
        self.query_one("#tables").set_class(self.tabbed, "tabbed")
        for name, spec in sjobs.CATEGORIES.items():
            tab = tabs.query_one(f"#tab-{name}", Tab)
            count = sum(len(row.get("_tasks", ())) or 1 for row in rows[name])
            tab.label = f"{spec['title']} {count}"
        if self.tabbed and not rows[self.active]:
            self.active = next((n for n in rows if rows[n]), self.active)
        if self.tabbed and tabs.active != f"tab-{self.active}":
            tabs.active = f"tab-{self.active}"

    def redraw_summary(self):
        summary = self.query_one("#summary", Static)
        if self.snapshot is None and self.error is None:
            summary.update(Text("Fetching jobs…", style="dim"))
            return
        text = Text()
        if self.snapshot is not None:
            grouped = sjobs.group_jobs(self.snapshot["jobs"])
            text = sjobs.render_summary(
                grouped, self.snapshot, self.host, self.fetched_at, self.show_user
            )
        if self.error:
            if text:
                text.append("\n")
            text.append(f"Error: {self.error}", style="bold red")
        summary.update(text)

    def redraw_table(self, name, rows, total, fields, widths):
        label = self.query_one(f"#label-{name}", Static)
        table = self.query_one(f"#table-{name}", DataTable)
        selected = self.selected_job(table)
        self.rows[name] = rows

        visible = bool(rows) and (not self.tabbed or name == self.active)
        table.display = visible
        label.display = visible and not self.tabbed
        shown = sum(len(row.get("_tasks", ())) or 1 for row in rows)
        label.update(sjobs.category_title(name, shown, total))

        widths = widths or sjobs.fit_widths(fields, rows)
        table.clear(columns=True)
        for key, header in fields:
            justify = "right" if key in sjobs.RIGHT_ALIGNED else "left"
            table.add_column(Text(header, justify=justify), key=key, width=widths[key])
        for row in rows:
            table.add_row(
                *(sjobs.job_cell(row, key, widths) for key, _header in fields)
            )
        if selected is not None:
            for index, row in enumerate(rows):
                if row["job_id"] == selected["job_id"]:
                    table.move_cursor(row=index, scroll=False)
                    break

    # Selection ----------------------------------------------------------------

    def tables(self):
        return [
            self.query_one(f"#table-{name}", DataTable) for name in sjobs.CATEGORIES
        ]

    def focus_first_table(self):
        for table in self.tables():
            if table.display:
                table.focus()
                return

    def selected_job(self, table=None):
        """The row under the cursor of the given (or the focused) table."""
        table = table or self.focused
        if not isinstance(table, DataTable) or not table.row_count:
            return None
        name = table.id.removeprefix("table-")
        rows = self.rows.get(name, [])
        if 0 <= table.cursor_row < len(rows):
            return rows[table.cursor_row]
        return None

    def move_between_tables(self, table, step):
        """Continue from the end of one stacked table into the next one."""
        if self.tabbed:
            return False
        visible = [
            other for other in self.tables() if other.display and other.row_count
        ]
        if table not in visible:
            return False
        index = visible.index(table) + step
        if not 0 <= index < len(visible):
            return False
        target = visible[index]
        target.focus()
        target.move_cursor(row=0 if step > 0 else target.row_count - 1)
        return True

    def on_tabs_tab_activated(self, event: Tabs.TabActivated):
        name = event.tab.id.removeprefix("tab-")
        if self.tabbed and name != self.active:
            self.active = name
            self.redraw()
            self.focus_first_table()

    # Actions ------------------------------------------------------------------

    def action_help(self):
        self.push_screen(HelpScreen(self.KEYS, self.interval))

    def action_show_category(self, name):
        if self.tabbed:
            self.active = name
            self.redraw()
        table = self.query_one(f"#table-{name}", DataTable)
        if table.display:
            table.focus()

    def action_refresh_now(self):
        if time.monotonic() - self.last_request < MIN_MANUAL_REFRESH:
            self.notify("Refreshed just now; try again in a few seconds.")
            return
        self.timer.reset()
        self.refresh_jobs()

    def action_toggle_all(self):
        if self.username:
            self.notify(f"Showing --user {self.username}; restart without it.")
            return
        self.show_all = not self.show_all
        self.timer.reset()
        self.refresh_jobs()

    def action_toggle_array(self):
        job = self.selected_job()
        array_id = None
        if job is not None and job.get("_tasks"):
            array_id = job["_tasks"][0]["_array_id"][0]
            self.expanded.add(array_id)
        elif job is not None and job.get("_array_id"):
            array_id = job["_array_id"][0]
            self.expanded.discard(array_id)
        if array_id is not None:
            self.redraw()
            self.select_array(array_id)

    def select_array(self, array_id):
        """Put the cursor on the first row of an array job."""
        table = self.focused
        if not isinstance(table, DataTable):
            return
        rows = self.rows[table.id.removeprefix("table-")]
        for index, row in enumerate(rows):
            tasks = row.get("_tasks") or [row]
            if tasks[0].get("_array_id") and tasks[0]["_array_id"][0] == array_id:
                table.move_cursor(row=index)
                return

    def action_columns(self):
        def chosen(columns):
            self.columns = columns
            self.redraw()

        self.push_screen(ColumnChooser(self.columns), chosen)

    def action_filter(self):
        box = self.query_one("#filter", Input)
        box.add_class("shown")
        box.focus()

    def action_clear_filter(self):
        box = self.query_one("#filter", Input)
        box.value = ""
        box.remove_class("shown")
        self.filter_text = ""
        self.redraw()
        self.focus_first_table()

    def on_input_changed(self, event: Input.Changed):
        self.filter_text = event.value.strip()
        self.redraw()

    def on_input_submitted(self, _event: Input.Submitted):
        if not self.filter_text:
            self.query_one("#filter", Input).remove_class("shown")
        self.focus_first_table()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """Enter (or a click on the highlighted row) expands an array job or
        opens the details."""
        job = self.selected_job(event.data_table)
        if job is not None and job.get("_tasks"):
            self.action_toggle_array()
        else:
            self.show_details(job)

    def action_details(self):
        self.show_details(self.selected_job())

    def show_details(self, job):
        if job is not None:
            self.push_screen(JobDetail(job))

    def action_log(self):
        job = self.selected_job()
        if job is not None and job.get("_tasks"):
            job = job["_tasks"][0]
        if job is None:
            self.notify("Select a job first.", severity="warning")
        elif not job["_output"]:
            self.notify("Slurm reports no output file for this job.")
        else:
            self.push_screen(LogScreen(job, self.host))

    def action_cancel_job(self):
        job = self.selected_job()
        if job is None:
            self.notify("Select a job first.", severity="warning")
            return

        def answered(confirmed):
            if confirmed:
                self.cancel(job)

        self.push_screen(ConfirmCancel(job), answered)

    @work(thread=True, group="cancel")
    def cancel(self, job):
        try:
            sjobs.run_scancel(job["job_id"], self.host)
        except click.ClickException as exc:
            message = exc.format_message()
            self.call_from_thread(self.notify, message, severity="error", markup=False)
            return
        self.call_from_thread(self.notify, f"Cancelled job {job['job_id']}.")
        self.call_from_thread(self.refresh_jobs)


def run_tui(**options):
    """Run the interactive watcher until the user quits."""
    SjobsApp(terminal_colors=query_terminal_colors(), **options).run()
