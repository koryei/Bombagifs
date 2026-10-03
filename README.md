# Bombagif

Bombagif converts WEBP, PNG, and SVG images into optimized GIFs, uploads them to **your own Zipline instance**, then replies with its link. People add your Discord application to their user account once and run `/gif` in DMs, group DMs, or servers; they do not need to install a bot in each server.

## Quick install

The interactive installer clones this public repository, asks which hosting mode you want, and prompts privately for **your own** Discord bot token, Zipline token, and Zipline URL. It never prints those values or places them in the command line. As with any `curl | bash` installer, the command executes the downloaded script; inspect it first if you want to review the code before running it.

```bash
curl -fsSL https://raw.githubusercontent.com/koryei/Bombagifs/main/install.sh | bash
```

The menu offers:

1. **Python venv** — local or VPS setup; prints the command to run Bombagif.
2. **Docker Compose** — builds and starts the bot with restart and bounded log settings.
3. **Ubuntu systemd** — asks before using `sudo` to install packages and register a service that starts at boot. Use a normal VPS login user, not `root`; install goes under that user's home directory.

The installer leaves its clone in `~/Bombagif` by default and intentionally stops if that path already exists. To choose another fresh path, download the installer first and run `BOMBAGIF_INSTALL_DIR=/path/to/new/folder bash /tmp/bombagif-install.sh`.

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

Users choose **Add to my apps**, authorize, then use `/gif` and select a WEBP, PNG, or SVG image (maximum 15 MB). Global command propagation after first startup may take a short while.

## Zipline and environment

Every hoster supplies **their own** Zipline HTTPS base URL and API token. The bot sends uploads to the configured URL only. There is no Bombagif-owned Zipline endpoint or shared maintainer token in the application configuration. Users invoking `/gif` do not see or provide the hoster's credentials.

The installer writes the entered values into a private `.env` in the install directory (mode `600`). For manual setup, copy [.env.example](.env.example) only when you do not already have a `.env`; otherwise edit the existing file without overwriting your secrets. Set `DISCORD_TOKEN`, `ZIPLINE_TOKEN`, and `ZIPLINE_URL` to your own values. `ZIPLINE_URL` must be an HTTPS base URL, not an `/api/upload` URL. The example domain is intentionally a placeholder and will not work until replaced. In this shared VPS install, `/gif` runs under the bot owner's Discord app; this project does not let each end-user enter separate Zipline credentials.

Leave `ALLOWED_GUILDS` blank to allow public use in all server contexts and DMs. Optionally set comma-separated guild IDs to restrict invocations made in server contexts. Never put secrets in an install command, source file, issue, screenshot, or public repository.

## Manual Python setup

Requires Python 3.11+; SVG conversion additionally needs Cairo. The bot starts and handles PNG/WEBP without Cairo. On macOS install Cairo with `brew install cairo`; on Ubuntu, install it with `sudo apt-get install libcairo2`.

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

## Docker Compose (manual)

```bash
(umask 077 && cp -n .env.example .env)
# Edit .env with your own credentials and ZIPLINE_URL.
docker compose up -d --build
```

Useful commands: `docker compose logs -f bombagif`, `docker compose restart bombagif`, and `docker compose down`. Compose does not publish ports. For an always-on VPS, use a restart policy (already set in [compose.yaml](compose.yaml)) and keep the `.env` file private. Compose v2.24.0+ is needed for the required env-file check in [compose.yaml](compose.yaml). The customizable Discord presence is in [`status.config`](status.config); edit it then restart the bot to apply. Be aware that the Docker daemon runs containers with root-equivalent host privileges; only run containers/images you trust.

## Custom Discord presence

Edit [`status.config`](status.config) in the install directory, then restart Bombagif. The `[status]` section supports `status = online|idle|dnd|invisible`, `activity_type = playing|listening|watching|competing|streaming|custom`, and `activity_text` up to 128 characters. For `streaming`, set an HTTPS `streaming_url`; for `custom`, optionally set `activity_emoji`. Leave `activity_text` blank to hide the activity while keeping the selected presence status. The bot reloads this configuration whenever Discord connects or reconnects; it does not hot-reload edits made while connected.

For the Ubuntu systemd installer, edit `~/Bombagif/status.config` and run `sudo systemctl restart bombagif`. For Docker Compose, edit `~/Bombagif/status.config` and run `docker compose restart bombagif`; Compose mounts the file read-only into the container. For Python, restart the running process.

## Zipline upload responses

Bombagif supports Zipline JSON responses containing a `files` URL list, legacy objects with a `url` field, a JSON string URL, and the documented plain-text URL response. If Zipline returns an HTTP link for your same host while your configured public URL is HTTPS, Bombagif safely upgrades that link to your configured HTTPS host. If an upload succeeds but no safe URL is returned, Bombagif explains that HTTPS return URLs may need to be enabled in Zipline (`CORE_RETURN_HTTPS_URLS=true`) and warns you to check Zipline before retrying to avoid duplicate uploads.

## Secret hygiene before publishing

- [`.gitignore`](.gitignore) excludes local env files, virtualenvs, and Python caches. [`.dockerignore`](.dockerignore) keeps env files out of Docker build context.
- Commit `.env.example`, never `.env`. Review the installer and staged files before publishing. Git history is separate: if any credential was ever committed, revoke it; deleting it from the newest revision does not invalidate a leaked token.
- This repository does not contain your Discord bot token, your Zipline token, or your private Zipline URL. Only the operator's local `.env` contains those values.

## Checks

Run `bash tests/test_installer.sh` for installer helper checks and `python -m unittest discover -s tests -v` for offline image and local mock-Zipline behavior tests. They do not contact Discord or your production Zipline host.
