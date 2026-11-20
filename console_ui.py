"""Terminal UI for the Bombagif self-bot: boot banner, live status panel, pretty logs.

Discord-free on purpose (the adapter passes plain values in), so every renderer can be tested
by rendering to text. Needs ``rich`` (see requirements-selfbot.txt).
"""

from __future__ import annotations

from typing import Any

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

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
