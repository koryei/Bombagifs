#!/usr/bin/env bash
# Interactive public self-host installer/manager for Bombagif (Bash 3.2+ compatible).
set -Eeuo pipefail

REPO_URL="${BOMBAGIF_REPO_URL:-https://github.com/koryei/Bombagifs.git}"
BRANCH="${BOMBAGIF_BRANCH:-main}"
INSTALL_DIR="${BOMBAGIF_INSTALL_DIR:-${HOME}/Bombagif}"
INSTALL_MODE="${BOMBAGIF_INSTALL_MODE:-}"
INSTALL_ACTION="${BOMBAGIF_INSTALL_ACTION:-}"
SERVICE_NAME="bombagif"

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "" && "${TERM:-}" != "dumb" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_CYAN=$'\033[36m'
  C_PURPLE=$'\033[35m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_CYAN=''; C_PURPLE=''; C_GREEN=''; C_YELLOW=''; C_RED=''
fi

say() { printf '%b\n' "$*"; }
info() { say "${C_CYAN}[*]${C_RESET} $*"; }
ok() { say "${C_GREEN}[+]${C_RESET} $*"; }
warn() { say "${C_YELLOW}[!]${C_RESET} $*"; }
tip() { say "${C_PURPLE}[i]${C_RESET} $*"; }
fail() { say "${C_RED}[x]${C_RESET} $*"; }
die() { fail "$*" >&2; exit 1; }

banner() {
  say ""
  say "${C_BOLD}${C_PURPLE}BOMBAGIF${C_RESET}"
  say "${C_BOLD}GIFs on the fly. Your Discord app. Your Zipline. All self-hostable.${C_RESET}"
  say "${C_DIM}Install, update, check, repair, or remove Bombagif from this menu.${C_RESET}"
  say ""
}

dotenv_quote() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\'/\\\'}
  printf "'%s'" "$value"
}

read_prompt() {
  local prompt="$1" variable="$2" hidden="${3:-false}" value=''
  [[ -r /dev/tty ]] || die "Interactive setup needs a terminal. For automation, set BOMBAGIF_INSTALL_MODE and export your three credentials first."
  if [[ "$hidden" == true ]]; then
    read -r -s -p "$prompt" value </dev/tty
    printf '\n' >/dev/tty
  else
    read -r -p "$prompt" value </dev/tty
  fi
  printf -v "$variable" '%s' "$value"
}

confirm() {
  local prompt="$1" default="${2:-no}" answer=''
  read_prompt "$prompt" answer
  if [[ "$default" == "yes" ]]; then
    [[ -z "$answer" || "$answer" =~ ^[Yy] ]]
  else
    [[ "$answer" =~ ^[Yy] ]]
  fi
}

mode_label() {
  case "$1" in
    python) printf 'Python venv' ;;
    docker) printf 'Docker Compose' ;;
    systemd) printf 'Ubuntu service (systemd)' ;;
    *) printf '%s' "${1:-none}" ;;
  esac
}

choose_mode() {
  INSTALL_MODE="$(printf '%s' "$INSTALL_MODE" | tr '[:upper:]' '[:lower:]')"
  case "$INSTALL_MODE" in
    1|python|venv) INSTALL_MODE=python ;;
    2|docker|compose) INSTALL_MODE=docker ;;
    3|systemd|service) INSTALL_MODE=systemd ;;
    '')
      say "${C_BOLD}How should Bombagif run on this machine?${C_RESET}"
      say "  ${C_CYAN}1)${C_RESET} Python venv      ${C_DIM}portable; you control its process manager${C_RESET}"
      say "  ${C_CYAN}2)${C_RESET} Docker Compose   ${C_DIM}isolated container, restarts automatically${C_RESET}"
      say "  ${C_CYAN}3)${C_RESET} Ubuntu service   ${C_DIM}starts on boot; asks before sudo/system changes${C_RESET}"
      local choice=''
      read_prompt "Select [1-3]: " choice
      case "$choice" in
        1) INSTALL_MODE=python ;;
        2) INSTALL_MODE=docker ;;
        3) INSTALL_MODE=systemd ;;
        *) die "Choose 1, 2, or 3 and run the installer again." ;;
      esac
      ;;
    *) die "Unknown BOMBAGIF_INSTALL_MODE '$INSTALL_MODE'. Use python, docker, or systemd." ;;
  esac
}

find_python311() {
  local candidate
  for candidate in python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# Detects which mode an existing install uses, from the strongest evidence available.
detect_mode() {
  [[ -d "$INSTALL_DIR" ]] || return 1
  if [[ -f "$INSTALL_DIR/.install-mode" ]]; then
    local stored=''
    stored="$(head -n 1 "$INSTALL_DIR/.install-mode" 2>/dev/null | tr -d '[:space:]')"
    case "$stored" in
      python|docker|systemd) printf '%s' "$stored"; return 0 ;;
    esac
  fi
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    printf 'systemd'; return 0
  fi
  if [[ -d "$INSTALL_DIR/.venv" ]]; then printf 'python'; return 0; fi
  if [[ -f "$INSTALL_DIR/compose.yaml" ]] && command -v docker >/dev/null 2>&1 \
     && docker compose -f "$INSTALL_DIR/compose.yaml" ps -q >/dev/null 2>&1; then
    printf 'docker'; return 0
  fi
  if [[ -f "$INSTALL_DIR/compose.yaml" ]]; then printf 'docker'; return 0; fi
  return 1
}

write_mode_marker() {
  case "${INSTALL_MODE:-}" in
    python|docker|systemd) ;;
    *) return 0 ;;
  esac
  if [[ -d "$INSTALL_DIR" ]]; then
    umask 022
    printf '%s\n' "$INSTALL_MODE" > "$INSTALL_DIR/.install-mode" 2>/dev/null || true
  fi
}

# Prints one aligned "Label: value" status row.
status_row() {
  local label="$1" value="$2" color="${3:-}"
  if [[ -n "$color" ]]; then
    say "  ${C_DIM}${label}:${C_RESET} ${color}${value}${C_RESET}"
  else
    say "  ${C_DIM}${label}:${C_RESET} ${value}"
  fi
}

env_value() {
  # Reads a single KEY= line from .env without sourcing or printing secrets.
  local key="$1" line=''
  [[ -f "$INSTALL_DIR/.env" ]] || return 1
  line="$(grep -m1 "^${key}=" "$INSTALL_DIR/.env" 2>/dev/null)" || return 1
  line="${line#*=}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "$line"
}

service_state() {
  # Echoes "running", "stopped", or "missing" for the detected mode.
  local mode="$1"
  case "$mode" in
    python)
      if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then printf 'missing'; return; fi
      if pgrep -f "$INSTALL_DIR/.venv/bin/python.*main\.py" >/dev/null 2>&1; then printf 'running'; else printf 'stopped'; fi
      ;;
    docker)
      if ! command -v docker >/dev/null 2>&1; then printf 'missing'; return; fi
      if [[ -n "$(docker compose -f "$INSTALL_DIR/compose.yaml" ps -q "${SERVICE_NAME}" 2>/dev/null)" ]]; then printf 'running'; else printf 'stopped'; fi
      ;;
    systemd)
      if ! command -v systemctl >/dev/null 2>&1; then printf 'missing'; return; fi
      case "$(systemctl is-active "${SERVICE_NAME}.service" 2>/dev/null || true)" in
        active) printf 'running' ;;
        *) printf 'stopped' ;;
      esac
      ;;
    *) printf 'missing' ;;
  esac
}

show_status() {
  local mode state mode_color
  say "${C_BOLD}Current status${C_RESET}"
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
  if [[ ! -d "$INSTALL_DIR" ]]; then
    status_row "Install dir" "$INSTALL_DIR (not installed yet)" "$C_YELLOW"
    say ""
    tip "Choose 1 to install Bombagif here."
    return 1
  fi
  mode="$(detect_mode || true)"
  if [[ -z "$mode" ]]; then
    status_row "Install dir" "$INSTALL_DIR (files present, mode unknown)" "$C_YELLOW"
    return 1
  fi
  state="$(service_state "$mode")"
  case "$state" in
    running) mode_color="$C_GREEN" ;;
    stopped) mode_color="$C_YELLOW" ;;
    *) mode_color="$C_RED" ;;
  esac
  status_row "Install dir" "$INSTALL_DIR"
  status_row "Mode" "$(mode_label "$mode")"
  status_row "Service" "$state" "$mode_color"
  if [[ -f "$INSTALL_DIR/.env" ]]; then
    local env_mode=''
    env_mode="$(ls -l "$INSTALL_DIR/.env" 2>/dev/null | awk '{print $1}')"
    status_row ".env secrets" "present (${env_mode:-unknown permissions})"
  else
    status_row ".env secrets" "MISSING — run option 1 or 2 to configure" "$C_RED"
  fi
  local zipline_url=''
  zipline_url="$(env_value ZIPLINE_URL || true)"
  if [[ -n "$zipline_url" ]]; then
    status_row "Zipline URL" "$(printf '%s' "$zipline_url" | sed -e 's#\(https\?://[^/]*\).*#\1/…#')"
  fi
  if command -v git >/dev/null 2>&1 && [[ -d "$INSTALL_DIR/.git" ]]; then
    status_row "Source" "$(git -C "$INSTALL_DIR" log -1 --pretty='%h %s' 2>/dev/null | cut -c1-52)"
  fi
  case "$mode" in
    python) status_row "Run" "cd '$INSTALL_DIR' && .venv/bin/python main.py" ;;
    docker) status_row "Logs" "cd '$INSTALL_DIR' && docker compose logs -f ${SERVICE_NAME}" ;;
    systemd) status_row "Logs" "sudo journalctl -u ${SERVICE_NAME} -f" ;;
  esac
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
  [[ "$state" == running ]] && return 0 || return 1
}

preflight() {
  command -v git >/dev/null 2>&1 || die "Git is required. Install Git and run this installer again."
  [[ "$REPO_URL" == https://* ]] || die "BOMBAGIF_REPO_URL must be an HTTPS Git URL."
  [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "BOMBAGIF_BRANCH has unsupported characters."
  [[ "$INSTALL_DIR" == /* && "$INSTALL_DIR" != *[[:space:]]* ]] || die "BOMBAGIF_INSTALL_DIR must be an absolute path without spaces."
  [[ "$INSTALL_DIR" != "$HOME" ]] || die "Choose a dedicated install directory, not the home directory itself."
  [[ ! -L "$INSTALL_DIR" ]] || die "Install path is a symlink: $INSTALL_DIR. Choose a real directory; nothing was changed."

  if [[ "$INSTALL_ACTION" == install ]]; then
    # A fresh install may target a missing or empty directory; a directory with
    # unrelated files is refused so nothing of the user's is overwritten.
    [[ ! -e "$INSTALL_DIR" || -d "$INSTALL_DIR" ]] || die "Install path exists and is not a directory: $INSTALL_DIR"
    if [[ -d "$INSTALL_DIR" && ! -d "$INSTALL_DIR/.git" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
      die "$INSTALL_DIR already contains files and is not a Bombagif checkout. Choose a fresh BOMBAGIF_INSTALL_DIR, or move those files yourself."
    fi
  else
    [[ -d "$INSTALL_DIR" ]] || die "No Bombagif install found at $INSTALL_DIR. Run this installer and choose 1 (Install) first."
  fi

  # The runtime mode is the freshly chosen one for a new install, or the mode
  # detected from the existing install for update/manage/repair actions.
  local check_mode="$INSTALL_MODE"
  if [[ "$INSTALL_ACTION" == install ]]; then
    check_mode="${RUNTIME_MODE:-$INSTALL_MODE}"
  else
    check_mode="$(detect_mode || true)"
    [[ -n "$check_mode" ]] || die "Could not detect how Bombagif is installed at $INSTALL_DIR. Run option 1 or 2 to repair it."
  fi

  case "$check_mode" in
    python)
      find_python311 >/dev/null || die "Python 3.11 or newer is required. Install it first."
      if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists cairo 2>/dev/null; then
        warn "Cairo is not detected. PNG/WEBP work; SVG needs Cairo (macOS: brew install cairo; Ubuntu: sudo apt install libcairo2)."
      fi
      command -v ffmpeg >/dev/null 2>&1 || warn "FFmpeg is not detected. Image conversion works; MP4/WebM video conversion needs FFmpeg (macOS: brew install ffmpeg; Ubuntu: sudo apt install ffmpeg)."
      ;;
    docker)
      command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine/Desktop with Compose first."
      docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the docker compose command)."
      ;;
    systemd)
      [[ -r /etc/os-release ]] || die "The systemd option supports Ubuntu only. Choose Python or Docker elsewhere."
      # shellcheck disable=SC1091
      . /etc/os-release
      [[ "${ID:-}" == ubuntu ]] || die "Ubuntu systemd service is supported only on Ubuntu."
      [[ "$(id -un)" != root ]] || die "Run the installer as your normal Ubuntu user, not root."
      [[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "Systemd install path must be absolute and contain only letters, digits, / . _ or -."
      [[ "$INSTALL_DIR" == "$HOME"/* ]] || die "Install the service under your normal user's home directory (for example ~/Bombagif)."
      command -v sudo >/dev/null 2>&1 || die "sudo is required to install system packages and register the service."
      find_python311 >/dev/null || die "Ubuntu systemd mode requires Python 3.11+. Install it first; no system changes were made."
      ;;
  esac
}

clone_project() {
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Existing Bombagif checkout found — reinstalling into it and keeping your .env."
    return 0
  fi
  if [[ -d "$INSTALL_DIR" ]]; then
    info "Using the empty directory $INSTALL_DIR."
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
  fi
  info "Downloading Bombagif source (${BRANCH})..."
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || die "Download failed. Check your network connection and try again."
  ok "Source installed in $INSTALL_DIR"
}

update_project() {
  if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    warn "$INSTALL_DIR is not a Git checkout, so source cannot be pulled. Reinstalling dependencies instead."
    return 0
  fi
  info "Checking for updates from ${BRANCH}..."
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH" >/dev/null 2>&1 || die "Could not reach the repository. Check your network connection."
  local before after
  before="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
  after="$(git -C "$INSTALL_DIR" rev-parse --short FETCH_HEAD 2>/dev/null || printf 'unknown')"
  if [[ "$before" == "$after" ]]; then
    ok "Already up to date (${before})."
  else
    if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null)" ]]; then
      warn "Local file changes exist in $INSTALL_DIR. Updating replaces tracked files."
      confirm "Discard those local changes and update? [y/N]: " no || die "Cancelled; nothing was changed."
    fi
    info "Updating ${before} → ${after}..."
    git -C "$INSTALL_DIR" reset --hard FETCH_HEAD >/dev/null 2>&1 || die "Update failed; the previous revision was kept."
    ok "Source updated to ${after}."
  fi
}

collect_secrets() {
  local discord_token="${DISCORD_TOKEN:-}" zipline_token="${ZIPLINE_TOKEN:-}" zipline_url="${ZIPLINE_URL:-}" keep_existing=''

  if [[ -L "$INSTALL_DIR/.env" || ( -e "$INSTALL_DIR/.env" && ! -f "$INSTALL_DIR/.env" ) ]]; then
    die "Refusing to follow or replace a non-regular .env path. Inspect it manually."
  fi
  if [[ -f "$INSTALL_DIR/.env" ]]; then
    warn "Existing .env may contain secrets."
    read_prompt "Keep it unchanged? [Y/n]: " keep_existing
    if [[ -z "$keep_existing" || "$keep_existing" =~ ^[Yy] ]]; then
      chmod 600 "$INSTALL_DIR/.env"
      ok "Kept the existing .env and restricted permissions to your account."
      return
    fi
    warn "The existing .env will be replaced only after your explicit choice."
  fi

  if [[ -z "$discord_token" ]]; then
    say "${C_BOLD}Configure your own Discord app and Zipline.${C_RESET}"
    say "${C_YELLOW}Tokens are hidden while typing and are never echoed by this installer.${C_RESET}"
    read_prompt "Your Discord bot token: " discord_token true
  fi
  [[ -n "$discord_token" ]] || die "DISCORD_TOKEN cannot be empty."
  if [[ -z "$zipline_token" ]]; then read_prompt "Your Zipline API token: " zipline_token true; fi
  [[ -n "$zipline_token" ]] || die "ZIPLINE_TOKEN cannot be empty."
  if [[ -z "$zipline_url" ]]; then read_prompt "Your Zipline base URL (HTTPS): " zipline_url; fi
  [[ "$zipline_url" == https://* && "$zipline_url" != *\?* && "$zipline_url" != *\#* && "$zipline_url" != *'@'* ]] || die "ZIPLINE_URL must be an HTTPS base URL without user info, query, or fragment."
  case "$discord_token$zipline_token$zipline_url" in
    *$'\n'*|*$'\r'*) die "Credential values must each be a single line." ;;
  esac

  local env_tmp
  umask 077
  env_tmp="$(mktemp "$INSTALL_DIR/.env.tmp.XXXXXX")"
  if ! {
    printf 'DISCORD_TOKEN=%s\n' "$(dotenv_quote "$discord_token")"
    printf 'ZIPLINE_TOKEN=%s\n' "$(dotenv_quote "$zipline_token")"
    printf 'ZIPLINE_URL=%s\n' "$(dotenv_quote "$zipline_url")"
    printf 'ALLOWED_GUILDS=\nLOG_LEVEL=INFO\nGIF_BACKGROUND=#FFFFFF\n'
  } > "$env_tmp"; then
    rm -f "$env_tmp"
    die "Could not safely write credentials; secret values were not printed."
  fi

  if [[ -e "$INSTALL_DIR/.env" ]]; then
    chmod 600 "$INSTALL_DIR/.env"
    if ! mv -f "$env_tmp" "$INSTALL_DIR/.env"; then
      rm -f "$env_tmp"
      die "Could not replace .env. Existing file was preserved."
    fi
  else
    if ! ln "$env_tmp" "$INSTALL_DIR/.env" 2>/dev/null; then
      rm -f "$env_tmp"
      die "Could not create .env without overwriting an existing path."
    fi
    rm -f "$env_tmp"
  fi
  ok "Private .env created (mode 600); credentials were not displayed."
}

install_python_mode() {
  local python_bin
  python_bin="$(find_python311)" || die "Python 3.11+ is required."
  info "Creating a local virtual environment (this can take a minute)..."
  "$python_bin" -m venv "$INSTALL_DIR/.venv" || die "Could not create the virtual environment."
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip >/dev/null || die "Could not upgrade pip."
  "$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt" || die "Could not install dependencies."
  write_mode_marker
  ok "Python environment ready."
}

start_python_mode() {
  local log_file="$INSTALL_DIR/bombagif.log"
  if [[ "$(service_state python)" == running ]]; then
    ok "Bombagif is already running."
    return 0
  fi
  info "Starting Bombagif in the background (log: $log_file)..."
  (cd "$INSTALL_DIR" && nohup .venv/bin/python main.py >>"$log_file" 2>&1 &) || die "Could not start Bombagif."
  sleep 3
  if [[ "$(service_state python)" == running ]]; then
    ok "Bombagif started."
  else
    warn "Bombagif did not stay running. Last log lines:"
    tail -n 10 "$log_file" 2>/dev/null || true
    return 1
  fi
}

install_docker_mode() {
  info "Building and starting Bombagif with Compose (first build can take a few minutes)..."
  (cd "$INSTALL_DIR" && docker compose up -d --build) || die "Compose failed. Review the output above; .env was not removed."
  write_mode_marker
  ok "Container is up."
}

install_systemd_mode() {
  warn "Ubuntu setup will use sudo to install packages and register ${SERVICE_NAME}.service."
  confirm "Continue with those system changes? [y/N]: " no || die "Cancelled before system changes."
  local python_bin
  python_bin="$(find_python311)" || die "Python 3.11+ required; no system changes were made."
  sudo -v
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ffmpeg libcairo2
  local user_name group_name unit_tmp
  user_name="$(id -un)"
  group_name="$(id -gn)"
  info "Installing the isolated service environment..."
  "$python_bin" -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip >/dev/null
  "$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
  unit_tmp="$(mktemp)"
  trap 'rm -f "$unit_tmp"' RETURN
  cat >"$unit_tmp" <<UNIT
[Unit]
Description=Bombagif Discord GIF bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$user_name
Group=$group_name
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/main.py
Restart=on-failure
RestartSec=5
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
[Install]
WantedBy=multi-user.target
UNIT
  sudo install -o root -g root -m 0644 "$unit_tmp" "/etc/systemd/system/${SERVICE_NAME}.service"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${SERVICE_NAME}.service"
  rm -f "$unit_tmp"
  trap - RETURN
  write_mode_marker
  ok "Service installed and enabled on boot."
}

manage_service() {
  local mode action
  mode="$(detect_mode || true)"
  [[ -n "$mode" ]] || die "Could not detect how Bombagif is installed at $INSTALL_DIR."
  say "${C_BOLD}Manage $(mode_label "$mode")${C_RESET}"
  say "  ${C_CYAN}1)${C_RESET} Start"
  say "  ${C_CYAN}2)${C_RESET} Stop"
  say "  ${C_CYAN}3)${C_RESET} Restart"
  say "  ${C_CYAN}4)${C_RESET} Show recent logs"
  say "  ${C_CYAN}5)${C_RESET} Re-apply settings (.env / status.config)"
  say "  ${C_CYAN}6)${C_RESET} Back"
  read_prompt "Select [1-6]: " action
  case "$action" in
    1)
      case "$mode" in
        python) start_python_mode ;;
        docker) (cd "$INSTALL_DIR" && docker compose up -d) && ok "Container started." ;;
        systemd) sudo systemctl start "${SERVICE_NAME}.service" && ok "Service started." ;;
      esac
      ;;
    2)
      case "$mode" in
        python) pkill -f "$INSTALL_DIR/.venv/bin/python.*main\.py" >/dev/null 2>&1 && ok "Stopped." || warn "Was not running." ;;
        docker) (cd "$INSTALL_DIR" && docker compose stop) && ok "Container stopped." ;;
        systemd) sudo systemctl stop "${SERVICE_NAME}.service" && ok "Service stopped." ;;
      esac
      ;;
    3)
      case "$mode" in
        python) pkill -f "$INSTALL_DIR/.venv/bin/python.*main\.py" >/dev/null 2>&1 || true; start_python_mode ;;
        docker) (cd "$INSTALL_DIR" && docker compose restart) && ok "Container restarted." ;;
        systemd) sudo systemctl restart "${SERVICE_NAME}.service" && ok "Service restarted." ;;
      esac
      ;;
    4)
      case "$mode" in
        python) tail -n 40 "$INSTALL_DIR/bombagif.log" 2>/dev/null || warn "No log yet at $INSTALL_DIR/bombagif.log" ;;
        docker) (cd "$INSTALL_DIR" && docker compose logs --tail 40) ;;
        systemd) sudo journalctl -u "${SERVICE_NAME}.service" -n 40 --no-pager ;;
      esac
      ;;
    5)
      collect_secrets
      case "$mode" in
        python) pkill -f "$INSTALL_DIR/.venv/bin/python.*main\.py" >/dev/null 2>&1 || true; start_python_mode ;;
        docker) (cd "$INSTALL_DIR" && docker compose up -d) && ok "Settings applied." ;;
        systemd) sudo systemctl restart "${SERVICE_NAME}.service" && ok "Settings applied." ;;
      esac
      ;;
    6) return 0 ;;
    *) warn "No change made." ;;
  esac
  say ""
  show_status || true
}

doctor() {
  local problems=0 mode
  say "${C_BOLD}Checking this installation${C_RESET}"
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
  if [[ ! -d "$INSTALL_DIR" ]]; then
    fail "No install at $INSTALL_DIR — choose option 1 to install."
    return 1
  fi
  mode="$(detect_mode || true)"
  [[ -n "$mode" ]] || { fail "Install mode unknown."; problems=$((problems + 1)); }
  if [[ -f "$INSTALL_DIR/.env" ]]; then
    ok ".env found (permissions $(ls -l "$INSTALL_DIR/.env" 2>/dev/null | awk '{print $1}'))"
    local missing=''
    for key in DISCORD_TOKEN ZIPLINE_TOKEN ZIPLINE_URL; do
      if ! grep -q "^${key}=.\+" "$INSTALL_DIR/.env" 2>/dev/null; then missing="$missing $key"; fi
    done
    if [[ -n "$missing" ]]; then fail "Missing values in .env:$missing"; problems=$((problems + 1)); else ok "Discord + Zipline credentials are set."; fi
    local zipline_url=''
    zipline_url="$(env_value ZIPLINE_URL || true)"
    if [[ -n "$zipline_url" && "$zipline_url" != https://* ]]; then
      fail "ZIPLINE_URL must start with https:// (got: $zipline_url)"
      problems=$((problems + 1))
    elif [[ -n "$zipline_url" ]] && command -v curl >/dev/null 2>&1; then
      local code=''
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$zipline_url" 2>/dev/null || true)"
      if [[ "$code" == "200" || "$code" == "301" || "$code" == "302" || "$code" == "307" || "$code" == "308" ]]; then
        ok "Zipline responds at $(printf '%s' "$zipline_url" | sed -e 's#\(https\?://[^/]*\).*#\1/…#') (HTTP $code)"
      else
        warn "Could not confirm Zipline at $(printf '%s' "$zipline_url" | sed -e 's#\(https\?://[^/]*\).*#\1/…#') (HTTP ${code:-no response}). Check the URL and your network."
      fi
    fi
  else
    fail ".env is missing — run option 1 or 2 to enter your credentials."
    problems=$((problems + 1))
  fi
  if [[ -f "$INSTALL_DIR/status.config" ]]; then
    ok "status.config found (customizable Discord status card)."
  else
    warn "status.config missing — the bot will still show an online status."
  fi
  case "$mode" in
    python)
      if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then ok "Virtual environment ready."; else fail "Virtual environment missing — re-run option 1."; problems=$((problems + 1)); fi
      if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists cairo 2>/dev/null; then ok "Cairo present (SVG input supported)."; else warn "Cairo not detected — PNG/WEBP work, SVG will be rejected."; fi
      if command -v ffmpeg >/dev/null 2>&1; then ok "FFmpeg present (MP4/WebM input supported)."; else warn "FFmpeg not detected — MP4/WebM video conversion is unavailable."; fi
      ;;
    docker)
      if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then ok "Docker + Compose v2 available."; else fail "Docker Compose v2 missing."; problems=$((problems + 1)); fi
      ;;
    systemd)
      if command -v systemctl >/dev/null 2>&1; then ok "systemd available."; else warn "systemd not available on this machine."; fi
      ;;
  esac
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
  if [[ "$problems" -eq 0 ]]; then ok "No problems found."; else warn "$problems problem(s) found above."; fi
  show_status || true
  return 0
}

uninstall_bombagif() {
  local mode answer
  mode="$(detect_mode || true)"
  [[ -n "$mode" ]] || die "Could not detect how Bombagif is installed at $INSTALL_DIR."
  warn "This removes the Bombagif service and its files under $INSTALL_DIR."
  case "$mode" in
    python) confirm "Stop the running Python process? [Y/n]: " yes && pkill -f "$INSTALL_DIR/.venv/bin/python.*main\.py" >/dev/null 2>&1 || true ;;
    docker)
      if confirm "Stop and remove the Docker container and image? [Y/n]: " yes; then
        (cd "$INSTALL_DIR" && docker compose down --rmi local) || warn "Compose reported an issue while removing the container."
      fi
      ;;
    systemd)
      if confirm "Stop, disable, and delete ${SERVICE_NAME}.service (needs sudo)? [Y/n]: " yes; then
        sudo systemctl disable --now "${SERVICE_NAME}.service" || true
        sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
        sudo systemctl daemon-reload
        ok "Service removed."
      fi
      ;;
  esac
  say ""
  say "Your credentials live in ${C_BOLD}$INSTALL_DIR/.env${C_RESET}."
  if confirm "Also delete $INSTALL_DIR and everything in it? [y/N]: " no; then
    case "$INSTALL_DIR" in
      /|""|"$HOME") die "Refusing to delete $INSTALL_DIR for safety." ;;
    esac
    rm -rf "$INSTALL_DIR" || die "Could not delete $INSTALL_DIR."
    ok "Removed $INSTALL_DIR."
  else
    ok "Kept $INSTALL_DIR. Delete it yourself whenever you are ready: rm -rf '$INSTALL_DIR'"
  fi
}

post_install_result() {
  local mode="$1" running="${2:-unknown}"
  say ""
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
  if [[ "$running" == no ]]; then
    warn "Setup finished, but Bombagif is not running right now. Check the log, then start it:"
    say "     tail -n 20 '$INSTALL_DIR/bombagif.log'"
  else
    ok "Done. Bombagif is configured on this machine."
  fi
  say ""
  say "${C_BOLD}Useful commands${C_RESET}"
  case "$mode" in
    python)
      say "  ${C_DIM}Start${C_RESET}   cd '$INSTALL_DIR' && .venv/bin/python main.py"
      say "  ${C_DIM}Logs${C_RESET}    tail -f '$INSTALL_DIR/bombagif.log'"
      say "  ${C_DIM}Stop${C_RESET}    pkill -f '$INSTALL_DIR/.venv/bin/python'"
      ;;
    docker)
      say "  ${C_DIM}Logs${C_RESET}    cd '$INSTALL_DIR' && docker compose logs -f ${SERVICE_NAME}"
      say "  ${C_DIM}Restart${C_RESET} cd '$INSTALL_DIR' && docker compose restart ${SERVICE_NAME}"
      say "  ${C_DIM}Stop${C_RESET}    cd '$INSTALL_DIR' && docker compose down"
      ;;
    systemd)
      say "  ${C_DIM}Status${C_RESET}  sudo systemctl status ${SERVICE_NAME} --no-pager"
      say "  ${C_DIM}Logs${C_RESET}    sudo journalctl -u ${SERVICE_NAME} -f"
      say "  ${C_DIM}Restart${C_RESET} sudo systemctl restart ${SERVICE_NAME}"
      ;;
  esac
  say ""
  say "${C_BOLD}Good to know${C_RESET}"
  tip "Discord setup: enable User Install in the Developer Portal, then invite with"
  say "     https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=applications.commands&integration_type=1"
  tip "Presence: edit '$INSTALL_DIR/status.config' — the running bot applies it in ~15s."
  tip "Re-run this installer any time: update, check status, see logs, or uninstall."
  tip "Your tokens stay in '$INSTALL_DIR/.env' (mode 600). Never share or commit that file."
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
  say ""
  show_status || true
}

main() {
  banner
  INSTALL_ACTION="$(printf '%s' "$INSTALL_ACTION" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "$INSTALL_ACTION" ]]; then
    say "${C_BOLD}What would you like to do?${C_RESET}"
    say "  ${C_CYAN}1)${C_RESET} Install Bombagif       ${C_DIM}first-time setup (or repair an existing folder)${C_RESET}"
    say "  ${C_CYAN}2)${C_RESET} Update to the latest   ${C_DIM}pull new source and rebuild${C_RESET}"
    say "  ${C_CYAN}3)${C_RESET} Manage the service     ${C_DIM}start, stop, restart, logs, re-apply settings${C_RESET}"
    say "  ${C_CYAN}4)${C_RESET} Show status            ${C_DIM}where it is, whether it runs, what to type next${C_RESET}"
    say "  ${C_CYAN}5)${C_RESET} Uninstall              ${C_DIM}remove the service and optionally the files${C_RESET}"
    say "  ${C_CYAN}6)${C_RESET} Check installation     ${C_DIM}diagnose .env, Zipline, Cairo, FFmpeg, Docker${C_RESET}"
    local choice=''
    read_prompt "Select [1-6]: " choice
    case "$choice" in
      1) INSTALL_ACTION=install ;;
      2) INSTALL_ACTION=update ;;
      3) INSTALL_ACTION=manage ;;
      4) INSTALL_ACTION=status ;;
      5) INSTALL_ACTION=uninstall ;;
      6) INSTALL_ACTION=doctor ;;
      *) die "Choose 1-6 and run the installer again." ;;
    esac
  fi

  case "$INSTALL_ACTION" in
    status) show_status && exit 0 || exit 1 ;;
    doctor) doctor && exit 0 || exit 1 ;;
    uninstall) preflight; uninstall_bombagif; exit 0 ;;
  esac

  if [[ "$INSTALL_ACTION" == update ]]; then
    preflight
    update_project
    INSTALL_MODE="$(detect_mode || true)"
    [[ -n "$INSTALL_MODE" ]] || die "Could not detect the install mode at $INSTALL_DIR."
    case "$INSTALL_MODE" in
      python)
        install_python_mode
        pkill -f "$INSTALL_DIR/.venv/bin/python.*main\.py" >/dev/null 2>&1 || true
        start_python_mode || die "Updated source was installed but Bombagif did not restart; inspect $INSTALL_DIR/bombagif.log."
        ;;
      docker) install_docker_mode ;;
      systemd)
        install_systemd_mode
        sudo systemctl restart "${SERVICE_NAME}.service" || die "Updated source was installed but Bombagif did not restart; inspect sudo journalctl -u ${SERVICE_NAME}.service."
        ;;
    esac
    ok "Update complete; the service is running the new source."
    show_status || true
    exit 0
  fi

  if [[ "$INSTALL_ACTION" == manage ]]; then
    preflight
    manage_service
    exit 0
  fi

  # Fresh install.
  INSTALL_MODE="${INSTALL_MODE:-}"
  [[ -n "$INSTALL_MODE" ]] || choose_mode
  RUNTIME_MODE="$INSTALL_MODE"
  preflight
  clone_project
  collect_secrets
  local started='yes'
  case "$INSTALL_MODE" in
    python) install_python_mode; start_python_mode || started='no' ;;
    docker) install_docker_mode; [[ "$(service_state docker)" == running ]] || started='no' ;;
    systemd) install_systemd_mode; [[ "$(service_state systemd)" == running ]] || started='no' ;;
  esac
  post_install_result "$INSTALL_MODE" "$started"
}

# `curl ... | bash` reads from stdin, where BASH_SOURCE[0] is empty.
if [[ "${BASH_SOURCE[0]:-}" == "$0" || -z "${BASH_SOURCE[0]:-}" ]]; then
  main "$@"
fi
