# Bombagif self-bot — design

Status: awaiting approval. Size: a project (several independent commits, one new runtime).

## Intent

Bombagif today is a Discord *application*: people install it to their account and run `/gif`. This
adds a second front door. A dedicated Discord **user account** ("the self-bot", in the style of
[BombaclatAI](https://github.com/koryei/BombaclatAI)) that anyone can `@` with an image, or `@` while
replying to a message that contains media, and it answers in the same chat with the Zipline GIF
link. No install, no slash command.

Why: `/gif` needs a one-time install and a file picker. Pinging a friend-like account on an existing
message is the shortest possible path from "that image should be a GIF" to a link.

## Goals

- Ping with an attachment → reply with a GIF link.
- Ping while replying to a message with media → reply (to the *pinging* message) with the link.
- In a 1:1 DM with the account, media alone is enough (no ping needed).
- Only people who have **DMed the account first** are served ("consent by DM").
- Reuse the existing, tested conversion/upload pipeline; the slash-command bot keeps working untouched.
- Ship as small, atomic commits.

## Non-goals (v1)

- Autonomous behaviour, memory, LLM, personality (that is BombaclatAI's job, not this).
- Inbound `.gif` input (already unsupported by the pipeline today).
- Installer / Dockerfile / compose integration for the self-bot (README documents a manual venv run;
  packaging is a follow-up).
- Hosting the self-bot and the app in one process (impossible, see Constraints).

## Anti-goals (a failure even if it "works")

- Replying to strangers, or to anyone not on the allowlist, ever (not even an error message).
- Spamming a channel: one reply per trigger, bounded batch, per-user rate limit.
- Leaking the user token in logs, errors or replies.
- Regressing the 44 existing tests or the app's behaviour.

## Constraints

- `discord.py-self` and `discord.py` both install as `import discord`; they cannot share an
  environment. The self-bot therefore runs as its own process with its own requirements file and venv.
- Automating a user account violates Discord's ToS. Same accepted risk as BombaclatAI; README will
  say so plainly. The allowlist-by-DM rule exists partly to keep the account's behaviour low-profile.
- The conversion pipeline must stay discord-free after extraction so both runtimes (and tests that
  run under either library) can import it.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Extract `bombagif_core.py`; add `selfbot.py` as a separate entrypoint with its own venv | One refactor commit; two deployables sharing one tested pipeline |
| B | Self-bot inside `main.py` | Impossible: both libraries are `import discord` |
| C | Bombagif as an HTTP service, self-bot as a client | Cleanest long-term, but adds an inbound port and auth for no v1 benefit |

## Design

### Module layout

```
bombagif_core.py   discord-free: limits, errors, Settings reader, SVG/image/video conversion,
                   download_attachment (duck-typed: .filename/.content_type/.size/.url),
                   upload_to_zipline, JsonFormatter/configure_logging
main.py            slash-command app (imports from core; behaviour unchanged)
selfbot_logic.py   discord-free, fully unit-testable: allowlist store, trigger decision,
                   media-source collection from duck-typed message objects, rate limiter
selfbot.py         thin discord.py-self adapter: client, events, typing, replying
requirements-selfbot.txt
```

`main.py` re-exports the core names it used to define so `import main` call sites keep working.
Existing tests that patch `main._convert_image_once` / `main._convert_video_once` are repointed at
`bombagif_core.*` (patches on `main.shutil` / `main.importlib` keep working since they patch the
shared stdlib modules).

### Trigger rules (`selfbot_logic.decide`)

Inputs: message author id/bot flag, whether it is the self-bot's own message, channel kind
(1:1 DM / group DM / guild), whether the self-bot is mentioned, content, allowlist membership.

1. Ignore own messages, bot accounts, and system messages.
2. **1:1 DM, author not allowlisted** → *register*: add to allowlist, send a short welcome/how-to
   (reply). If that same message carries media it is also converted.
3. 1:1 DM, allowlisted: exact message `stop` (case-insensitive) → remove from allowlist, confirm.
   Otherwise, media present → convert (no ping needed).
4. Group DM or guild: only when the self-bot is mentioned **and** the author is allowlisted →
   convert. Non-allowlisted pings are ignored silently.

### Media sources (in priority order, first non-empty wins)

1. Attachments on the triggering message.
2. Attachments on the message it replies to (`message.reference`, fetched if not cached).
3. (Commit 4) Image/video embeds on either message: `embed.type` `image`/`gifv`/`video`, using the
   embed's media URL (Tenor `gifv` is MP4), plus linked image URLs in content. Remote fetches go
   through the same bounded downloader with an https-only, size-capped check.

Capped at `MAX_GIF_BATCH` (5) per trigger; extras are dropped with a note in the reply.
Unsupported types are skipped; if nothing usable remains, a one-line "no supported image or video
found" reply (only to allowlisted users).

### Reply flow

- `async with channel.typing()` while processing (concurrency limited by the existing
  semaphore-backed `_process_one` equivalent).
- One reply to the triggering message, `mention_author=False`, `AllowedMentions.none()`.
  Bare links so Discord embeds the GIF. Multiple files → one line per file: `name → link`.
- `UserFacingError` text is reported to the (allowlisted) requester; anything else logs with a
  trace id and replies with a generic apology.

### Persistence & config

- Allowlist: JSON file at `$SELFBOT_DATA_DIR/allowlist.json` (default `./data`), atomic write
  (tmp + rename), file mode 600, loaded at start. Stores user id → first-DM timestamp.
- Env: `SELFBOT_TOKEN` (required), plus the existing `ZIPLINE_URL`, `ZIPLINE_TOKEN`,
  `GIF_BACKGROUND`, `LOG_LEVEL`. `_read_settings` becomes parameterised by token variable name so
  the app still requires `DISCORD_TOKEN` and the self-bot requires `SELFBOT_TOKEN` (and neither
  demands the other).
- Rate limit: sliding window, 5 triggers per user per 60 s; excess triggers get no reply (not even
  an error) to avoid amplifying spam.

## Commit plan (atomic)

1. `docs`: this design + project `CLAUDE.md`.
2. `refactor`: extract `bombagif_core.py`, repoint tests, parameterise settings — no behaviour change.
3. `feat`: self-bot skeleton — config, allowlist store, logging, client login/ready, requirements file.
4. `feat`: triggers & reply flow (attachments + reply-to-message), rate limiter, welcome/stop.
5. `feat`: embed / linked-image / Tenor sources.
6. `docs`: README section (setup, ToS warning, allowlist behaviour, manual run).

Each commit keeps `python -m unittest discover -s tests` green.

## Testing

- All logic that decides *whether and what* to convert lives in `selfbot_logic.py` and is tested
  with plain fake objects (no `discord` import needed): decision table, allowlist persistence and
  atomic writes, rate limiter, source collection and priority, batch capping.
- `selfbot.py` is kept thin and covered by a couple of adapter tests with mocks; if the sandbox
  lacks `discord.py-self`, those are skipped with a clear reason.

## Left to the builder

Exact welcome wording, log field names, rate-limit constants (5/60 s is a starting point),
how `discord.py-self` exposes reference resolution/typing in the pinned version.

## My calls (not discussed — easy to change)

- `stop` DM opt-out and removal from the allowlist.
- Silent ignore for non-allowlisted pings and rate-limit excess.
- JSON file instead of SQLite for the allowlist.
- Reply targets the triggering message, not the replied-to one.
- First-DM media is converted immediately after the welcome.
