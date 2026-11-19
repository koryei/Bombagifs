# Bombagif

Discord bot that converts images/videos (PNG, JPG, AVIF, WEBP, SVG, MP4, WebM) to optimized GIFs
(~1 MB target), uploads them to the operator's own Zipline instance, and returns a link.

## Layout

- `main.py` — user-installed slash-command app (`/gif`, up to 5 files), uses `discord.py`.
- `tests/` — `python -m unittest discover -s tests -v`; `bash tests/test_installer.sh`.
- `install.sh`, `Dockerfile`, `compose.yaml` — deployment for the app.

## Current work: self-bot front door

Adding a user-account "self-bot" (like github.com/koryei/BombaclatAI) that anyone who has DMed it
first can ping with an image, or ping while replying to media, to get a GIF link back.
Design: `docs/superpowers/specs/2026-11-18-selfbot-design.md`. Plan: `PLAN.md` (once written).

Key decisions:
- `discord.py-self` and `discord.py` both install as `discord`, so the self-bot is a separate process
  and venv; the conversion pipeline is extracted into a discord-free `bombagif_core.py`.
- Consent model: only users who DMed the account first are served; everyone else is ignored silently.
- Small atomic commits; tests stay green at each commit.

## Status

Design written, awaiting approval. No self-bot code yet.
