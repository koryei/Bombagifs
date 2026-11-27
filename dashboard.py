"""The at-a-glance dashboard: one screen, read from ``data/status.json``.

The bot writes that file every few seconds (``selfbot_logic.write_status``), so this module never parses
logs and never imports ``discord``. ``./selfbot-ctl.sh dashboard`` runs it; ``--once`` prints a single frame.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from console_ui import create_console

STALE_AFTER_S = 30.0  # the bot writes every 5 s; this much silence means stopped or hung
REFRESH_S = 2.0

_STATE_VIEW: dict[str, tuple[str, str, str]] = {
    "online": ("\u25cf", "online", "ok"),
    "connecting": ("\u25cc", "connecting", "warn"),
    "paused": ("\u23f8", "paused", "bad"),
}
_LEVEL_VIEW: dict[str, tuple[str, str]] = {
    "ok": ("\u2714", "ok"),
    "fail": ("\u2716", "bad"),
    "warn": ("!", "warn"),
    "info": ("\u2022", "info"),
}


def _duration(seconds: float) -> str:
    """``2d 04h`` / ``3h 12m`` / ``45s``: the same shape the bot's own uptime uses."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def read_status(path: str | Path) -> tuple[dict[str, Any] | None, float | None]:
    """The snapshot and its age in seconds, or ``(None, None)`` when the bot has not written one."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        return None, None
    try:
        snapshot = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(snapshot, dict):
        return None, None
    written = snapshot.get("at")
    age = max(0.0, time.time() - written) if isinstance(written, (int, float)) else None
    return snapshot, age


def service_state(service: str, *, systemctl: str = "systemctl") -> str:
    """``active``, ``inactive``, ``failed``… or ``unknown`` when systemd cannot be asked."""
    try:
        result = subprocess.run(
            [systemctl, "is-active", service], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (result.stdout or result.stderr).strip() or "unknown"


def _stat(label: str, value: object) -> Text:
    text = Text()
    text.append(f"{label} ", style="muted")
    text.append(str(value), style="accent")
    return text


def _state_view(snapshot: Mapping[str, Any] | None, service: str) -> tuple[str, str, str]:
    """What the header says. A stopped service wins: a bot that is not running cannot be online."""
    if service in ("inactive", "failed"):
        return ("\u25cb", "stopped", "bad")
    state = str(snapshot.get("state", "unknown")) if snapshot else "unknown"
    return _STATE_VIEW.get(state, ("\u25cb", "no status yet", "muted"))


def _header(snapshot: Mapping[str, Any] | None, *, version: str, service: str = "unknown") -> Panel:
    line = Text()
    icon, label, style = _state_view(snapshot, service)
    line.append(f"{icon} {label}", style=style)
    if snapshot:
        line.append("    ")
        line.append(str(snapshot.get("account", "?")), style="value")
        line.append("    zipline ", style="muted")
        line.append(str(snapshot.get("zipline", "?")), style="value")
        line.append("    ")
        line.append(str(snapshot.get("allowlist", 0)), style="accent")
        line.append(" allowlisted", style="muted")
    subtitle = f"{version} \u00b7 up {_duration(snapshot.get('uptime_s', 0))}" if snapshot else version
    return Panel(
        line,
        title="[brand]Bombagif[/brand]",
        subtitle=f"[muted]{subtitle}[/muted]",
        border_style="brand",
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _watching(scout: Mapping[str, Any]) -> Text:
    """Name the chats the scout is watching, or say how many when it has no names."""
    chats = int(scout.get("chats") or 0)
    names = [str(name) for name in (scout.get("chat_names") or []) if name]
    line = Text()
    if not names:
        line.append(f"{chats} chats on" if chats else "no chats on", style="value")
        return line
    shown = names[:3]
    line.append("watching ", style="muted")
    line.append(", ".join(name if len(name) <= 24 else name[:23] + "\u2026" for name in shown), style="value")
    hidden = chats - len(shown)
    if hidden > 0:
        line.append(f" +{hidden} more", style="muted")
    return line


def _scout_panel(snapshot: Mapping[str, Any] | None) -> Panel:
    scout = dict((snapshot or {}).get("scout") or {})
    counters = dict((snapshot or {}).get("counters") or {})
    body = Text()
    if scout.get("mode", "off") == "off":
        body.append("off", style="muted")
        body.append("  nothing is being watched", style="muted")
    else:
        body.append(str(scout.get("mode", "?")), style="accent")
        body.append("  \u00b7  judge ", style="muted")
        body.append(str(scout.get("judge", "?")), style="ok" if scout.get("can_post") else "warn")
        waiting = int(scout.get("reviews_waiting") or 0)
        if waiting:
            body.append("  \u00b7  ", style="muted")
            body.append(f"{waiting} reviews waiting", style="value")
        body.append("\n")
        body.append_text(_watching(scout))
        body.append("\n")
        body.append(f"judged {counters.get('judged', 0)}", style="muted")
        body.append("   ")
        body.append(f"passed {counters.get('passed', 0)}", style="muted")
        body.append("   ")
        body.append(f"posted {counters.get('posted', 0)}", style="muted")
        body.append("   ")
        body.append(f"rejected {counters.get('rejected', 0)}", style="muted")
    return Panel(body, title="[label]Clip scout[/label]", border_style="muted", box=box.ROUNDED, padding=(0, 1))


def _conversions_panel(snapshot: Mapping[str, Any] | None) -> Panel:
    counters = dict((snapshot or {}).get("counters") or {})
    grid = Table.grid(padding=(0, 3))
    grid.add_column()
    grid.add_column()
    grid.add_row(_stat("converted", counters.get("converted", 0)), _stat("failed", counters.get("failed", 0)))
    grid.add_row(_stat("triggers", counters.get("triggers", 0)), _stat("rate limited", counters.get("rate_limited", 0)))
    grid.add_row(_stat("new users", counters.get("new_users", 0)), _stat("friends", counters.get("friends", 0)))
    return Panel(grid, title="[label]Conversions[/label]", border_style="muted", box=box.ROUNDED, padding=(0, 1))


def _activity_panel(snapshot: Mapping[str, Any] | None) -> Panel:
    entries = list((snapshot or {}).get("activity") or [])
    if not entries:
        body: RenderableType = Text("nothing yet", style="muted")
    else:
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(style="muted", no_wrap=True)
        grid.add_column(no_wrap=True)
        grid.add_column(overflow="ellipsis", ratio=1)
        for entry in reversed(entries):  # newest first
            icon, style = _LEVEL_VIEW.get(str(entry.get("level", "info")), ("\u2022", "info"))
            at = entry.get("at")
            clock = time.strftime("%H:%M", time.localtime(at)) if isinstance(at, (int, float)) else "--:--"
            grid.add_row(clock, Text(icon, style=style), Text(str(entry.get("text", ""))))
        body = grid
    return Panel(body, title="[label]Recent activity[/label]", border_style="muted", box=box.ROUNDED, padding=(0, 1))


def needs_you(snapshot: Mapping[str, Any] | None, *, age: float | None, service: str) -> list[str]:
    """The short list of things only the operator can fix. Empty means everything is fine."""
    needs: list[str] = []
    if service not in ("active", "unknown"):
        needs.append(f"the service is {service}: ./selfbot-ctl.sh start")
    if snapshot is None:
        needs.append("no status yet: the bot writes one every 5 s once it is connected")
        return needs
    if age is not None and age > STALE_AFTER_S:
        needs.append(f"no status for {_duration(age)}: the bot may be stopped or hung")
    if snapshot.get("state") == "paused":
        needs.append("paused by a CAPTCHA: finish it in a browser, delete data/captcha-blocked.json, restart")
    scout = dict(snapshot.get("scout") or {})
    if scout.get("mode", "off") != "off" and not scout.get("can_post"):
        needs.append("the judge cannot post (heuristic only): set INCEPTION_API_KEY or SCOUT_LLM_*")
    waiting = int(scout.get("reviews_waiting") or 0)
    if waiting:
        needs.append(f"{waiting} review DM{'s' if waiting != 1 else ''} waiting for your rating")
    return needs


def _needs_panel(needs: list[str]) -> Panel:
    if not needs:
        body: RenderableType = Text("\u2714 nothing needs you", style="ok")
    else:
        body = Text("\n".join(f"!  {line}" for line in needs), style="warn")
    return Panel(body, title="[label]Needs you[/label]", border_style="muted", box=box.ROUNDED, padding=(0, 1))


def render_dashboard(
    snapshot: Mapping[str, Any] | None,
    *,
    age: float | None = None,
    service: str = "unknown",
    version: str = "",
) -> RenderableType:
    """The whole screen: who the bot is, what the scout is doing, what happened, what needs you."""
    needs = _needs_panel(needs_you(snapshot, age=age, service=service))
    if snapshot is None:  # a wall of zeros would only confuse: say what is missing and stop
        return Group(_header(None, version=version, service=service), needs)
    return Group(
        _header(snapshot, version=version, service=service),
        Columns([_scout_panel(snapshot), _conversions_panel(snapshot)], expand=True),
        _activity_panel(snapshot),
        needs,
    )


def _frame(status_path: Path, *, service: str, version: str, systemctl: str) -> RenderableType:
    snapshot, age = read_status(status_path)
    return render_dashboard(
        snapshot, age=age, service=service_state(service, systemctl=systemctl), version=version
    )


def main(argv: list[str] | None = None) -> int:
    """Print one frame (``--once``) or keep a full-screen dashboard up until Ctrl-C."""
    parser = argparse.ArgumentParser(description="Show the Bombagif dashboard.")
    parser.add_argument("--status", default="data/status.json", help="the snapshot the bot writes")
    parser.add_argument("--service", default="bombagif-selfbot", help="the systemd unit to report on")
    parser.add_argument("--systemctl", default="systemctl", help="the systemctl binary to ask")
    parser.add_argument("--version", default="", help="the version shown in the header")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    parser.add_argument("--interval", type=float, default=REFRESH_S, help="seconds between frames")
    args = parser.parse_args(argv)

    status_path = Path(args.status)
    console: Console = create_console()
    frame = lambda: _frame(status_path, service=args.service, version=args.version, systemctl=args.systemctl)  # noqa: E731
    if args.once or not console.is_terminal:  # a pipe gets one frame, not an empty alternate screen
        console.print(frame())
        return 0
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(frame(), refresh=True)
                time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
