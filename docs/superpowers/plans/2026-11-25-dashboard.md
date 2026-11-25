# Bombagif dashboard

## Primary goal

The operator opens one command and, in a single screen, sees whether the bot is healthy, what the clip scout is
doing, what happened recently, and whether anything needs them — without reading a log line or knowing what
`clip=False funny=0.0 via inception (decide: …)` means.

## Phase 0 — Documentation discovery (findings)

Read before planning; every later phase copies from these.

| Fact | Source |
| --- | --- |
| Rich is already a self-bot dependency; theme, logo, console factory | `console_ui.py:20-60` (`THEME`, `create_console`) |
| Existing live panel: state line, counters, activity feed | `console_ui.py:137-256` (`render_dashboard`, `Dashboard`) |
| Counters the bot already keeps | `selfbot_logic.py:548-587` (`BotStats`) |
| Activity feed entries (level + text) | `selfbot_logic.py:539-573` (`ActivityEntry`, `stats.note`) |
| Connection state | `selfbot.py:252-256` (`ui_status`) |
| Scout mode / judge / can-post | `selfbot.py:258-265` (`scout_view`) |
| Scout facts (chats, reviews, taste) | `selfbot.py:267-286` (`_scout_info`) |
| Chat **names** (not ids) | `data/scout-chats.json` — `{"<id>": {"name": "💬｜general", "at": …}}` |
| Atomic mode-600 JSON write | `private_files.py:26-28` (`write_private_json`) |
| Service facts for the shell command | `selfbot-ctl.sh:414-420` (`cmd_status`), `sc_read` at `:31-36` |
| Log-line reformatter (reuse its parsing ideas) | `logfmt.py` |

**Allowed APIs:** `rich.live.Live`, `rich.panel.Panel`, `rich.table.Table`, `rich.text.Text`, `rich.columns.Columns`,
`rich.box.ROUNDED`, `rich.console.Console`, `private_files.write_private_json`, `selfbot_logic.BotStats`,
`console_ui.create_console`, `console_ui.THEME`.

**Anti-patterns:** do not parse the journal for state (the bot knows the truth); do not import `discord` in the
renderer; do not print chat ids where a name exists; do not invent a second colour language.

## Phase 1 — The bot writes `data/status.json`

**How this serves the primary goal:** the dashboard can only be honest if it reads the bot's own state instead of
guessing from log text.

- `selfbot_logic.py`: `StatusSnapshot` (frozen dataclass) + `snapshot_to_dict()`; `write_status(path, snapshot)`
  using `write_private_json`. Discord-free, so it is unit-testable.
- `selfbot.py`: a background task every 5 s building the snapshot from `stats`, `ui_status`, `scout_view`,
  `_scout_info`, `len(allowlist)`, `len(runtime.chats.list())`, `len(runtime.reviews)`; cancelled on close.
- Shape (v1): `at`, `version`, `state`, `account`, `zipline`, `uptime_s`, `allowlist`, `scout{mode,judge,can_post,
  chats,reviews_waiting,manual}`, `counters{…}`, `activity[{at,level,text}]`.
- **Verify:** unit test writes a snapshot and reads it back; adapter test asserts the task writes a file with the
  expected keys; `python -m unittest tests.test_status_snapshot`.
- **Guard:** never write tokens or message text into the snapshot.

## Phase 2 — `dashboard.py` renders it

**How this serves the primary goal:** this is the screen itself — plain English, chat names, colour that means
something.

- `read_status(path)` → `(snapshot | None, age_seconds)`; stale beyond 30 s is reported, not hidden.
- `render_dashboard(snapshot, *, service, now, width)` → Rich renderable, four bands:
  1. **Header** — state dot, account, Zipline host, allowlist size, version, uptime.
  2. **Clip scout** — mode, judge, chats on, reviews waiting.
  3. **This session** — judged / passed / posted / rejected / errors.
  4. **Recent activity** — `HH:MM  icon  plain sentence`, chat names resolved from `scout-chats.json`.
  5. **Needs you** — derived only: CAPTCHA pause, judge paused, stale status, service not active.
- **Verify:** render to text at 80 and 120 columns; assert no `clip=`, no bare 18-digit ids, no `trace_id`.
- **Guard:** every number is labelled with its window ("this session"), so nothing is ambiguous.

## Phase 3 — `./selfbot-ctl.sh dashboard`

**How this serves the primary goal:** one command, from the same place as `status` and `logs`.

- `dashboard.py --watch` owns the refresh loop (`rich.live.Live`, 2 s), `--once` prints one frame (for tests and
  for `| less`), `--systemctl` overrides the binary for tests.
- `selfbot-ctl.sh`: `dashboard` subcommand + usage line; falls back to a clear message when the venv is missing.
- **Verify:** `bash tests/test_selfbot_ctl.sh`; `dashboard.py --once` against the repo's `data/`.
- **Guard:** Ctrl-C leaves the terminal usable (no live region left behind).

## Phase 4 — Verification

**How this serves the primary goal:** proves the screen is readable, not just that it renders.

- Full suite green; `test_selfbot_ctl.sh` and `test_installer.sh` green.
- Render at 80×24 and 120×40, read both, confirm no jargon and no ids.
- Run `--once` against a real `data/` directory with a stale and a fresh snapshot.

## Not now

Web dashboard · historical charts · per-day counters · remote access · alerting.
