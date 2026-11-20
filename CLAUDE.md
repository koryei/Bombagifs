# Bombagif

Discord bot that converts images/videos (PNG, JPG, AVIF, WEBP, SVG, MP4, WebM) to optimized GIFs
(~1 MB target), uploads them to the operator's own Zipline instance, and returns a link.

## Layout

- `main.py` — v1: user-installed slash-command app (`/gif`, up to 5 files), uses `discord.py`.
- `bombagif_core.py` — discord-free shared pipeline: limits, errors, `Settings`/`read_settings`, image/SVG/video
  conversion, `download_url`, `upload_to_zipline`, `MediaSource`, `convert_and_upload`, `is_discord_media_url`,
  `sniff_suffix`, logging. Never import `discord` here.
- `selfbot_logic.py` — discord-free v2 logic: `AllowlistStore`, `RateLimiter`, `decide`, `collect_media`,
  `format_results`, reply copy. Never import `discord` here.
- `selfbot.py` — v2: thin `discord.py-self` adapter (`SelfBot`); run from `.venv-selfbot`
  (`requirements-selfbot.txt`). Note `SelfBot.config` holds `Settings` (`Client.settings` is a library property).
- `console_ui.py` — discord-free Rich terminal UI for the self-bot (boot banner, live status panel, pretty log
  handler, `TerminalUi`, `demo()`); needs `rich` (in `requirements-selfbot.txt`). Preview: `python console_ui.py`.
- `tests/` — `python3 -m unittest discover -s tests -v` (system env, `discord.py`);
  `.venv-selfbot/bin/python -m unittest tests.test_selfbot_adapter -v` for the self-bot adapter (skipped elsewhere);
  `bash tests/test_installer.sh`.
- `install.sh`, `Dockerfile`, `compose.yaml` — deployment for the app only (the self-bot is run manually).

## Roadmap

- **v1 (shipped):** `/gif` user-installed app.
- **v2 (this branch, `v2-selfbot`):** self-bot front door. Spec: `docs/superpowers/specs/2026-11-18-selfbot-design.md`;
  plan: `PLAN.md`. Consent model: only users who DMed the account first are served; everyone else is ignored silently.
- **v3 (planned, not started):** autonomous "clip scout" — spots clip-worthy messages and renders them as clips.
  Proposed (not yet agreed with the owner): only allowlisted people's messages are eligible, clips are private by
  default (never auto-posted), hard opt-out. Needs its own brainstorm and spec.

## Key decisions

- `discord.py-self` and `discord.py` both install as `discord`, so the self-bot is a separate process and venv.
- Embedded media is fetched only via Discord proxy URLs (SSRF-safe); bare links in a ping's own text are not fetched.
- Small atomic commits; every commit keeps the suite green.

## v2.1 (branch `v2.1-captcha-guard`)

Zipline failures now log status + error snippet; `CaptchaGuard` (persisted in `data/captcha-blocked.json`) pauses all
account actions on `discord.CaptchaRequired` until the file is deleted; opt-in `SELFBOT_AUTO_ACCEPT_FRIENDS` (paced,
capped, guard-aware, never registers anyone).

## v2.2 (branch `v2.2-terminal-ui`, stacked on v2.1)

Pretty terminal for the self-bot: `BotStats` counters + activity feed in `selfbot_logic.py`, `console_ui.py`
renderers, `SELFBOT_UI=auto|pretty|json` (auto = pretty only on a TTY, JSON otherwise so systemd/Docker are unchanged).

## Status

v2 implemented on branch `v2-selfbot` (core extraction, allowlist, rate limiter, decision rules, media
collection, reply copy, adapter, embeds, docs). Live smoke test with a real alt-account token is still manual.
Known limit: a ping whose own bare link has not been embedded yet at message time gets the no-media reply.
