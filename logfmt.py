"""Reformat journald lines as the plain text log lines the bot writes.

``./selfbot-ctl.sh logs`` pipes journalctl through this, so a peek reads the same whether an entry was
written before or after the switch to text logs. Anything that is not ours passes through untouched.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime

# journald prefixes each line with "Nov 24 23:49:35 host python[pid]: "; the record starts at the first
# object that carries our "time" field, so a message that merely contains a brace is left alone.
_RECORD_START = re.compile(r'\{"time": "')
_RECORD_KEYS = frozenset({"time", "level", "logger", "message", "exception"})
# The same prefix, so a text line can drop the clock journald already printed.
_JOURNAL_PREFIX = re.compile(r"^(?P<prefix>.+?python[\d.]*\[\d+\]: )(?P<body>.*)$")
_TEXT_CLOCK = re.compile(r"^\d\d:\d\d:\d\d ")


def _clock(value: object) -> str:
    """The record's local wall-clock time as ``HH:MM:SS``, or an empty string when it is missing or unreadable."""
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ""


def _format_record(prefix: str, payload: str) -> str | None:
    """Render one of our JSON records as a text line, or ``None`` when ``payload`` is not one."""
    try:
        record = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("message"), str):
        return None
    level = str(record.get("level", "INFO"))
    name = str(record.get("logger", "")).removeprefix("bombagif.")
    body = f"{level:<7} {name:<10} {record['message']}"
    clock = _clock(record.get("time"))
    if clock and not prefix:  # journald already printed the time in its own prefix
        body = f"{clock} {body}"
    for key, value in record.items():
        if key not in _RECORD_KEYS and value is not None:
            body += f" {key}={value}"
    if record.get("exception"):
        body += "\n" + str(record["exception"])
    return f"{prefix}{body}\n"


def format_line(line: str) -> str:
    """Return ``line`` as a readable log line, or unchanged when it is not one of ours."""
    text = line.rstrip("\n")
    match = _RECORD_START.search(text)
    if match is not None:
        rendered = _format_record(text[: match.start()], text[match.start() :])
        if rendered is not None:
            return rendered
    prefixed = _JOURNAL_PREFIX.match(text)
    if prefixed is not None:  # a text line: journald's prefix already carries the clock
        return f"{prefixed['prefix']}{_TEXT_CLOCK.sub('', prefixed['body'], count=1)}\n"
    return line


def main() -> None:
    """Reformat stdin line by line, flushing each one so ``logs -f`` keeps streaming."""
    for line in sys.stdin:
        sys.stdout.write(format_line(line))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
