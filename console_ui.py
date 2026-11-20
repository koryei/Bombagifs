"""Terminal UI for the Bombagif self-bot: boot banner, live status panel, pretty logs.

Discord-free on purpose (the adapter passes plain values in), so every renderer can be tested
by rendering to text. Needs ``rich`` (see requirements-selfbot.txt).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Literal

from rich import box
from rich.columns import Columns
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.traceback import Traceback

from selfbot_logic import BotStats

BLURPLE = "#5865F2"  # the colour of Bombagif's embeds
BOMB = "#FF8A3D"

THEME = Theme(
    {
        "brand": f"bold {BLURPLE}",
        "accent": f"bold {BOMB}",
        "label": f"bold {BLURPLE}",
        "value": "white",
        "muted": "grey58",
        "ok": "bold green",
        "warn": "bold yellow",
        "bad": "bold red",
        "info": BLURPLE,
    }
)

LOGO = r"""
    ____  ____  __  _______  ___   ________________
   / __ )/ __ \/  |/  / __ )/   | / ____/  _/ ____/
  / __  / / / / /|_/ / __  / /| |/ / __ / // /_
 / /_/ / /_/ / /  / / /_/ / ___ / /_/ // // __/
/_____/\____/_/  /_/_____/_/  |_\____/___/_/
""".strip("\n")


def create_console(**options: Any) -> Console:
    """A console that carries the Bombagif theme (``options`` go to ``rich.console.Console``)."""
    return Console(theme=THEME, **options)


def _logo() -> Text:
    """The logo with a blurple-to-orange gradient, one colour per line."""
    colours = ["#5865F2", "#7A6BEE", "#A07AD6", "#D58A8F", "#FF8A3D"]
    lines = LOGO.splitlines()
    text = Text()
    for index, line in enumerate(lines):
        text.append(line + ("\n" if index < len(lines) - 1 else ""), style=f"bold {colours[min(index, len(colours) - 1)]}")
    return text


class BootBanner:
    """Startup panel that adapts to the terminal: logo beside, above, or (when tiny) without the facts."""

    def __init__(self, info: dict[str, str]) -> None:
        self.info = info

    def _facts(self) -> Table:
        facts = Table.grid(padding=(0, 2))
        facts.add_column(style="label", justify="right", no_wrap=True)
        facts.add_column(style="value", overflow="fold")
        for label, value in self.info.items():
            facts.add_row(label, str(value))
        return facts

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        logo_width = max(len(line) for line in LOGO.splitlines())
        facts = self._facts()
        facts_width = measure_renderable(console, options, facts)
        inner = options.max_width - 6  # panel border + padding
        body: RenderableType
        if inner >= logo_width + 4 + facts_width:
            body = Table.grid(padding=(0, 4))
            body.add_row(_logo(), facts)
        elif inner >= logo_width:
            body = Group(_logo(), Text(""), facts)
        else:
            body = facts
        yield Panel(
            body,
            border_style="brand",
            box=box.ROUNDED,
            title="[brand]Bombagif self-bot is online[/brand]",
            subtitle="[muted]DM me first, then ping me on any media[/muted]",
            padding=(1, 2),
        )


def measure_renderable(console: Console, options: ConsoleOptions, renderable: RenderableType) -> int:
    """The natural width of a renderable."""
    return Measurement.get(console, options, renderable).maximum


def render_boot_banner(info: dict[str, str]) -> BootBanner:
    """A neofetch-style startup panel: logo on the left, key facts on the right."""
    return BootBanner(info)


Status = Literal["connecting", "online", "paused"]

_STATUS_VIEW: dict[str, tuple[str, str, str]] = {
    "connecting": ("\u25cc", "connecting", "warn"),
    "online": ("\u25cf", "online", "ok"),
    "paused": ("\u23f8", "CAPTCHA pause, see the log", "bad"),
}
_LEVEL_VIEW: dict[str, tuple[str, str]] = {
    "ok": ("\u2714", "ok"),
    "fail": ("\u2716", "bad"),
    "warn": ("!", "warn"),
    "info": ("\u2022", "info"),
}


def _stat(label: str, value: int) -> Text:
    text = Text()
    text.append(f"{label} ", style="muted")
    text.append(str(value), style="accent")
    return text


def render_dashboard(stats: BotStats, *, status: Status, allowlist_size: int, now: float | None = None) -> Panel:
    """The live status panel: state line, counters, then the recent activity feed."""
    icon, label, style = _STATUS_VIEW[status]
    header = Text()
    header.append(f"{icon} {label}", style=style)
    header.append("   uptime ", style="muted")
    header.append(stats.uptime(now), style="value")
    header.append("   ")
    header.append(str(allowlist_size), style="accent")
    header.append(" allowlisted", style="muted")

    counters = Columns(
        [
            _stat("converted", stats.conversions_ok),
            _stat("failed", stats.conversions_failed),
            _stat("triggers", stats.triggers),
            _stat("rate limited", stats.rate_limited),
            _stat("new users", stats.registered),
            _stat("friends", stats.friends_accepted),
        ],
        padding=(0, 3),
        expand=True,
    )

    feed = Table.grid(padding=(0, 1), expand=True)
    feed.add_column(style="muted", no_wrap=True)
    feed.add_column(no_wrap=True)
    feed.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
    if not stats.activity:
        feed_body: RenderableType = Text("Waiting for the first DM or ping\u2026", style="muted")
    else:
        for entry in stats.activity:
            glyph, glyph_style = _LEVEL_VIEW[entry.level]
            feed.add_row(
                time.strftime("%H:%M:%S", time.localtime(entry.at)),
                Text(glyph, style=glyph_style),
                Text(entry.text, overflow="ellipsis", no_wrap=True),
            )
        feed_body = feed

    return Panel(
        Group(header, Text(""), counters, Text(""), Text("recent activity", style="label"), feed_body),
        title="[brand]live status[/brand]",
        border_style="brand",
        box=box.ROUNDED,
        padding=(1, 2),
    )


class Dashboard:
    """A Rich renderable that reads fresh in-memory state each time it is drawn (for ``rich.live.Live``)."""

    def __init__(
        self,
        stats: BotStats,
        get_status: Callable[[], Status],
        get_allowlist_size: Callable[[], int],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.stats = stats
        self.get_status = get_status
        self.get_allowlist_size = get_allowlist_size
        self.clock = clock

    def __rich__(self) -> Panel:
        return render_dashboard(
            self.stats,
            status=self.get_status(),
            allowlist_size=self.get_allowlist_size(),
            now=self.clock(),
        )


def render_shutdown_panel(stats: BotStats, now: float | None = None) -> Panel:
    """A one-line goodbye with the session totals."""
    line = Text(justify="center")
    line.append("Bombagif signed off", style="brand")
    line.append(
        f"  \u2022  {stats.conversions_ok} converted, {stats.conversions_failed} failed  \u2022  up {stats.uptime(now)}",
        style="muted",
    )
    return Panel(line, border_style="brand", box=box.ROUNDED)


_LEVEL_STYLES = {
    "DEBUG": ("DEBUG", "muted"),
    "INFO": ("INFO", "info"),
    "WARNING": ("WARN", "warn"),
    "ERROR": ("ERROR", "bad"),
    "CRITICAL": ("FATAL", "bold white on red"),
}
# record attribute -> label shown in the line (same fields the JSON formatter emits)
_EXTRA_LABELS = (
    ("user_id", "user_id"),
    ("guild_id", "guild_id"),
    ("attachment_name", "file"),
    ("attempt", "attempt"),
    ("trace_id", "trace"),
)


class PrettyLogHandler(logging.Handler):
    """Print log records as one coloured, human-readable line (``HH:MM:SS LEVEL logger message k=v``)."""

    def __init__(self, console: Console) -> None:
        super().__init__()
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            label, style = _LEVEL_STYLES.get(record.levelname, (record.levelname, "value"))
            line = Text(no_wrap=False, overflow="fold")
            line.append(time.strftime("%H:%M:%S", time.localtime(record.created)) + "  ", style="muted")
            line.append(f"{label:<5}", style=style)
            line.append("  ")
            line.append(f"{record.name.removeprefix('bombagif.'):<10}", style="muted")
            line.append("  ")
            line.append(record.getMessage())  # Text.append never parses markup
            for attribute, shown in _EXTRA_LABELS:
                value = getattr(record, attribute, None)
                if value is not None:
                    value = str(value)[:8] if attribute == "trace_id" else value
                    line.append(f"  {shown}={value}", style="muted")
            self.console.print(line, highlight=False, soft_wrap=True)
            if record.exc_info and record.exc_info[0] is not None:
                self.console.print(Traceback.from_exception(*record.exc_info, max_frames=8, width=min(self.console.width, 110)))
        except Exception:  # logging must never take the bot down
            self.handleError(record)


def configure_pretty_logging(console: Console, level: str) -> None:
    """Route all logging to the console as pretty lines (replacing any existing handlers)."""
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(PrettyLogHandler(console))
    root.setLevel(getattr(logging, level, logging.INFO))
