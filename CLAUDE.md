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
- `private_files.py` — atomic mode-600 JSON write + append helpers for the data stores (discord-free).
- Clip scout (v3), all discord-free unless noted: `scout_config.py` (env -> `ScoutConfig`), `scout_buffer.py` (in-memory
  message buffer: versions, deletes, laugh signals, runs, windows), `scout_safety.py` (eligibility + the local sensitive
  filter that the judge can never override), `scout_stores.py` (opt-out list, enabled chats, clip ledger),
  `scout_judge.py` (anonymised windows, verdict validation, Mercury Decide + chat-completions clients, `InceptionJudge`,
  `OpenAICompatJudge`, `JudgeChain`), `scout.py` (the `Scout` orchestrator, `ScoutRuntime`, notice text, `!scout`
  parsing), `clip_render.py` (Pillow card renderer + markdown/emoji), `clip_assets.py` (asset host allowlist, card
  building), `scout_discord.py` (**imports discord**: message mapping), `assets/fonts/` (Noto, SIL OFL).
- v3.1 (teaching + "clip that"): `scout_taste.py` (`TasteNotes`, `TasteStore`, `TasteBook`, `tune_report`),
  `scout_manual.py` (`ManualGate`: refusals + limits for manual clips); `scout.py` also holds the review queue
  (`ReviewItem`, `Scout.rate`), `HELP_OWNER`/`HELP_USER`, and the `!scout` parser. Files in `SELFBOT_DATA_DIR`:
  `scout-taste.txt`, `taste.jsonl`, `optout.json`, `scout-chats.json`, `clips.jsonl`, `scout-blocklist.txt`.
- Spoken triggers: `scout_trigger.py` (`parse_trigger`: sure/unsure phrases + typo tolerance, `pick_targets`),
  `scout_intent.py` (`IntentClassifier`: the AI check for unsure ones; one line of chat text, as data, daily cap).
- `console_ui.py` — discord-free Rich terminal UI for the self-bot (boot banner, live status panel, pretty log
  handler, `TerminalUi`, `demo()`); needs `rich` (in `requirements-selfbot.txt`). Preview: `python console_ui.py`.
- `tests/` — `python3 -m unittest discover -s tests -v` (system env, `discord.py`);
  `.venv-selfbot/bin/python -m unittest tests.test_selfbot_adapter -v` for the self-bot adapter (skipped elsewhere);
  `bash tests/test_installer.sh`.
- `install.sh`, `Dockerfile`, `compose.yaml` — deployment for the app only.
- `selfbot-ctl.sh` — VM deployment for the self-bot (Ubuntu/Debian + systemd): `install`, `update` (ff-only, deps only when
  requirements changed, smoke import, health check, auto rollback), `auto on|off`, `status`, `logs`, `doctor`. Test: `bash tests/test_selfbot_ctl.sh`
  (fake systemctl + throwaway git repos; the real `install` step needs a real VM and is not covered).

Formats: `ALLOWED_TYPES` / `VIDEO_SUFFIXES` in `bombagif_core.py` (images: PNG, JPG, WEBP, AVIF, BMP, TIFF, SVG; video: MP4, MOV, WebM, MKV, AVI, M4V, 3GP, OGV, WMV via FFmpeg). Not supported: HEIC, GIF input.
A "nothing to convert" reply names the file types it couldn't read (`no_media_reply`).

## Roadmap

- **v1 (shipped):** `/gif` user-installed app.
- **v2 (this branch, `v2-selfbot`):** self-bot front door. Spec: `docs/superpowers/specs/2026-11-18-selfbot-design.md`;
  plan: `PLAN.md`. Consent model: only users who DMed the account first are served; everyone else is ignored silently.
- **v3 (implemented on branch `v3-scout`):** the clip scout. Spec: `docs/superpowers/specs/2026-11-19-clip-scout-design.md`;
  plan: `PLAN.md`. **Not yet run against real chats**: the live `dry-run` (needs `INCEPTION_API_KEY` +
  `SELFBOT_OWNER_ID` in `.env`) is the next step before turning `SELFBOT_SCOUT_MODE=on`.

## Key decisions

- Owner commands are bare (`!on`, `!off`, `!status`, `!list`, `!taste`, `!examples`, `!tune`, `!help`); the old `!scout <cmd>`
  form still parses. Only the owner's messages are ever read as commands.

- Clip scout: scouting is **off everywhere** until the owner runs `!on` in a chat; `dry-run` before `on`; the
  provider payload is anonymised text only; the local safety filter, opt-outs, cooldowns and caps always win over the
  judge; the heuristic (no-LLM) judge never posts. Inception's API ends 2026-12-28, so the judge is a swappable chain.

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

## v2.2.1 (branch `v2.2.1-ui-fixes`, stacked on v2.2)

Found in the first live run: Ctrl-C left a stray panel border (a full-width live panel makes the terminal's `^C` echo wrap,
breaking Rich's erase maths), so the live panel is now 3 columns narrower, non-transient, and its last frame stays.
`discord.*` INFO chatter is hidden in pretty mode (shown at LOG_LEVEL=DEBUG). Zipline 401/403/404/413 rejections log a hint.
Verified with a pty + `pyte` screen emulator harness (kept out of the repo).

## v3 clip scout (branch `v3-scout`)

Built per the spec in 18 plan tasks (see `PLAN.md`): buffer -> candidate runs -> Decide screen -> Mercury 3 final call ->
local gates -> pure Pillow renderer -> post with a who-was-clipped + `stop` notice. Extras beyond the spec are listed in
the spec's "Implementation notes". Strangers who ping the account now get a help reply. `stop` in any DM opts out of
clipping and conversions; `start` opts back in.

## v3.2 spoken triggers (branch `clip-triggers`)

In enabled chats, "clip that" and a reply "gif that" work with no ping. Known phrases are matched locally;
typos ("clkip that LMFAII") go to one cheap Mercury Decide call (two yes/no questions; a chat-model fallback after Inception shuts down) that sees
only that line (as data). Refusals are silent (nobody pinged);
pings keep their old path. Settings: `SCOUT_TRIGGERS=ai|phrases|off`, `SCOUT_TRIGGER_ANYONE`, `SCOUT_TRIGGER_AI_PER_DAY`,
`SCOUT_TRIGGER_MAX_AGE_SECONDS`. Not yet run in a real chat.

## v3.1 teaching (branch `v3.1-teach`, stacked on v3)

Spec: the "v3.1 addendum" in the clip scout spec; plan: `PLAN.md`. Reply-ping a text message to clip it ("clip that"),
owner taste notes, review DMs and posted clips the owner rates by *replying* `good`/`bad`/`skip` (the account never adds reactions: rate limits) that become few-shot examples, `!tune`, and `!help`. Chat
text can never reach the system prompt (examples ride in the user JSON as data); only the owner teaches.

## Status

v3.1 (teaching, "clip that", `!help`) is implemented on `v3.1-teach`, stacked on the unmerged `v3-scout`; neither has been
run against real chats yet (the dry-run is the next step).

v3 (clip scout) is implemented on branch `v3-scout` (not merged or released); the live dry-run is still manual.
v2, v2.1 (CAPTCHA guard, auto-accept, terminal UI) are merged to `main` and released (tags `v2`, `v2.1`). v2 was implemented on branch `v2-selfbot` (core extraction, allowlist, rate limiter, decision rules, media
collection, reply copy, adapter, embeds, docs). Live smoke test with a real alt-account token is still manual.
Known limit: a ping whose own bare link has not been embedded yet at message time gets the no-media reply.
