#!/usr/bin/env bash
# Interactive public self-host installer for Bombagif (Bash 3.2+ compatible).
set -Eeuo pipefail

REPO_URL="${BOMBAGIF_REPO_URL:-https://github.com/koryei/Bombagifs.git}"
BRANCH="${BOMBAGIF_BRANCH:-main}"
INSTALL_DIR="${BOMBAGIF_INSTALL_DIR:-${HOME}/Bombagif}"
INSTALL_MODE="${BOMBAGIF_INSTALL_MODE:-}"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_CYAN=$'\033[36m'
  C_PURPLE=$'\033[35m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
else
  C_RESET=''; C_BOLD=''; C_CYAN=''; C_PURPLE=''; C_GREEN=''; C_YELLOW=''; C_RED=''
fi

say() { printf '%b\n' "$*"; }
info() { say "${C_CYAN}[*]${C_RESET} $*"; }
ok() { say "${C_GREEN}[+]${C_RESET} $*"; }
warn() { say "${C_YELLOW}[!]${C_RESET} $*"; }
die() { say "${C_RED}[x]${C_RESET} $*" >&2; exit 1; }

banner() {
  say "${C_PURPLE}${C_BOLD}"
  say ' ____                     _             _  __'
  say '| __ )  ___  _ __ ___  _ __| | __ _  __| |/ _|'
  say '|  _ \ / _ \| '\''_ ` _ \ / _` |/ _` | | |_ '
  say '| |_) | (_) | | | | | | (_| | (_| | |  _|'
  say '|____/ \\___/|_| |_| |_|\\__,_|\\__,_| |_|'
  say "${C_RESET}${C_BOLD}GIFs on the fly. Your Discord app. Your Zipline. Your secrets stay yours.${C_RESET}"
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
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

choose_mode() {
  INSTALL_MODE="$(printf '%s' "$INSTALL_MODE" | tr '[:upper:]' '[:lower:]')"
  case "$INSTALL_MODE" in
    1|python|venv) INSTALL_MODE=python ;;
    2|docker|compose) INSTALL_MODE=docker ;;
    3|systemd|service) INSTALL_MODE=systemd ;;
    '')
      say "${C_BOLD}Choose how to run Bombagif:${C_RESET}"
      say "  ${C_CYAN}1)${C_RESET} Python venv     — portable; you control its process manager"
      say "  ${C_CYAN}2)${C_RESET} Docker Compose — isolated container with restart policy"
      say "  ${C_CYAN}3)${C_RESET} Ubuntu service — starts on boot; asks before sudo/system changes"
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

preflight() {
  command -v git >/dev/null 2>&1 || die "Git is required. Install Git and run this installer again."
  [[ "$REPO_URL" == https://* ]] || die "BOMBAGIF_REPO_URL must be an HTTPS Git URL."
  [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "BOMBAGIF_BRANCH has unsupported characters."
  [[ "$INSTALL_DIR" == /* && "$INSTALL_DIR" != *[[:space:]]* ]] || die "BOMBAGIF_INSTALL_DIR must be an absolute path without spaces."
  [[ "$INSTALL_DIR" != "$HOME" ]] || die "Choose a dedicated install directory, not the home directory itself."
  [[ ! -e "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]] || die "Install path already exists: $INSTALL_DIR. Choose a fresh path; existing files were not changed."

  case "$INSTALL_MODE" in
    python)
      find_python311 >/dev/null || die "Python 3.11 or newer is required. Install it first."
      if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists cairo 2>/dev/null; then
        warn "Cairo is not detected. PNG/WEBP work; SVG needs Cairo (macOS: brew install cairo; Ubuntu: sudo apt install libcairo2)."
      fi
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
  mkdir -p "$(dirname "$INSTALL_DIR")"
  info "Cloning ${REPO_URL} (${BRANCH})..."
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || die "Clone failed. Check repository URL, branch, Git, and network."
  ok "Source installed in $INSTALL_DIR"
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
  info "Creating a local virtual environment..."
  "$python_bin" -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
  "$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
  ok "Python dependencies installed. Start with:"
  say "  cd '$INSTALL_DIR' && .venv/bin/python main.py"
}

install_docker_mode() {
  info "Building and starting Bombagif with Compose..."
  (cd "$INSTALL_DIR" && docker compose up -d --build) || die "Compose failed. Review output above; .env was not removed."
  ok "Bombagif is running. Useful commands:"
  say "  cd '$INSTALL_DIR' && docker compose logs -f bombagif"
  say "  cd '$INSTALL_DIR' && docker compose restart bombagif"
  say "  cd '$INSTALL_DIR' && docker compose down"
}

install_systemd_mode() {
  warn "Ubuntu setup will use sudo to install packages and register bombagif.service."
  local confirm=''
  read_prompt "Continue with those system changes? [y/N]: " confirm
  [[ "$confirm" =~ ^[Yy] ]] || die "Cancelled before system changes."
  local python_bin
  python_bin="$(find_python311)" || die "Python 3.11+ required; no system changes were made."
  sudo -v
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv libcairo2
  local user_name group_name unit_tmp
  user_name="$(id -un)"
  group_name="$(id -gn)"
  info "Installing the isolated service environment..."
  "$python_bin" -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
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
  sudo install -o root -g root -m 0644 "$unit_tmp" /etc/systemd/system/bombagif.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now bombagif.service
  rm -f "$unit_tmp"
  trap - RETURN
  ok "Service installed and enabled on boot."
  say "  sudo systemctl status bombagif --no-pager"
  say "  sudo journalctl -u bombagif -f"
}

main() {
  banner
  say "This installer clones source and configures only YOUR Discord and Zipline credentials."
  say "Review the installer first: https://github.com/koryei/Bombagifs/blob/main/install.sh"
  choose_mode
  preflight
  clone_project
  collect_secrets
  case "$INSTALL_MODE" in
    python) install_python_mode ;;
    docker) install_docker_mode ;;
    systemd) install_systemd_mode ;;
  esac
  say "${C_CYAN}────────────────────────────────────────────────────────────────${C_RESET}"
  ok "Setup complete. Your Discord token and Zipline token were not printed."
  say "Keep $INSTALL_DIR/.env private; do not commit or share it."
}

# `curl ... | bash` reads from stdin, where BASH_SOURCE[0] is empty.
if [[ "${BASH_SOURCE[0]:-}" == "$0" || -z "${BASH_SOURCE[0]:-}" ]]; then
  main "$@"
fi
