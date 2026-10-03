# Bombagif

Bombagif converts WEBP, PNG, SVG, MP4, and WebM uploads into optimized GIFs, uploads them to **your own Zipline instance**, then replies with its link. People add your Discord application to their user account once and run `/gif` in DMs, group DMs, or servers; they do not need to install a bot in each server.

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

Users choose **Add to my apps**, authorize, then use `/gif` and select a WEBP, PNG, SVG, MP4, or WebM file (maximum 15 MB). Video uploads are trimmed to their first 10 seconds and converted at 15 fps, scaled to fit within 480×480. Global command propagation after first startup may take a short while.

## Zipline and environment

Every hoster supplies **their own** Zipline HTTPS base URL and API token. The bot sends uploads to the configured URL only. There is no Bombagif-owned Zipline endpoint or shared maintainer token in the application configuration. Users invoking `/gif` do not see or provide the hoster's credentials.

The installer writes the entered values into a private `.env` in the install directory (mode `600`). For manual setup, copy [.env.example](.env.example) only when you do not already have a `.env`; otherwise edit the existing file without overwriting your secrets. Set `DISCORD_TOKEN`, `ZIPLINE_TOKEN`, and `ZIPLINE_URL` to your own values. `ZIPLINE_URL` must be an HTTPS base URL, not an `/api/upload` URL. The example domain is intentionally a placeholder and will not work until replaced. In this shared VPS install, `/gif` runs under the bot owner's Discord app; this project does not let each end-user enter separate Zipline credentials.

Leave `ALLOWED_GUILDS` blank to allow public use in all server contexts and DMs. Optionally set comma-separated guild IDs to restrict invocations made in server contexts. Never put secrets in an install command, source file, issue, screenshot, or public repository.

## Manual Python setup

Requires Python 3.11+; MP4/WebM video conversion requires FFmpeg, and SVG conversion additionally needs Cairo. The bot starts and handles WEBP/PNG images without either. Install FFmpeg on macOS with `brew install ffmpeg` or Ubuntu with `sudo apt-get install ffmpeg`; install Cairo with `brew install cairo` or `sudo apt-get install libcairo2`.

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

The `/gif` command accepts MP4 and WebM video attachments up to 15 MB. FFmpeg probes the file container rather than forcing a guessed demuxer, including MP4s whose index (`moov`) atom is at the end. It converts only the first 10 seconds at 15 fps and scales frames to fit within 480×480. FFmpeg conversion runs with a 45-second timeout; the output GIF is capped at 25 MB, and temporary input/output files are deleted after processing. Install FFmpeg on the host for Python mode; Docker and Ubuntu systemd installs include it automatically. Python mode also requires Cairo for SVG conversion; Docker and Ubuntu systemd installs include Cairo, while macOS Python installs need `brew install cairo`.

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

## Secret hygiene before publishing

- [`.gitignore`](.gitignore) excludes local env files, virtualenvs, and Python caches. [`.dockerignore`](.dockerignore) keeps env files out of Docker build context.
- Commit `.env.example`, never `.env`. Review the installer and staged files before publishing. Git history is separate: if any credential was ever committed, revoke it; deleting it from the newest revision does not invalidate a leaked token.
- This repository does not contain your Discord bot token, your Zipline token, or your private Zipline URL. Only the operator's local `.env` contains those values.

## Checks

Run `bash tests/test_installer.sh` for installer helper checks and `python -m unittest discover -s tests -v` for offline image/video, presence, and local mock-Zipline behavior tests. They do not contact Discord or your production Zipline host. The SVG rasterization test needs Cairo installed; without it, that single test reports the Cairo prerequisite while the rest pass.
