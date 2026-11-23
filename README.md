# Bombagifs
<img width="800" height="429" alt="BombaGif Tutorial" src="https://github.com/user-attachments/assets/7d58900f-f757-45bc-ab14-1d31066a783d" /><br>
Bombagif converts JPG/JPEG, AVIF, WEBP, PNG, BMP, TIFF, SVG and video (MP4, MOV, WebM, MKV, AVI, M4V, 3GP, OGV, WMV) uploads into optimized GIFs, uploads them to **your own Zipline instance**, then replies with a link. The `/gif` command accepts up to five files at a time and aims for GIFs around or below 1 MB when quality allows. People add your Discord application to their user account once and run `/gif` in DMs, group DMs, or servers; they do not need to install a bot in each server.
## Quick install

One script installs, updates, inspects, and removes Bombagif. It prompts privately for **your own** Discord bot token, Zipline token, and Zipline URL, and never prints those values or places them in the command line. As with any `curl | bash` installer, the command executes the downloaded script; inspect it first if you want to review the code before running it.

```bash
curl -fsSL https://raw.githubusercontent.com/koryei/Bombagifs/main/install.sh | bash
```

Run the same command any time to reopen the menu:

1. **Install Bombagif** — first-time setup; also repairs or reconfigures an existing install.
2. **Update to the latest** — pulls new source, then rebuilds or restarts with your existing `.env`.
3. **Manage the service** — start, stop, restart, show recent logs, or re-apply `.env`/`status.config`.
4. **Show status** — install directory, detected mode, whether the bot is running, and the next command to type.
5. **Uninstall** — stops and removes the service, then asks before deleting the install directory.
6. **Check installation** — verifies `.env` keys, Zipline reachability, Cairo, FFmpeg, Docker, and the virtualenv.

For a new install the menu then asks how Bombagif should run:

1. **Python venv** — local or VPS setup; installs dependencies and starts the bot in the background with a log file.
2. **Docker Compose** — builds and starts the bot with restart and bounded log settings.
3. **Ubuntu systemd** — asks before using `sudo` to install packages and register a service that starts at boot. Use a normal VPS login user, not `root`; install goes under that user's home directory.

Every install finishes with a results summary: whether the bot actually started, the exact log and restart commands for your mode, the Discord invite URL, and a reminder that `.env` holds your secrets. To choose another install path, download the installer first and run `BOMBAGIF_INSTALL_DIR=/path/to/folder bash /tmp/bombagif-install.sh`. Non-interactive automation can set `BOMBAGIF_INSTALL_MODE` (`python`, `docker`, or `systemd`) and `BOMBAGIF_INSTALL_ACTION` (`install`, `update`, `manage`, `status`, `uninstall`, or `doctor`) with `DISCORD_TOKEN`, `ZIPLINE_TOKEN`, and `ZIPLINE_URL` exported.

For security-conscious installs, inspect the installer first, then run it:

```bash
curl -fsSLo /tmp/bombagif-install.sh https://raw.githubusercontent.com/koryei/Bombagifs/main/install.sh
less /tmp/bombagif-install.sh
bash /tmp/bombagif-install.sh
```

You can customize the repository, branch, install path, and mode with `BOMBAGIF_REPO_URL`, `BOMBAGIF_BRANCH`, `BOMBAGIF_INSTALL_DIR`, and `BOMBAGIF_INSTALL_MODE`. Valid mode values: `python`, `docker`, `systemd`. This is useful for forks; review your fork's installer before sharing its command. The Docker path requires Docker Engine plus Compose v2. The systemd path is for Ubuntu only and requests confirmation before system-wide changes. It installs packages and registers a systemd unit but does not create a separate OS account.

## Discord app setup (each hoster does this once)

1. Create an application at [Discord Developer Applications](https://discord.com/developers/applications).
2. Under **Installation**, enable **User Install**. Bombagif registers a global user-installed `/gif` command and does not require Message Content Intent or a bot added to every server.
3. Under **Bot**, create/reset a bot token. The installer asks for this token privately; alternatively put it in `DISCORD_TOKEN` in your local `.env`.
4. Copy the **Application ID** from **General Information**. Share this install URL with users, replacing the placeholder:

   `https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=applications.commands&integration_type=1`

Users choose **Add to my apps**, authorize, then use `/gif` and select up to five JPG/JPEG, AVIF, WEBP, PNG, BMP, TIFF, SVG, or video (MP4, MOV, WebM, MKV, AVI, ...) files (maximum 15 MB each). Video uploads are trimmed to their first 10 seconds. GIF compression targets 1 MB using bounded reductions in dimensions and frame rate/count; if the target cannot be met at the lowest quality step, Bombagif returns the smallest candidate it produced instead, subject to the existing 25 MB hard ceiling. Global command propagation after first startup may take a short while.

## Zipline and environment

Every hoster supplies **their own** Zipline HTTPS base URL and API token. The bot sends uploads to the configured URL only. There is no Bombagif-owned Zipline endpoint or shared maintainer token in the application configuration. Users invoking `/gif` do not see or provide the hoster's credentials.

The installer writes the entered values into a private `.env` in the install directory (mode `600`). For manual setup, copy [.env.example](.env.example) only when you do not already have a `.env`; otherwise edit the existing file without overwriting your secrets. Set `DISCORD_TOKEN`, `ZIPLINE_TOKEN`, and `ZIPLINE_URL` to your own values. `ZIPLINE_URL` must be an HTTPS base URL, not an `/api/upload` URL. The example domain is intentionally a placeholder and will not work until replaced. In this shared VPS install, `/gif` runs under the bot owner's Discord app; this project does not let each end-user enter separate Zipline credentials.

Leave `ALLOWED_GUILDS` blank to allow public use in all server contexts and DMs. Optionally set comma-separated guild IDs to restrict invocations made in server contexts. Never put secrets in an install command, source file, issue, screenshot, or public repository.

## Manual Python setup

Requires Python 3.11+; video conversion (MP4, MOV, WebM, MKV, ...) requires FFmpeg, and SVG conversion additionally needs Cairo. JPG/JPEG, AVIF, WEBP, and PNG decoding use the installed Pillow build; when Pillow has no AVIF codec, AVIF decoding can fall back to FFmpeg if present. Install FFmpeg on macOS with `brew install ffmpeg` or Ubuntu with `sudo apt-get install ffmpeg`; install Cairo with `brew install cairo` or `sudo apt-get install libcairo2`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
# Copy only on first setup; cp -n leaves an existing .env untouched.
(umask 077 && cp -n .env.example .env)
# Edit .env: set your own DISCORD_TOKEN, ZIPLINE_TOKEN, and ZIPLINE_URL.
.venv/bin/python main.py
```

Keep the process running on your machine/VPS/container. Discord sends interactions over the bot's existing gateway connection, so no inbound web port is needed.

The `/gif` command accepts JPG/JPEG, AVIF, WEBP, PNG, BMP, TIFF, SVG, and video (MP4, MOV, WebM, MKV, AVI, M4V, 3GP, OGV, WMV) attachments, up to five files per command and 15 MB per file. FFmpeg probes video containers, converts up to the first 10 seconds, and makes bounded attempts at progressively smaller GIF dimensions/frame rates toward 1 MB. If the target is unrealistic, the smallest candidate is returned; all outputs remain capped at 25 MB. Temporary video input/output files are deleted after processing. Install FFmpeg on the host for Python mode; Docker and Ubuntu systemd installs include it automatically. Python mode also requires Cairo for SVG conversion; Docker and Ubuntu systemd installs include Cairo, while macOS Python installs need `brew install cairo`.

## Docker Compose (manual)

```bash
(umask 077 && cp -n .env.example .env)
# Edit .env with your own credentials and ZIPLINE_URL.
docker compose up -d --build
```

Useful commands: `docker compose logs -f bombagif`, `docker compose restart bombagif`, and `docker compose down`. Compose does not publish ports. For an always-on VPS, use a restart policy (already set in [compose.yaml](compose.yaml)) and keep the `.env` file private. Compose v2.24.0+ is needed for the required env-file check in [compose.yaml](compose.yaml). The customizable Discord presence is in [`status.config`](status.config), which is reloaded while the bot runs. Be aware that the Docker daemon runs containers with root-equivalent host privileges; only run containers/images you trust.

## Custom Discord presence

The presence is sent with the bot's very first gateway connection and re-applied on every reconnect, so the app never appears offline. If `status.config` is missing or contains an invalid value, Bombagif logs the problem and stays online instead of failing to connect.

Edit [`status.config`](status.config) in the install directory while Bombagif runs: edits are applied within about 15 seconds, the presence is re-pushed every few minutes so a quiet app still looks live, and restarting is optional. The `[status]` section builds the card: `status = online|idle|dnd|invisible`, `activity_type = playing|listening|watching|competing|streaming|custom`, `activity_name` (the bold title), `details` (line 1), `state` (line 2), optional card images (`large_image`, `large_text`, `small_image`, `small_text`), and an optional `button_label` with `button_url` (either an HTTPS URL or the shortcut `user-install`, which points at this app's own User-Install URL). Every text field is limited to 128 characters and image keys to 32. For `streaming`, set an HTTPS `streaming_url`; for `custom`, optionally set `activity_emoji` (`custom` is a single line and cannot carry details, state, images, or a button). `activity_text` is the simple one-line alternative; leave every text field blank to hide the activity while keeping the selected presence status. Discord only sends presence button labels over the gateway and resolves button links from your app's own rich-presence settings, so the configured URL is also attached as a link on the state line. Discord renders an app's card only where the client can already see the app, so install it in a server (or the card stays invisible) and remember that Discord decides which rich fields it shows for app/bot accounts.

Nothing needs restarting for a presence change: edit `~/Bombagif/status.config` and the running bot picks it up. Compose mounts the file read-only into the container, so editing the host file is enough. To force a clean re-read instead, use **Manage the service → Re-apply settings** in the installer menu, which restarts the right thing for your mode (`sudo systemctl restart bombagif` on Ubuntu, `docker compose restart bombagif` with Compose).

## Zipline upload responses

Bombagif supports Zipline JSON responses containing a `files` URL list, legacy objects with a `url` field, a JSON string URL, and the documented plain-text URL response. If Zipline returns an HTTP link for your same host while your configured public URL is HTTPS, Bombagif safely upgrades that link to your configured HTTPS host. If an upload succeeds but no safe URL is returned, Bombagif explains that HTTPS return URLs may need to be enabled in Zipline (`CORE_RETURN_HTTPS_URLS=true`) and warns you to check Zipline before retrying to avoid duplicate uploads.

## Self-bot (v2): ping Bombagif instead of running `/gif`

Besides the `/gif` app, Bombagif can run as a Discord **user account** (a "self-bot", in the style of
[BombaclatAI](https://github.com/koryei/BombaclatAI)). Anyone who has **DMed that account first** can then:

- **DM it a file** (or a GIF/image link embed): it replies with the GIF link.
- **`@` it with a file attached**, in any server, DM, or group DM it can see.
- **Reply to someone's message that has media and `@` it**: it converts the replied-to media.

It replies once to the triggering message, with bare links so Discord embeds the GIF, up to five files per
message, and at most five triggers per person per minute (excess is ignored silently). Plain link previews are
not triggers.

**Consent rule.** The first DM from a person registers them (they get a short welcome, and any media in that
first message is converted). Everyone else is ignored completely: no replies, no error messages. Sending `stop` in
the DM removes you again. The allowlist is a private JSON file (`$SELFBOT_DATA_DIR/allowlist.json`, mode 600).

> **Warning:** automating a user account violates [Discord's Terms of Service](https://discord.com/terms) and can
> get the account banned. Use a throwaway or alt account you are willing to lose, never your main one.

It uses `discord.py-self`, which installs as the same `discord` package as `discord.py`, so it needs its **own
virtualenv**; the conversion pipeline (`bombagif_core.py`) is shared and imports neither library.

```bash
python3 -m venv .venv-selfbot
.venv-selfbot/bin/pip install -r requirements-selfbot.txt
# In .env: SELFBOT_TOKEN (the user-account token), SELFBOT_DATA_DIR (default ./data),
# plus the same ZIPLINE_URL / ZIPLINE_TOKEN as the app.
.venv-selfbot/bin/python selfbot.py
```

**Running it on a VM (beta).** On an Ubuntu/Debian VM with systemd, one script installs and updates the self-bot:

```bash
git clone https://github.com/koryei/Bombagifs.git && cd Bombagifs
./selfbot-ctl.sh install        # venv, FFmpeg, Cairo, systemd service (you bring your own .env)
./selfbot-ctl.sh update         # fast-forward to the latest main, restart, check it stays up
./selfbot-ctl.sh auto on 15     # optional: look for updates every 15 minutes
./selfbot-ctl.sh timezone America/New_York   # clip cards print clock times in the VM's timezone (default UTC)
./selfbot-ctl.sh training       # dry-run training form: review every judged run, then rate the DMs
./selfbot-ctl.sh status | logs -f | doctor | restart | stop | start | version
```

Bring your own `.env`: copy it onto the VM (`scp .env user@vm:~/Bombagifs/.env`) before or after `install`. The script never
prompts for secrets; it locks the file to mode 600 and checks that `SELFBOT_TOKEN`, `ZIPLINE_URL` and `ZIPLINE_TOKEN` are set. Until they are,
`install` leaves the service enabled but stopped, and `start`/`restart` refuse and say what is missing.

`update` refuses to run over local edits or diverged history, installs dependencies only when `requirements-selfbot.txt`
changed, imports the new code before restarting, and **rolls back** to the previous commit if the service does not stay
up for 20 seconds. `data/` and `.env` are never touched. `auto on` adds a systemd timer and a one-line sudoers entry that
lets your user restart only this service without a password. `doctor` reports what is missing without printing secrets.
Tests: `bash tests/test_selfbot_ctl.sh`.

Before it restarts, `update` reads `data/busy.json` and waits for in-flight conversions and clip renders to finish,
re-checking every 10 seconds for up to 5 minutes, so a restart never kills a job mid-flight. A marker older than two
minutes is ignored, so a crashed bot cannot block updates. `auto on 1` checks every minute; the timer's first check is
five minutes after boot.

`training` is the form for a teaching phase: it asks a few questions (or takes `on` / `off` / `status`) and writes only
the scout settings in `.env` — `SELFBOT_SCOUT_MODE=dry-run`, `SCOUT_REVIEW=always`, `SCOUT_REVIEW_PER_HOUR=30`,
`SCOUT_NEAR_MISS_*_FUNNY=0`, `SCOUT_EXAMPLES=12` — then restarts. Your tokens and every other line stay as they are,
and nothing is printed. `training off` puts the review defaults back; `training status` shows the current values.

**Update announcements.** Every update leaves `data/update-notice.json` behind, and the bot DMs it to everyone on the
allowlist on its next boot: the new version, when it landed, and one line per commit it brought. A rollback is announced
too, so nobody wonders why nothing changed. The notice is deleted once delivered; if no DM got through it stays for the
next boot. Set `SELFBOT_UPDATE_NOTICES=false` to keep the bot quiet about updates.

**Terminal UI.** Run in a terminal, the self-bot prints a boot banner (account, Zipline host, allowlist size,
guard state, limits), keeps a live status panel (state, uptime, counters, and a feed of recent conversions, sign-ups,
and warnings), writes readable coloured log lines above it, and signs off with a session summary. Redirected output,
systemd, and Docker keep the one-line JSON logs automatically. Force a look with `SELFBOT_UI=pretty` or `json`
(default `auto`). Preview it without connecting to Discord: `.venv-selfbot/bin/python console_ui.py`.

**CAPTCHA guard.** The self-bot cannot solve CAPTCHAs, and retrying only makes Discord stricter. If Discord asks
for one (for example when replying, fetching a message, or accepting a friend request), the self-bot logs a single
clear error, pauses *every* account action, and stays paused across restarts (`$SELFBOT_DATA_DIR/captcha-blocked.json`).
To resume: log in to the account in a browser, complete whatever Discord asks, delete that file, and restart.

**Friend requests.** Friending the account is not required: people in a shared server or group chat can DM it
(unless the account blocks them). Set `SELFBOT_AUTO_ACCEPT_FRIENDS=true` to accept incoming requests automatically
(paced a few seconds apart, at most 10 pending requests per connect, and paused by the CAPTCHA guard). Accepting a
request does not register anyone; they still have to DM the account first.

**Troubleshooting uploads.** A failed upload logs `bombagif.zipline` with the HTTP status and a short snippet of
Zipline's error (never your token). `status=401/403` usually means a wrong `ZIPLINE_TOKEN`; `413` means Zipline's
size limit; a Cloudflare or proxy page in the snippet points at your reverse proxy.

The self-bot is run manually for now; the installer, Dockerfile, and compose file only cover the `/gif` app. It
needs FFmpeg and Cairo on the host for the same formats as the app. Embedded media is only ever fetched through
Discord's own CDN/proxy URLs.

## Clip scout (v3): catch the funny stuff

A "Medal for chat". The self-bot can watch chats **you turn it on in**, spot a message (or a short run of messages)
that is genuinely funny with no context, and post it back as a static Discord-style quote card that names who is in
it. Deleted and edited messages are still clippable: the account keeps its own short-lived copy of recent messages.

> **Think before you turn it on.**
> - **Privacy:** while a chat is enabled its messages are sent (anonymised: `A`/`B` speaker labels, text only, no
>   names, ids, or avatars) to a third-party model. The Inception API may train on submissions **unless** you turn off
>   "Improve the model for everyone" in its API platform settings. Tell the people in the chat.
> - **Discord's ToS** forbids automating a user account, and a bot that reads everything and posts uninvited is easier
>   to flag than the converter. Use an alt account you can lose.
> - Judging is imperfect. Always run **dry-run** first.

**How it works.** Messages in enabled chats sit in an in-memory buffer (15 minutes, never written to disk). After a
45 second reaction window each run of one person's messages is screened by **Mercury Decide** (cheap probabilities for
"funny", "funny without context", "sensitive") and, if it passes, judged by **Mercury 3**, which picks the exact
messages (up to 8 messages, 600 characters, 3 people) and must decline anything that needs context. Then local gates
run that the model cannot override: no personal info, credentials, invites, links, `@everyone`, or blocklisted words;
nobody who opted out; per-chat and per-person cooldowns; at most 4 clips an hour. Only then is the card posted.

**Turn it on**
1. Set `SELFBOT_OWNER_ID` (your own user id), `INCEPTION_API_KEY`, and `SELFBOT_SCOUT_MODE=dry-run` in `.env`
   (see `.env.example` for every setting), then restart the self-bot.
2. In a chat the account is in, type `!on` (only you can; other people's commands are ignored). The account
   announces it there: it is clipping funny messages, names whoever is in them, and `stop` opts out. In dry-run there is
   no announcement, because nothing will be posted.
3. Watch the terminal: candidates and scores appear in the feed (`dry-run: would clip Maple (funny 8.7)`) and
   `data/clips.jsonl` records them without text. Adjust the bars, then switch to `SELFBOT_SCOUT_MODE=on`.
4. `!off` stops it in that chat (and drops its buffer); `!status` / `!list` answer you by DM. From a
   DM with the account you can also use `!on <chat id>` / `!off <chat id>`. It never scouts 1:1 DMs or
   NSFW channels.

**What a clip looks like.** One card per clip, posted as a reply to the last message in it (or a plain message if
they were all deleted), with ``🎬 Clipped **Name** — DM me `stop` to opt out of clips.`` Names are shown, never
mentioned. Deleted messages get a stricter bar (and `SELFBOT_CLIP_DELETED=owner` sends those to you privately instead).

**Opting out.** `stop` in a DM with the account (anyone can, even if they never talked to it), or a reply of `stop` to
a clip, stops clipping that person. A DM `stop` also turns their conversions off; `start` in a DM turns everything back
on. Opted-out people's messages are never stored or sent to the model.

**Strangers who ping the account** (without DMing it first) now get one short help message (at most once an hour per
person and once per ten minutes per chat), not silence.

**When Inception shuts down (2026-12-28).** The judge is a chain: Inception, then any OpenAI-compatible model you
configure (`SCOUT_LLM_BASE_URL`, `SCOUT_LLM_API_KEY`, `SCOUT_LLM_MODEL`; it must support JSON-schema output), then
"heuristic", which only shows candidates in the terminal and never posts. A provider that is gone (401/402/404/410) or
failing five times in a row is dropped automatically, with one line in the terminal. To switch, set the `SCOUT_LLM_*`
variables (or `SCOUT_JUDGE=openai`) and restart.

Preview the card without Discord: `python clip_render.py demo out.png`. Everything the scout stores lives in
`SELFBOT_DATA_DIR`: `optout.json`, `scout-chats.json`, `clips.jsonl` (all mode 600), `emoji-cache/`, and an optional
`scout-blocklist.txt` (one term per line, `#` comments). Bundled fonts: Noto Sans (SIL OFL, `assets/fonts/OFL.txt`).

### Clip that (manual clips)

Reply to any **text** message and `@` the account: it posts that message (and the rest of that person's run, if it
is still buffered) as a card, with ``🎬 Clipped **Name** at **You**'s request — DM me `stop` to opt out of clips.``
No judge is involved, since a person chose it. It posts even in `dry-run`.
- **Who:** people who have DMed the account first. Members can only do it in chats where the owner ran `!on`;
  the owner (`SELFBOT_OWNER_ID`) can do it anywhere except NSFW channels and 1:1 DMs.
- **Always refused:** opted-out people, bots, the account itself, non-text messages (links, images, stickers,
  spoilers), anything the local sensitive filter catches, and a message that was already clipped. Refusals are one
  short reply and never quote the message.
- **Limits** (members): 3 clips per 10 minutes each, and one clip per person per 2 minutes
  (`SCOUT_MANUAL_LIMIT_PER_10_MIN`, `SCOUT_MANUAL_AUTHOR_COOLDOWN_SECONDS`). `SCOUT_MANUAL=off` turns it off.
- When the **owner** clips something this way it is saved as a "good" example.

#### Without a ping (spoken triggers)

In a chat where the owner ran `!on`, nobody has to `@` the account. Say or reply:

| Say | What happens |
|---|---|
| reply `clip that` (also `clip this`, `clip it`, `clip dat`, `clip that one`, ...) | clips the message you replied to |
| `clip that` (no reply) | clips the latest message in the chat (and that person's run), if it is under `SCOUT_TRIGGER_MAX_AGE_SECONDS` old (180) |
| reply `gif that` / `make that a gif` | converts the media in the message you replied to |

Filler, laughing, emoji and stretched letters are ignored, so `bro clip thattt LMFAOOO 😭` works. **Typos and odd
wording** (`clkip that LMFAII`, `cilp dat`) are confirmed by a small AI check. It is an easy yes/no job, so it goes to **Mercury Decide** (one cheap call, two
questions), with any `SCOUT_LLM_*` chat model as the fallback once Inception shuts down. It sees only that one line,
as data, under a daily cap (`SCOUT_TRIGGER_AI_PER_DAY`, 300), and "nice clip" or "clip that
video" are correctly ignored. With no provider, only the exact phrases work.
- **Who:** registered members; `SCOUT_TRIGGER_ANYONE=on` lets anyone in an enabled chat ask for a clip (never a GIF).
- **Quiet:** refusals (opted out, sensitive, already clipped, slow down, nothing recent) are never posted, since nobody
  pinged the account and it can't know the message was meant for it. They show in the terminal feed. A pinged request
  still gets its refusal, as before.
- **Off switches:** `SCOUT_TRIGGERS=off`, or `phrases` for exact phrases only (no AI). Chats without `!on` are not
  listened to, and a request message is never buffered as chat.

### Teaching it

The judge starts with a fixed idea of "funny out of context". You can steer it three ways (all owner-only, answers
come by DM):
1. **Taste notes**, plain English, up to 1,500 characters: `!taste add we like deadpan one-liners, skip
   anything about exams`. They go into the judge's instructions. `!taste` shows them, `!taste clear` wipes
   them, and `data/scout-taste.txt` can be edited by hand (changes apply on the next judging call).
2. **Rate examples.** In dry-run the account DMs you each candidate (what it would clip, plus some near-misses; at most
   `SCOUT_REVIEW_PER_HOUR`). **Reply to that DM** with `good` (clip-worthy), `bad` (never) or `skip` (the account never adds reactions, they are
   rate limited; your own 👍/👎/🙈 reactions on the DM still count). You can also reply `good`/`bad` to any clip it posted. Your
   ratings are stored in `data/taste.jsonl` (only lines you rated, anonymous speaker labels, mode 600) and the newest
   `SCOUT_EXAMPLES` good and bad ones are shown to the judge as examples. `!examples` lists them,
   `!examples clear` forgets them.
3. **Tune the bar.** `!tune` compares your good/bad ratings with the scores and suggests `SCOUT_JUDGE_FUNNY_MIN` (needs
   5 scored of each).

**Training phase.** To teach it fast, run `SELFBOT_SCOUT_MODE=dry-run` with `SCOUT_REVIEW=always`,
`SCOUT_REVIEW_PER_HOUR=30`, `SCOUT_SCREEN=all`, `SCOUT_NEAR_MISS_DECIDE_FUNNY=0`, `SCOUT_NEAR_MISS_FINAL_FUNNY=0`
and `SCOUT_EXAMPLES=12`: every judged run is DM'd to you (still capped per hour), including the ones the judge
rejected, so you can catch misses as well as false positives. Rate them, run `!tune`, then put the near-miss bars
back to `0.6` / `6` and switch to `SELFBOT_SCOUT_MODE=on` when the scores separate your taste.

Privacy: the examples are real chat lines sent (anonymised) to the model on every judging call, and review DMs show you
real names and text. Only lines you chose to rate are kept. Say so in the chat.

### `!help`

`!help` (or `!help`) from the owner DMs the full command list. Anyone else gets the short guide, and only in a
1:1 DM with the account; elsewhere it is ignored. Handy for testers.

## Secret hygiene before publishing

- [`.gitignore`](.gitignore) excludes local env files, virtualenvs, and Python caches. [`.dockerignore`](.dockerignore) keeps env files out of Docker build context.
- Commit `.env.example`, never `.env`. Review the installer and staged files before publishing. Git history is separate: if any credential was ever committed, revoke it; deleting it from the newest revision does not invalidate a leaked token.
- This repository does not contain your Discord bot token, your Zipline token, or your private Zipline URL. Only the operator's local `.env` contains those values.

## Checks

Run `bash tests/test_installer.sh` for installer helper checks and `python -m unittest discover -s tests -v` for offline image/video, presence, and local mock-Zipline behavior tests. They do not contact Discord or your production Zipline host. The self-bot's adapter tests need `discord.py-self` and run only in its own venv: `.venv-selfbot/bin/python -m unittest tests.test_selfbot_adapter -v` (they are skipped elsewhere); the rest of the self-bot logic is Discord-free and runs in either environment. The SVG rasterization test needs Cairo installed; without it, that single test reports the Cairo prerequisite while the rest pass.
