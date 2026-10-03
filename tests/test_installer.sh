#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/install.sh"

assert_equal() {
  local expected="$1" actual="$2" label="$3"
  if [[ "$expected" != "$actual" ]]; then
    printf 'FAIL: %s\n  expected: %s\n  actual:   %s\n' "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

# Bash's expansion order for the replacement must preserve dotenv apostrophes
# and backslashes. The result remains compatible with python-dotenv parsing.
quoted="$(dotenv_quote "token'with\\slash")"
parsed="$(python3 -c 'from dotenv import dotenv_values; import sys; print(dotenv_values(stream=sys.stdin).get("VALUE", ""))' <<< "VALUE=$quoted")"
assert_equal "token'with\\slash" "$parsed" "dotenv round-trip"

# Write only harmless test credentials, then verify python-dotenv reads the exact values.
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/bombagif-installer-test.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT
INSTALL_DIR="$TEST_ROOT/credentials"
mkdir -p "$INSTALL_DIR"
DISCORD_TOKEN="test-discord'quoted\\token"
ZIPLINE_TOKEN='test-zipline\\token'
ZIPLINE_URL=https://zipline.example.test
export DISCORD_TOKEN ZIPLINE_TOKEN ZIPLINE_URL
collect_secrets
python3 - "$INSTALL_DIR/.env" <<'PY'
import os
import stat
import sys
from dotenv import dotenv_values

path = sys.argv[1]
parsed = dotenv_values(path)
assert parsed["DISCORD_TOKEN"] == os.environ["DISCORD_TOKEN"]
assert parsed["ZIPLINE_TOKEN"] == os.environ["ZIPLINE_TOKEN"]
assert parsed["ZIPLINE_URL"] == os.environ["ZIPLINE_URL"]
assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
PY
unset DISCORD_TOKEN ZIPLINE_TOKEN ZIPLINE_URL

# Setup choices are accepted case-insensitively and normalize correctly.
INSTALL_MODE=PYTHON
choose_mode
assert_equal python "$INSTALL_MODE" "python mode normalization"
INSTALL_MODE=DOCKER
choose_mode
assert_equal docker "$INSTALL_MODE" "docker mode normalization"

# Existing directories and dangling symlinks must fail before cloning.
REPO_URL=https://github.com/koryei/Bombagifs.git
BRANCH=main
INSTALL_DIR="$TEST_ROOT/existing"
mkdir -p "$INSTALL_DIR"
INSTALL_MODE=python
if (preflight >/dev/null 2>&1); then
  printf 'FAIL: existing install path was not rejected\n' >&2
  exit 1
fi
INSTALL_DIR="$TEST_ROOT/dangling-link"
ln -s "$TEST_ROOT/nonexistent" "$INSTALL_DIR"
if (preflight >/dev/null 2>&1); then
  printf 'FAIL: dangling symlink install path was not rejected\n' >&2
  exit 1
fi

# Keep the startup header clean and text-based rather than restoring a large ASCII logo.
banner_output="$(banner)"
assert_equal BOMBAGIF "$(printf '%s\n' "$banner_output" | sed -n '2p')" "color-first banner title"
if [[ "$banner_output" == *" ____ "* || "$banner_output" == *"|____/"* || "$banner_output" == *"────────────────"* || "$banner_output" == *$'\033'* ]]; then
  printf 'FAIL: banner contains ASCII art, separator art, or escape codes in non-interactive output\n' >&2
  exit 1
fi

# FFmpeg is installed in the Ubuntu service and Docker runtime, but is only an
# optional host prerequisite for Python installs.
grep -Eq 'apt-get install -y python3 python3-venv ffmpeg libcairo2' "$ROOT/install.sh"
grep -Eq 'apt-get install -y --no-install-recommends ffmpeg libcairo2 libffi8' "$ROOT/Dockerfile"
grep -Eq 'FFmpeg is not detected' "$ROOT/install.sh"

# Updating source must restart Python/systemd deployments so they execute the
# fetched code. Docker's `up --build` already recreates the running container.
UPDATE_TEST_LOG="$TEST_ROOT/update-actions.log"
UPDATE_INSTALLER="$ROOT/install.sh"
export UPDATE_TEST_LOG UPDATE_INSTALLER
UPDATE_LOG=""
info() { UPDATE_LOG+="info:$*;"; }
ok() { UPDATE_LOG+="ok:$*;"; }
show_status() { UPDATE_LOG+="show-status;"; return 0; }
preflight() { UPDATE_LOG+="preflight;"; }
die() { printf 'unexpected installer error: %s\n' "$*" >&2; exit 1; }
update_project() { UPDATE_LOG+="fetch-reset;"; }
detect_mode() { printf '%s' "$INSTALL_MODE"; }
install_python_mode() { UPDATE_LOG+="install-python;"; }
start_python_mode() { UPDATE_LOG+="start-python;"; }
install_docker_mode() { UPDATE_LOG+="install-docker;"; }
install_systemd_mode() { UPDATE_LOG+="install-systemd;"; }
pkill() { UPDATE_LOG+="pkill-python;"; }
sudo() { UPDATE_LOG+="sudo $*;"; }
for update_mode in python docker systemd; do
  (
    INSTALL_ACTION=update
    INSTALL_MODE="$update_mode"
    INSTALL_DIR="$TEST_ROOT/update-$update_mode"
    preflight() { :; }
    update_project() { printf 'fetch-reset\n' >> "$UPDATE_TEST_LOG"; }
    detect_mode() { printf '%s' "$INSTALL_MODE"; }
    install_python_mode() { printf 'install-python\n' >> "$UPDATE_TEST_LOG"; }
    start_python_mode() { printf 'start-python\n' >> "$UPDATE_TEST_LOG"; }
    install_docker_mode() { printf 'install-docker\n' >> "$UPDATE_TEST_LOG"; }
    install_systemd_mode() { printf 'install-systemd\n' >> "$UPDATE_TEST_LOG"; }
    pkill() { printf 'pkill-python\n' >> "$UPDATE_TEST_LOG"; }
    sudo() { printf 'sudo %s\n' "$*" >> "$UPDATE_TEST_LOG"; }
    show_status() { return 0; }
    main >/dev/null
  )
done
expected_updates=$'fetch-reset\ninstall-python\npkill-python\nstart-python\nfetch-reset\ninstall-docker\nfetch-reset\ninstall-systemd\nsudo systemctl restart bombagif.service'
actual_updates="$(cat "$UPDATE_TEST_LOG")"
assert_equal "$expected_updates" "$actual_updates" "update restarts Python and systemd; rebuilds Docker"

printf 'Installer unit checks passed.\n'
