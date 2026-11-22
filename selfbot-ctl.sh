#!/usr/bin/env bash
# Install, update and run the Bombagif self-bot as a systemd service on an Ubuntu/Debian VM.
#
#   git clone https://github.com/koryei/Bombagifs.git && cd Bombagifs && ./selfbot-ctl.sh install
#   ./selfbot-ctl.sh update          # pull the latest main, restart, roll back if it does not stay up
#   ./selfbot-ctl.sh auto on 15      # optional: check for updates every 15 minutes
#   ./selfbot-ctl.sh status | logs | doctor | restart | stop | start | version
#
# Secrets live in .env (mode 600) and are never printed. data/ (allowlist, taste, opt-outs) is never touched by updates.
# Portable bash (3.2+) so the same file can be unit-tested on a Mac.
set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="${SELFBOT_SERVICE:-bombagif-selfbot}"
VENV="$DIR/.venv-selfbot"
HEALTH_SECONDS="${SELFBOT_HEALTH_SECONDS:-20}"
UNIT_DIR="${SELFBOT_UNIT_DIR:-/etc/systemd/system}"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_CYAN=$'\033[36m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_CYAN=''; C_GREEN=''; C_YELLOW=''; C_RED=''
fi
QUIET=false
say() { $QUIET || printf '%b\n' "$*"; }
info() { say "${C_CYAN}[*]${C_RESET} $*"; }
ok() { say "${C_GREEN}[+]${C_RESET} $*"; }
warn() { printf '%b\n' "${C_YELLOW}[!]${C_RESET} $*" >&2; }
die() { printf '%b\n' "${C_RED}[x]${C_RESET} $*" >&2; exit 1; }

# --- systemd helpers (SELFBOT_CTL_SYSTEMCTL replaces systemctl in tests) --------------------------------------
sc_read() { if [[ -n "${SELFBOT_CTL_SYSTEMCTL:-}" ]]; then "$SELFBOT_CTL_SYSTEMCTL" "$@"; else systemctl "$@"; fi; }
sc_write() {
  if [[ -n "${SELFBOT_CTL_SYSTEMCTL:-}" ]]; then "$SELFBOT_CTL_SYSTEMCTL" "$@"
  elif [[ $EUID -eq 0 ]]; then systemctl "$@"
  else sudo systemctl "$@"; fi
}
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }

py() { "${SELFBOT_CTL_PYTHON:-$VENV/bin/python}" "$@"; }
git_() { git -C "$DIR" "$@"; }

version() {
  git_ describe --tags --always --dirty 2>/dev/null || git_ rev-parse --short HEAD 2>/dev/null || echo unknown
}

# --- .env -----------------------------------------------------------------------------------------------------
# set_env KEY  (value taken from the variable named by $2, so it never appears in argv or the output)
set_env() {
  local key="$1" from="$2"
  KEY="$key" VAL="${!from}" ENVFILE="$DIR/.env" python3 - <<'PY'
import os, re, tempfile
path, key, val = os.environ["ENVFILE"], os.environ["KEY"], os.environ["VAL"]
quoted = "'" + val.replace("\\", "\\\\").replace("'", "\\'") + "'"
lines = open(path).read().split("\n") if os.path.exists(path) else []
live = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
commented = re.compile(r"^\s*#\s*" + re.escape(key) + r"\s*=")
index = next((i for i, line in enumerate(lines) if live.match(line)), None)
if index is None:
    index = next((i for i, line in enumerate(lines) if commented.match(line)), None)
if index is None:
    lines.append(f"{key}={quoted}")
else:
    lines[index] = f"{key}={quoted}"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".env.")
with os.fdopen(fd, "w") as handle:
    handle.write("\n".join(lines).rstrip("\n") + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

env_has() {  # env_has KEY: non-empty value in .env, without printing it
  KEY="$1" ENVFILE="$DIR/.env" python3 - <<'PY'
import os
from dotenv import dotenv_values
value = dotenv_values(os.environ["ENVFILE"]).get(os.environ["KEY"])
# the placeholder URL that .env.example ships with does not count as configured
raise SystemExit(0 if value and "your-zipline.example.com" not in value else 1)
PY
}

TTY="${SELFBOT_CTL_TTY:-/dev/tty}"

# ask <var> <prompt> [hidden]: read one line into the caller's variable. (Locals here are underscore-prefixed so they
# can never shadow the variable being filled.)
ask() {
  local _var="$1" _prompt="$2" _hidden="${3:-}" _reply=''
  # Answers come from fd 3 (opened once by collect_env), so a scripted file is consumed line by line like a terminal.
  if [[ -n "$_hidden" ]]; then read -r -s -p "$_prompt" _reply <&3 || die "No more input; edit $DIR/.env by hand, then re-run install."; printf '\n' >&2
  else read -r -p "$_prompt" _reply <&3 || die "No more input; edit $DIR/.env by hand, then re-run install."; fi
  printf -v "$_var" '%s' "$_reply"
}

# prompt_env <KEY> <prompt> <hidden|plain> <required|optional>: ask only when the key is not set yet.
prompt_env() {
  local key="$1" prompt="$2" mode="$3" need="$4" answer=''
  env_has "$key" && return 0
  while :; do
    ask answer "$prompt" "$([[ "$mode" == hidden ]] && echo hidden)"
    if [[ -n "$answer" ]]; then set_env "$key" answer; return 0; fi
    [[ "$need" == optional ]] && return 0
    warn "$key is required."
  done
}

collect_env() {
  if [[ ! -f "$DIR/.env" ]]; then
    umask 077
    cp "$DIR/.env.example" "$DIR/.env"
  fi
  chmod 600 "$DIR/.env"
  [[ -r "$TTY" ]] || die "Needs a terminal. Edit $DIR/.env by hand (see .env.example), then run: ./selfbot-ctl.sh install"
  exec 3<"$TTY"
  prompt_env SELFBOT_TOKEN "Self-bot account token (hidden; use an alt account): " hidden required
  prompt_env ZIPLINE_URL "Your Zipline URL (https://...): " plain required
  prompt_env ZIPLINE_TOKEN "Zipline API token (hidden): " hidden required
  prompt_env SELFBOT_OWNER_ID "Your Discord user ID (the only person who can use !on etc.): " plain required
  prompt_env INCEPTION_API_KEY "Inception API key for the clip judge (hidden, Enter to skip): " hidden optional
  exec 3<&-
  return 0
}

# --- the units ------------------------------------------------------------------------------------------------
render_unit() {
  local user="$1" group="$2"
  cat <<UNIT
[Unit]
Description=Bombagif self-bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$user
Group=$group
WorkingDirectory=$DIR
ExecStart=$VENV/bin/python $DIR/selfbot.py
Restart=on-failure
RestartSec=10
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=SELFBOT_UI=json
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
}

render_update_unit() {
  local user="$1" group="$2"
  cat <<UNIT
[Unit]
Description=Update the Bombagif self-bot

[Service]
Type=oneshot
User=$user
Group=$group
WorkingDirectory=$DIR
ExecStart=$DIR/selfbot-ctl.sh update --quiet
UNIT
}

render_timer() {
  local minutes="$1"
  cat <<UNIT
[Unit]
Description=Check for Bombagif self-bot updates

[Timer]
OnBootSec=5min
OnUnitActiveSec=${minutes}min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT
}

render_sudoers() {  # lets the service user restart only this service without a password (for unattended updates)
  local user="$1"
  printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart %s.service, /usr/bin/systemctl start %s.service, /usr/bin/systemctl stop %s.service\n' \
    "$user" "$SERVICE" "$SERVICE" "$SERVICE"
}

install_file() {  # install_file <content-producing function + args...> -- <dest> <mode>
  local dest="${*: -2:1}" mode="${*: -1}" tmp
  tmp="$(mktemp)"
  "${@:1:$#-3}" >"$tmp"
  as_root install -o root -g root -m "$mode" "$tmp" "$dest"
  rm -f "$tmp"
}

# --- commands -------------------------------------------------------------------------------------------------
find_python311() {
  local candidate
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s' "$candidate"; return 0
    fi
  done
  return 1
}

cmd_install() {
  [[ -r /etc/os-release ]] && . /etc/os-release
  case "${ID:-}${ID_LIKE:-}" in *ubuntu*|*debian*) ;; *) die "This installer supports Ubuntu/Debian. Run selfbot.py by hand elsewhere (see the README)." ;; esac
  command -v systemctl >/dev/null 2>&1 || die "systemd is required."
  local python_bin user group
  user="$(id -un)"; group="$(id -gn)"
  info "Installing system packages (Python venv, FFmpeg, Cairo, git) with sudo..."
  as_root apt-get update -qq
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv ffmpeg libcairo2 git >/dev/null
  python_bin="$(find_python311)" || die "Python 3.11+ is required (try Ubuntu 22.04+ or install python3.11)."
  info "Creating the isolated environment ($VENV)..."
  "$python_bin" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r "$DIR/requirements-selfbot.txt"
  collect_env
  mkdir -p "$DIR/data"; chmod 700 "$DIR/data"
  info "Registering $SERVICE.service..."
  install_file render_unit "$user" "$group" -- "$UNIT_DIR/$SERVICE.service" 0644
  sc_write daemon-reload
  sc_write enable --now "$SERVICE.service"
  ok "Installed $(version) and started. Scouting stays off until you run !on in a chat."
  say "${C_DIM}Next: ./selfbot-ctl.sh status   ./selfbot-ctl.sh logs -f   ./selfbot-ctl.sh update${C_RESET}"
}

wait_healthy() {  # healthy = active for the whole window and no restart since $1 (the NRestarts count before)
  local before="$1" waited=0 now
  while (( waited < HEALTH_SECONDS )); do
    sleep 1; waited=$((waited + 1))
    [[ "$(sc_read is-active "$SERVICE.service" 2>/dev/null || true)" == active ]] || return 1
    now="$(sc_read show -p NRestarts --value "$SERVICE.service" 2>/dev/null || echo 0)"
    [[ "$now" == "$before" ]] || return 1
  done
  return 0
}

deps_changed() { ! git_ diff --quiet "$1" "$2" -- requirements-selfbot.txt; }

install_deps() {
  if [[ -n "${SELFBOT_CTL_PIP:-}" ]]; then "$SELFBOT_CTL_PIP" -r "$DIR/requirements-selfbot.txt"
  else py -m pip install --quiet -r "$DIR/requirements-selfbot.txt"; fi
}

smoke_ok() {
  if [[ -n "${SELFBOT_CTL_SMOKE:-}" ]]; then (cd "$DIR" && eval "$SELFBOT_CTL_SMOKE") >/dev/null 2>&1
  else (cd "$DIR" && py -c 'import selfbot' >/dev/null 2>&1); fi
}

cmd_update() {
  git_ diff --quiet && git_ diff --cached --quiet || die "This checkout has local changes; refusing to update. Run: git -C $DIR status"
  local branch old new
  branch="$(git_ rev-parse --abbrev-ref HEAD)"
  [[ "$branch" != HEAD ]] || die "Detached HEAD; check out a branch first (git checkout main)."
  git_ fetch --quiet origin "$branch" || die "Could not reach the repository (git fetch failed)."
  old="$(git_ rev-parse HEAD)"
  new="$(git_ rev-parse "origin/$branch")"
  if [[ "$old" == "$new" ]]; then ok "Already up to date ($(version))."; return 0; fi
  git_ merge-base --is-ancestor "$old" "$new" || die "Local and remote history diverged; not updating. Fix it with git, then retry."
  info "Updating $(git_ rev-parse --short "$old") -> $(git_ rev-parse --short "$new"):"
  say "$(git_ log --format='    %h %s' "$old..$new")"
  local before_restarts
  before_restarts="$(sc_read show -p NRestarts --value "$SERVICE.service" 2>/dev/null || echo 0)"
  git_ merge --quiet --ff-only "$new"
  local deps=false
  if deps_changed "$old" "$new"; then deps=true; info "Dependencies changed; installing..."; fi
  if { ! $deps || install_deps; } && smoke_ok; then
    sc_write restart "$SERVICE.service"
    before_restarts="$(sc_read show -p NRestarts --value "$SERVICE.service" 2>/dev/null || echo 0)"
    if wait_healthy "$before_restarts"; then ok "Updated to $(version) and healthy."; return 0; fi
    warn "The new version did not stay up."
  else
    warn "The new version failed its pre-restart check."
  fi
  warn "Rolling back to $(git_ rev-parse --short "$old")."
  git_ reset --quiet --hard "$old"
  if $deps; then install_deps || warn "Could not reinstall the old dependencies; check them by hand."; fi
  sc_write restart "$SERVICE.service"
  die "Update failed and was rolled back to $(version). Look at: ./selfbot-ctl.sh logs"
}

cmd_auto() {
  local action="${1:-}" minutes="${2:-15}" user group
  user="$(id -un)"; group="$(id -gn)"
  case "$action" in
    on)
      [[ "$minutes" =~ ^[0-9]+$ && "$minutes" -ge 5 ]] || die "Use at least 5 minutes: ./selfbot-ctl.sh auto on 15"
      install_file render_sudoers "$user" -- "/etc/sudoers.d/$SERVICE" 0440
      if [[ -z "${SELFBOT_CTL_SYSTEMCTL:-}" ]] && ! as_root visudo -cf "/etc/sudoers.d/$SERVICE" >/dev/null; then
        as_root rm -f "/etc/sudoers.d/$SERVICE"; die "Could not validate the sudoers entry; auto-update not enabled."
      fi
      install_file render_update_unit "$user" "$group" -- "$UNIT_DIR/$SERVICE-update.service" 0644
      install_file render_timer "$minutes" -- "$UNIT_DIR/$SERVICE-update.timer" 0644
      sc_write daemon-reload
      sc_write enable --now "$SERVICE-update.timer"
      ok "Checking for updates every $minutes minutes (it rolls back a version that won't start)."
      ;;
    off)
      sc_write disable --now "$SERVICE-update.timer" || true
      as_root rm -f "$UNIT_DIR/$SERVICE-update.timer" "$UNIT_DIR/$SERVICE-update.service" "/etc/sudoers.d/$SERVICE"
      sc_write daemon-reload
      ok "Auto-update is off. Use ./selfbot-ctl.sh update when you want one."
      ;;
    *) die "Use: ./selfbot-ctl.sh auto on [minutes]   or   ./selfbot-ctl.sh auto off" ;;
  esac
}

cmd_status() {
  say "${C_BOLD}Bombagif self-bot${C_RESET} $(version)"
  say "  service:    $(sc_read is-active "$SERVICE.service" 2>/dev/null || true) (enabled: $(sc_read is-enabled "$SERVICE.service" 2>/dev/null || echo no))"
  say "  auto-update: $(sc_read is-active "$SERVICE-update.timer" 2>/dev/null || echo off)"
  [[ -e "$DIR/data/captcha-blocked.json" ]] && say "  ${C_YELLOW}paused by a CAPTCHA${C_RESET}: finish it in a browser on the account, delete data/captcha-blocked.json, restart."
  return 0
}

cmd_logs() {
  local follow=()
  [[ "${1:-}" == "-f" ]] && follow=(-f)
  journalctl -u "$SERVICE.service" -n 60 --no-pager ${follow[@]+"${follow[@]}"}
}

cmd_doctor() {
  local bad=0
  check() { if "${@:2}" >/dev/null 2>&1; then ok "$1"; else warn "$1: MISSING"; bad=1; fi; }
  check "python 3.11+ in the venv" py -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
  check "discord.py-self imports" py -c 'import discord; assert hasattr(discord, "CaptchaRequired")'
  check "pillow + cairosvg import" py -c 'import PIL, cairosvg'
  check "ffmpeg installed" command -v ffmpeg
  check ".env exists" test -f "$DIR/.env"
  for key in SELFBOT_TOKEN ZIPLINE_URL ZIPLINE_TOKEN SELFBOT_OWNER_ID; do check "$key is set" env_has "$key"; done
  env_has INCEPTION_API_KEY || env_has SCOUT_LLM_API_KEY || warn "No clip-judge key (INCEPTION_API_KEY or SCOUT_LLM_*): clip scouting can't post."
  [[ "$(stat -c %a "$DIR/.env" 2>/dev/null || stat -f %Lp "$DIR/.env" 2>/dev/null)" == 600 ]] || warn ".env should be mode 600: chmod 600 $DIR/.env"
  [[ -e "$DIR/data/captcha-blocked.json" ]] && warn "Paused by a CAPTCHA (data/captcha-blocked.json)."
  return $bad
}

usage() {
  cat <<USAGE
Usage: ./selfbot-ctl.sh <command>
  install            set up the venv, .env, and the systemd service (asks for sudo once)
  update [--quiet]   fast-forward to the latest commit, restart, roll back if it won't stay up
  auto on [min]|off  check for updates on a timer (default every 15 min)
  status             service state and version
  logs [-f]          recent logs (follow with -f)
  doctor             check the install without printing secrets
  start|stop|restart control the service
  version            the checked-out version
USAGE
}

main() {
  local command="${1:-}"
  [[ $# -gt 0 ]] && shift
  for arg in "$@"; do [[ "$arg" == --quiet ]] && QUIET=true; done
  case "$command" in
    install) cmd_install ;;
    update) cmd_update ;;
    auto) cmd_auto "$@" ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    doctor) cmd_doctor ;;
    start|stop|restart) sc_write "$command" "$SERVICE.service" && ok "$command: done." ;;
    version) version ;;
    ''|-h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
