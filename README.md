# Bombagif

Bombagif converts WEBP, PNG, and SVG images into optimized GIFs, uploads them to Zipline, and replies with a link. **Anyone can add the command to their Discord account once and use `/gif` in DMs, group DMs, or servers without inviting a bot into each server.**

## 1. Create the Discord app

1. Go to [Discord Developer Applications](https://discord.com/developers/applications) → **New Application** → name it **Bombagif**.
2. In **Installation**, enable **User Install**. The command is user-install-only and global. It does not need a server bot install or privileged Message Content intent.
3. In **Bot**, create/reset a bot token. Keep it secret; it goes in `.env`, never in this repository.
4. Copy the **Application ID** from **General Information**. Make the user install URL by replacing the placeholder below:

   `https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=applications.commands&integration_type=1`

   Anyone can open that URL, choose **Add to my apps**, and authorize. You can alternatively copy the user install link from **Installation → Install Link**.

## 2. Configure your own services and secrets

This repository intentionally contains **no maintainer-owned Discord token, Zipline token, or Zipline domain**. Every operator needs their own Discord application/bot token and their own Zipline instance/API token. The bot sends each uploaded GIF only to the `ZIPLINE_URL` and token you configure; users do not supply credentials to the bot.

Create an API token in your own Zipline account, then copy [.env.example](.env.example) to `.env` only if you do not already have one. Otherwise edit your existing `.env` and preserve your current values. Set `DISCORD_TOKEN`, `ZIPLINE_TOKEN`, and `ZIPLINE_URL` to your own credentials and HTTPS Zipline base URL. Leave `ALLOWED_GUILDS` empty for public user-installed access; set comma-separated IDs only to limit use in server contexts. Never publish, share, or commit `.env` or tokens. If a real token was ever committed or shared, revoke and replace it before publishing.

`ZIPLINE_URL` is required at startup and intentionally has no maintainer-owned default. The sample uses the reserved `your-zipline.example.com` placeholder and will not work until replaced.

## 3. Run Bombagif

Use Python 3.11. Cairo's native library is required only when converting SVG. The bot itself can start without Cairo; WEBP and PNG conversion work without it.

If your terminal prompt shows `(base)`, Conda is active. To avoid mixing Conda and system Homebrew libraries, activate the `.venv` below and run Bombagif with `.venv/bin/python main.py` (or activate it first and run `python main.py`).

On macOS, install Cairo before setting up the Python environment:

```bash
brew install cairo
```

Then create the environment and install the Python dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
# Create the sample only if you don't already have a local .env.
test -f .env || cp .env.example .env
# Edit .env: DISCORD_TOKEN, ZIPLINE_TOKEN, and your own ZIPLINE_URL.
.venv/bin/python main.py
```

Keep this process running on a machine/server/container: Discord users invoke the app, but the bot process handles the interaction and Zipline upload. On startup it publishes the global `/gif` command; global-command propagation can take a little while. Then share the user-install URL. Each user installs the app once and invokes `/gif`, selecting a WEBP, PNG, or SVG attachment (maximum 15 MB).

### Docker

```bash
test -f .env || cp .env.example .env
# Edit .env first: set both tokens and your own ZIPLINE_URL.
docker build -t bombagif .
docker run --rm --env-file .env bombagif
```

For always-on operation, deploy the image with a restart policy and pass all three settings (`DISCORD_TOKEN`, `ZIPLINE_TOKEN`, and `ZIPLINE_URL`) via your host's secret manager or protected environment file; do not bake credentials into the image. The Dockerfile installs Cairo, so no separate host Cairo setup is needed when running in that container.

## Publishing and secret hygiene

- Keep `.env` untracked. [`.gitignore`](.gitignore) excludes it, virtual environments, and Python bytecode; [`.dockerignore`](.dockerignore) also prevents it entering Docker build context.
- Public publication does not automatically clean old Git history. Check whether your existing `.env` or tokens were committed previously; if so, revoke the tokens and remove the sensitive history before making the repository public.
- Commit `.env.example`, never your real `.env`. Before publishing, inspect staged files and history for tokens, private endpoints, and personal data. Revoke any credential that was accidentally exposed; deleting it from the latest commit alone does not invalidate a leaked token.
- The Discord bot token and Zipline token are server-side secrets. A user-installed app does not expose them to users or put them in the install URL.

## Checks

`python -m unittest discover -s tests -v` runs local conversion and mocked Zipline tests; it does not call Discord or the production Zipline host.
