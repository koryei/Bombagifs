#!/usr/bin/env bash
# Install, update and run the Bombagif self-bot as a systemd service on an Ubuntu/Debian VM.
#
#   git clone https://github.com/koryei/Bombagifs.git && cd Bombagifs && ./selfbot-ctl.sh install
#   ./selfbot-ctl.sh update          # pull the latest main, restart, roll back if it does not stay up
#   ./selfbot-ctl.sh auto on 15      # optional: check for updates every 15 minutes
#   ./selfbot-ctl.sh dashboard       # one screen: state, scout, counters, recent activity
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
SUDOERS_DIR="${SELFBOT_CTL_SUDOERS_DIR:-/etc/sudoers.d}"

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
sudo_() { "${SELFBOT_CTL_SUDO:-sudo}" "$@"; }
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo_ "$@"; fi; }
can_root() {  # true when root is reachable without hanging on a password prompt
  [[ $EUID -eq 0 ]] && return 0
  if [[ -t 0 ]]; then sudo_ -v 2>/dev/null; else sudo_ -n true 2>/dev/null; fi
}
journal_() { if [[ -n "${SELFBOT_CTL_JOURNALCTL:-}" ]]; then "$SELFBOT_CTL_JOURNALCTL" "$@"; else journalctl "$@"; fi; }

py() { "${SELFBOT_CTL_PYTHON:-$VENV/bin/python}" "$@"; }
git_() { git -C "$DIR" "$@"; }

version() {
  git_ describe --tags --always --dirty 2>/dev/null || git_ rev-parse --short HEAD 2>/dev/null || echo unknown
}

opus_version() {  # the libopus version the venv can load, empty when it cannot
  # is_loaded() is False until something loads the library, so ask for the version: that loads it and raises
  # OpusNotLoaded when the host has none.
  py -c 'import discord.opus; print(discord.opus._OpusStruct.get_opus_version())' 2>/dev/null
}

opus_ok() {  # true when the venv can load libopus, which voice clips need to decode
  [[ -n "$(opus_version)" ]]
}

# --- .env -----------------------------------------------------------------------------------------------------
# You bring your own .env (e.g. scp it to $DIR/.env); this script never prompts for secrets. `training` rewrites only
# the scout settings it manages, and never prints a value.
REQUIRED_KEYS="SELFBOT_TOKEN ZIPLINE_URL ZIPLINE_TOKEN"
TRAINING_KEYS="SELFBOT_SCOUT_MODE SCOUT_REVIEW SCOUT_REVIEW_PER_HOUR SCOUT_REVIEW_BATCH SCOUT_REVIEW_BATCH_MINUTES SCOUT_SCREEN SCOUT_NEAR_MISS_DECIDE_FUNNY SCOUT_NEAR_MISS_FINAL_FUNNY SCOUT_EXAMPLES SCOUT_MANUAL"

env_has() {  # env_has KEY: non-empty value in .env, without printing it
  KEY="$1" ENVFILE="$DIR/.env" python3 - <<'PY'
import os
from dotenv import dotenv_values
value = dotenv_values(os.environ["ENVFILE"]).get(os.environ["KEY"])
# the placeholder URL that .env.example ships with does not count as configured
raise SystemExit(0 if value and "your-zipline.example.com" not in value else 1)
PY
}

env_missing() {  # prints the names of required keys that are not set; succeeds only when none are missing
  local key missing=""
  [[ -f "$DIR/.env" ]] || { printf '%s' "$REQUIRED_KEYS"; return 1; }
  for key in $REQUIRED_KEYS; do env_has "$key" || missing="$missing $key"; done
  missing="${missing# }"
  [[ -z "$missing" ]] || { printf '%s' "$missing"; return 1; }
}

ensure_env() {  # a blank template when there is no .env yet; always mode 600
  if [[ ! -f "$DIR/.env" ]]; then
    (umask 077; cp "$DIR/.env.example" "$DIR/.env")
  fi
  chmod 600 "$DIR/.env"
}

env_get() {  # env_get KEY: the value in .env (empty when unset); only used for non-secret settings
  KEY="$1" ENVFILE="$DIR/.env" python3 - <<'PY'
import os
from pathlib import Path
from dotenv import dotenv_values
path = Path(os.environ["ENVFILE"])
value = dotenv_values(path).get(os.environ["KEY"]) if path.exists() else None
print(value or "")
PY
}

env_set() {  # env_set KEY VALUE: set one key in .env (atomic, mode 600, comments kept, value never printed)
  KEY="$1" VALUE="$2" ENVFILE="$DIR/.env" python3 - <<'PY'
import os
import tempfile
from pathlib import Path

path = Path(os.environ["ENVFILE"])
key, value = os.environ["KEY"], os.environ["VALUE"]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
out, replaced = [], False
for line in lines:
    if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
        out.append(f"{key}={value}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"{key}={value}")
fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env-", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write("\n".join(out) + "\n")
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}

env_help() {  # what to do when the keys are missing
  warn "Missing in $DIR/.env: $1"
  say "  Copy your own .env onto the VM, for example from your computer:"
  say "    ${C_BOLD}scp .env $(id -un)@<this-vm>:$DIR/.env${C_RESET}"
  say "  then run:  ${C_BOLD}./selfbot-ctl.sh restart${C_RESET}   (or start)"
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

render_sudoers() {  # lets the service user restart this service, and kick off its update unit, without a password
  local user="$1"
  printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart %s.service, /usr/bin/systemctl start %s.service, /usr/bin/systemctl stop %s.service, /usr/bin/systemctl start %s-update.service\n' \
    "$user" "$SERVICE" "$SERVICE" "$SERVICE" "$SERVICE"
}

sudoers_current() {  # true when the installed rule already matches what this script would write
  local dest="$SUDOERS_DIR/$SERVICE"
  [[ -r "$dest" ]] && [[ "$(cat "$dest" 2>/dev/null)" == "$(render_sudoers "$(id -un)")" ]]
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
  ensure_env
  mkdir -p "$DIR/data"; chmod 700 "$DIR/data"
  info "Registering $SERVICE.service..."
  install_file render_unit "$user" "$group" -- "$UNIT_DIR/$SERVICE.service" 0644
  sc_write daemon-reload
  local missing
  if missing="$(env_missing)"; then
    sc_write enable --now "$SERVICE.service"
    ok "Installed $(version) and started. Scouting stays off until you run !on in a chat."
    say "${C_DIM}Next: ./selfbot-ctl.sh status   ./selfbot-ctl.sh logs -f   ./selfbot-ctl.sh update${C_RESET}"
  else
    sc_write enable "$SERVICE.service"
    ok "Installed $(version); it will start on boot, but it is not running yet."
    env_help "$missing"
  fi
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

write_notice() {  # write_notice <from> <to> <outcome> <commits: "sha<TAB>subject" lines>
  # The bot reads this on its next boot and DMs it to everyone on the allowlist, then deletes it.
  local from="$1" to="$2" outcome="$3" commits="${4:-}"
  NOTICE="$DIR/data/update-notice.json" FROM="$from" TO="$to" OUTCOME="$outcome" COMMITS="$commits" python3 - <<'PY'
import json, os, time
from pathlib import Path

path = Path(os.environ["NOTICE"])
commits = []
for line in os.environ.get("COMMITS", "").splitlines():
    sha, _, subject = line.partition("\t")
    if sha.strip():
        commits.append({"sha": sha.strip(), "subject": subject.strip()})
data = {
    "from": os.environ["FROM"],
    "to": os.environ["TO"],
    "outcome": os.environ["OUTCOME"],
    "at": int(time.time()),
    "commits": commits,
}
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(path.name + ".tmp")
tmp.write_text(json.dumps(data), encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

busy_jobs() {  # prints the in-flight job count; 0 when the marker is missing, stale or unreadable
  BUSY="$DIR/data/busy.json" STALE="${SELFBOT_BUSY_STALE_SECONDS:-120}" python3 - <<'PY'
import json, os, time
from pathlib import Path

try:
    data = json.loads(Path(os.environ["BUSY"]).read_text(encoding="utf-8"))
    jobs = int(data.get("jobs") or 0)
    at = float(data.get("at") or 0)
except (OSError, ValueError, TypeError, AttributeError):
    print(0)
else:
    fresh = time.time() - at <= float(os.environ["STALE"])
    print(jobs if jobs > 0 and fresh else 0)
PY
}

wait_for_idle() {  # wait_for_idle: let in-flight work finish before the restart kills it
  local waited=0 limit="${SELFBOT_IDLE_WAIT_SECONDS:-300}" step="${SELFBOT_IDLE_STEP_SECONDS:-10}" jobs
  while :; do
    jobs="$(busy_jobs)"
    [[ "$jobs" == 0 ]] && return 0
    if (( waited >= limit )); then
      warn "Still busy with $jobs job(s) after ${waited}s; updating anyway."
      return 0
    fi
    info "Busy with $jobs job(s); checking again in ${step}s."
    sleep "$step"
    waited=$((waited + step))
  done
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
  local before_restarts commits
  commits="$(git_ log --format='%h%x09%s' "$old..$new")"
  before_restarts="$(sc_read show -p NRestarts --value "$SERVICE.service" 2>/dev/null || echo 0)"
  git_ merge --quiet --ff-only "$new"
  # The bot's right to start its update unit lives in a root-owned file that only this script writes. When the
  # code changes what the bot needs, that file has to follow, or !update is refused forever after one update.
  local quiet_flag=""; $QUIET && quiet_flag="--quiet"
  bash "$DIR/selfbot-ctl.sh" update-service --if-needed $quiet_flag \
    || warn "Could not refresh the update unit; !update may be refused."
  local deps=false
  if deps_changed "$old" "$new"; then deps=true; info "Dependencies changed; installing..."; fi
  if { ! $deps || install_deps; } && smoke_ok; then
    wait_for_idle
    write_notice "$(git_ rev-parse --short "$old")" "$(git_ rev-parse --short "$new")" updated "$commits"
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
  write_notice "$(git_ rev-parse --short "$old")" "$(git_ rev-parse --short "$new")" rolled-back "$commits"
  sc_write restart "$SERVICE.service"
  die "Update failed and was rolled back to $(version). Look at: ./selfbot-ctl.sh logs"
}

cmd_update_service() {  # install just the update unit, so !update works without the timer
  local user group soft=false
  [[ "${1:-}" == "--if-needed" ]] && soft=true
  user="$(id -un)"; group="$(id -gn)"
  if $soft && sudoers_current && [[ -f "$UNIT_DIR/$SERVICE-update.service" ]]; then return 0; fi
  # Soft runs come from the update unit, which has no terminal: never sit waiting for a password there.
  if $soft && ! can_root; then
    warn "No root, so !update may be refused. Run: ./selfbot-ctl.sh update-service"
    return 0
  fi
  if ! install_file render_sudoers "$user" -- "$SUDOERS_DIR/$SERVICE" 0440; then
    $soft && { warn "Could not refresh $SUDOERS_DIR/$SERVICE; !update may still be refused."; return 0; }
    die "Could not write $SUDOERS_DIR/$SERVICE (needs root)."
  fi
  if [[ -z "${SELFBOT_CTL_SYSTEMCTL:-}" ]] && ! as_root visudo -cf "$SUDOERS_DIR/$SERVICE" >/dev/null; then
    as_root rm -f "$SUDOERS_DIR/$SERVICE"
    $soft && { warn "The sudoers entry did not validate; !update may still be refused."; return 0; }
    die "Could not validate the sudoers entry; !update not enabled."
  fi
  install_file render_update_unit "$user" "$group" -- "$UNIT_DIR/$SERVICE-update.service" 0644
  sc_write daemon-reload
  ok "!update is enabled: it runs ./selfbot-ctl.sh update in its own unit, so the restart cannot kill it."
}

cmd_auto() {
  local action="${1:-}" minutes="${2:-15}" user group
  user="$(id -un)"; group="$(id -gn)"
  case "$action" in
    on)
      [[ "$minutes" =~ ^[0-9]+$ && "$minutes" -ge 1 ]] || die "Use at least 1 minute: ./selfbot-ctl.sh auto on 1"
      install_file render_sudoers "$user" -- "$SUDOERS_DIR/$SERVICE" 0440
      if [[ -z "${SELFBOT_CTL_SYSTEMCTL:-}" ]] && ! as_root visudo -cf "$SUDOERS_DIR/$SERVICE" >/dev/null; then
        as_root rm -f "$SUDOERS_DIR/$SERVICE"; die "Could not validate the sudoers entry; auto-update not enabled."
      fi
      install_file render_update_unit "$user" "$group" -- "$UNIT_DIR/$SERVICE-update.service" 0644
      install_file render_timer "$minutes" -- "$UNIT_DIR/$SERVICE-update.timer" 0644
      sc_write daemon-reload
      sc_write enable --now "$SERVICE-update.timer"
      ok "Checking for updates every $minutes minutes (it rolls back a version that won't start)."
      ;;
    off)
      sc_write disable --now "$SERVICE-update.timer" || true
      as_root rm -f "$UNIT_DIR/$SERVICE-update.timer" "$UNIT_DIR/$SERVICE-update.service" "$SUDOERS_DIR/$SERVICE"
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
  say "${C_DIM}Live view: ./selfbot-ctl.sh dashboard${C_RESET}"
  return 0
}

td() { if [[ -n "${SELFBOT_CTL_TIMEDATECTL:-}" ]]; then "$SELFBOT_CTL_TIMEDATECTL" "$@"; else timedatectl "$@"; fi; }

cmd_timezone() {  # timezone [Area/City]: show, or set, the VM's timezone (clip cards print clock times in it)
  local zone="${1:-}"
  if [[ -z "$zone" ]]; then
    say "Timezone: $(td show -p Timezone --value 2>/dev/null || echo unknown)   (now: $(date '+%Y-%m-%d %H:%M %Z'))"
    say "${C_DIM}Change it with: ./selfbot-ctl.sh timezone America/New_York   (list: timedatectl list-timezones)${C_RESET}"
    return 0
  fi
  [[ "$zone" =~ ^[A-Za-z0-9_+/-]+$ ]] || die "That is not a timezone name. Example: America/New_York"
  td list-timezones 2>/dev/null | grep -qx -- "$zone" || die "Unknown timezone '$zone'. List them with: timedatectl list-timezones"
  if [[ -n "${SELFBOT_CTL_TIMEDATECTL:-}" ]]; then td set-timezone "$zone"; else as_root timedatectl set-timezone "$zone"; fi
  ok "Timezone is now $zone."
  if [[ "$(sc_read is-active "$SERVICE.service" 2>/dev/null || true)" == active ]]; then
    sc_write restart "$SERVICE.service"
    ok "Restarted the bot so clip cards use it."
  fi
}

training_write() {  # training_write MODE REVIEW PER_HOUR BATCH BATCH_MINUTES NEAR_DECIDE NEAR_FINAL EXAMPLES MANUAL
  env_set SELFBOT_SCOUT_MODE "$1"
  env_set SCOUT_REVIEW "$2"
  env_set SCOUT_REVIEW_PER_HOUR "$3"
  env_set SCOUT_REVIEW_BATCH "$4"
  env_set SCOUT_REVIEW_BATCH_MINUTES "$5"
  env_set SCOUT_SCREEN all
  env_set SCOUT_NEAR_MISS_DECIDE_FUNNY "$6"
  env_set SCOUT_NEAR_MISS_FINAL_FUNNY "$7"
  env_set SCOUT_EXAMPLES "$8"
  env_set SCOUT_MANUAL "$9"
}

cmd_training() {  # training [on|off|status]: the training-phase form (dry-run, review every judged run)
  local action="${1:-form}" mode="" per_hour="" batch="" batch_minutes="" examples="" near_decide="" near_final="" manual="" answer=""
  ensure_env
  case "$action" in
    status)
      say "${C_BOLD}Training settings${C_RESET} ($DIR/.env)"
      for key in $TRAINING_KEYS; do say "  $key=$(env_get "$key")"; done
      return 0 ;;
    off)
      mode="$(env_get SELFBOT_SCOUT_MODE)"; mode="${mode:-off}"
      manual="$(env_get SCOUT_MANUAL)"; manual="${manual:-on}"
      training_write "$mode" dry-run 6 10 15 0.6 6 8 "$manual"
      ok "Review settings are back to the defaults."
      say "  SELFBOT_SCOUT_MODE is still $mode; set it to on when you are ready to post."
      ;;
    on)
      manual="$(env_get SCOUT_MANUAL)"; manual="${manual:-on}"
      training_write dry-run always 30 5 5 0 0 12 "$manual"
      ok "Training phase: dry-run, every judged run reviewed, up to 30 candidates an hour in batches of 5."
      ;;
    form)
      say "${C_BOLD}Training phase${C_RESET}: judge everything, post nothing, DM you every judged run."
      read -r -p "  Scout mode (off, dry-run, on) [dry-run]: " mode || true; mode="${mode:-dry-run}"
      read -r -p "  Review DMs per hour [30]: " per_hour || true; per_hour="${per_hour:-30}"
      read -r -p "  Candidates per DM [5]: " batch || true; batch="${batch:-5}"
      read -r -p "  Send a partial batch after how many minutes [5]: " batch_minutes || true; batch_minutes="${batch_minutes:-5}"
      read -r -p "  Review every judged run, including rejects? [Y/n]: " answer || true
      if [[ "$answer" =~ ^[Nn] ]]; then near_decide=0.6; near_final=6; else near_decide=0; near_final=0; fi
      read -r -p "  Examples per label [12]: " examples || true; examples="${examples:-12}"
      read -r -p "  Keep manual \"clip that\" clips on? [Y/n]: " answer || true
      if [[ "$answer" =~ ^[Nn] ]]; then manual=off; else manual=on; fi
      [[ "$mode" =~ ^(off|dry-run|on)$ ]] || die "mode must be off, dry-run or on"
      [[ "$per_hour" =~ ^[1-9][0-9]*$ ]] || die "review DMs per hour must be a positive whole number"
      [[ "$batch" =~ ^[1-9][0-9]*$ ]] || die "candidates per DM must be a positive whole number"
      [[ "$batch_minutes" =~ ^[1-9][0-9]*$ ]] || die "batch minutes must be a positive whole number"
      [[ "$examples" =~ ^[1-9][0-9]*$ ]] || die "examples per label must be a positive whole number"
      training_write "$mode" always "$per_hour" "$batch" "$batch_minutes" "$near_decide" "$near_final" "$examples" "$manual"
      ok "Saved to $DIR/.env."
      ;;
    *) die "training takes: on, off, status (or nothing for the form)" ;;
  esac
  if [[ "$(sc_read is-active "$SERVICE.service" 2>/dev/null || true)" == active ]]; then
    sc_write restart "$SERVICE.service"
    ok "Restarted the bot."
  else
    say "  Start it with: ./selfbot-ctl.sh start"
  fi
}

cmd_voice() {  # voice [on|off|status]: install voice receive and turn call clips on
  local action="${1:-status}"
  ensure_env
  case "$action" in
    on)
      info "Installing voice receive (PyNaCl + discord-ext-voice-recv + davey)..."
      # --no-deps matters: voice-recv depends on discord.py, which would replace discord.py-self and break the bot.
      # davey is pinned by hand: discord.py-self asks for 0.1.0, which is not on PyPI, and Discord closes a voice
      # connection without the DAVE protocol (close code 4017).
      if [[ -n "${SELFBOT_CTL_PIP:-}" ]]; then
        "$SELFBOT_CTL_PIP" PyNaCl && "$SELFBOT_CTL_PIP" --no-deps discord-ext-voice-recv && "$SELFBOT_CTL_PIP" davey==0.1.7
      else
        py -m pip install --quiet PyNaCl && py -m pip install --quiet --no-deps discord-ext-voice-recv && py -m pip install --quiet davey==0.1.7
      fi
      env_set SCOUT_VOICE on
      opus_ok || warn "libopus is missing: sudo apt install libopus0 (voice clips need it to decode)"
      ok "Voice clips are on. Join a call, then run !join in a text channel."
      ;;
    off)
      env_set SCOUT_VOICE off
      ok "Voice clips are off."
      ;;
    status)
      say "${C_BOLD}Voice clips${C_RESET} ($DIR/.env)"
      for key in SCOUT_VOICE SCOUT_VOICE_SECONDS SCOUT_VOICE_KEEP_SECONDS SCOUT_VOICE_WAKE SCOUT_VOICE_WAKE_KEYWORD; do
        say "  $key=$(env_get "$key")"
      done
      if py -c 'import discord.ext.voice_recv' >/dev/null 2>&1; then ok "voice receive is installed"
      else warn "voice receive is not installed: ./selfbot-ctl.sh voice on"; fi
      if py -c 'import davey' >/dev/null 2>&1; then ok "davey is installed"
      else warn "davey is missing: ./selfbot-ctl.sh voice on"; fi
      if opus="$(opus_version)" && [[ -n "$opus" ]]; then ok "libopus $opus is available"
      else warn "libopus is missing: sudo apt install libopus0"; fi
      return 0
      ;;
    *) die "voice takes: on, off, status" ;;
  esac
  if [[ "$(sc_read is-active "$SERVICE.service" 2>/dev/null || true)" == active ]]; then
    sc_write restart "$SERVICE.service"
    ok "Restarted the bot."
  else
    say "  Start it with: ./selfbot-ctl.sh start"
  fi
}

cmd_logs() {
  local follow=()
  [[ "${1:-}" == "-f" ]] && follow=(-f)
  # Entries written before the switch to text logs are JSON: reformat them so a peek reads the same throughout.
  if [[ -x "${SELFBOT_CTL_PYTHON:-$VENV/bin/python}" ]]; then
    journal_ -u "$SERVICE.service" -n 60 --no-pager ${follow[@]+"${follow[@]}"} | py "$DIR/logfmt.py"
  else
    journal_ -u "$SERVICE.service" -n 60 --no-pager ${follow[@]+"${follow[@]}"}
  fi
}

cmd_dashboard() {  # dashboard [--once]: the at-a-glance screen, live until Ctrl-C
  [[ -x "${SELFBOT_CTL_PYTHON:-$VENV/bin/python}" ]] || die "The venv is missing: run ./selfbot-ctl.sh install"
  py "$DIR/dashboard.py" --status "$DIR/data/status.json" --service "$SERVICE" --version "$(version)" \
    --systemctl "${SELFBOT_CTL_SYSTEMCTL:-systemctl}" "$@"
}

cmd_doctor() {
  local bad=0
  check() { if "${@:2}" >/dev/null 2>&1; then ok "$1"; else warn "$1: MISSING"; bad=1; fi; }
  check "python 3.11+ in the venv" py -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
  check "discord.py-self imports" py -c 'import discord; assert hasattr(discord, "CaptchaRequired")'
  check "pillow + cairosvg import" py -c 'import PIL, cairosvg'
  check "ffmpeg installed" command -v ffmpeg
  check ".env exists" test -f "$DIR/.env"
  for key in $REQUIRED_KEYS; do check "$key is set" env_has "$key"; done
  say "${C_DIM}timezone: $(td show -p Timezone --value 2>/dev/null || echo unknown) (the bot also honours TZ in .env)${C_RESET}"
  env_has SELFBOT_OWNER_ID || warn "SELFBOT_OWNER_ID is not set: nobody can use !on / !help (conversions still work)."
  env_has INCEPTION_API_KEY || env_has SCOUT_LLM_API_KEY || warn "No clip-judge key (INCEPTION_API_KEY or SCOUT_LLM_*): clip scouting can't post."
  if py -c 'import discord.ext.voice_recv' >/dev/null 2>&1; then
    py -c 'import davey' >/dev/null 2>&1 || warn "davey is missing: ./selfbot-ctl.sh voice on (Discord requires it for voice)"
    opus_ok || warn "libopus is missing: sudo apt install libopus0 (voice clips need it to decode)"
  else
    say "  ${C_DIM}voice receive: not installed (./selfbot-ctl.sh voice on)${C_RESET}"
  fi
  [[ "$(stat -c %a "$DIR/.env" 2>/dev/null || stat -f %Lp "$DIR/.env" 2>/dev/null)" == 600 ]] || warn ".env should be mode 600: chmod 600 $DIR/.env"
  [[ -e "$DIR/data/captcha-blocked.json" ]] && warn "Paused by a CAPTCHA (data/captcha-blocked.json)."
  return $bad
}

usage() {
  cat <<USAGE
Usage: ./selfbot-ctl.sh <command>
  install            set up the venv and the systemd service (you supply .env; asks for sudo once)
  update [--quiet]   fast-forward to the latest commit, restart, roll back if it won't stay up
  auto on [min]|off  check for updates on a timer (default every 15 min)
  update-service     let the bot run !update from Discord (installs the update unit, no timer)
  dashboard [--once] one screen: state, scout, counters, recent activity (live until Ctrl-C)
  status             service state and version
  logs [-f]          recent logs (follow with -f)
  timezone [zone]    show or set the VM's timezone (clip cards print clock times in it)
  training [on|off|status]  dry-run training form: review every judged run, then rate the DMs
  voice [on|off|status]     install voice receive and clip what people say in a call
  doctor             check the install without printing secrets
  start|stop|restart control the service
  version            the checked-out version
USAGE
}

main() {
  local missing command="${1:-}"
  [[ $# -gt 0 ]] && shift
  for arg in "$@"; do [[ "$arg" == --quiet ]] && QUIET=true; done
  case "$command" in
    install) cmd_install ;;
    update) cmd_update ;;
    update-service) cmd_update_service "$@" ;;
    auto) cmd_auto "$@" ;;
    dashboard) cmd_dashboard "$@" ;;
    status) cmd_status ;;
    timezone) cmd_timezone "$@" ;;
    training) cmd_training "$@" ;;
    voice) cmd_voice "$@" ;;
    logs) cmd_logs "$@" ;;
    doctor) cmd_doctor ;;
    stop) sc_write stop "$SERVICE.service" && ok "stop: done." ;;
    start|restart)
      ensure_env
      if missing="$(env_missing)"; then sc_write "$command" "$SERVICE.service" && ok "$command: done."
      else env_help "$missing"; exit 1; fi ;;
    version) version ;;
    ''|-h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
