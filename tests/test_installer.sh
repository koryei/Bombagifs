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

# FFmpeg is installed in the Ubuntu service and Docker runtime, but is only an
# optional host prerequisite for Python installs.
grep -Eq 'apt-get install -y python3 python3-venv ffmpeg libcairo2' "$ROOT/install.sh"
grep -Eq 'apt-get install -y --no-install-recommends ffmpeg libcairo2 libffi8' "$ROOT/Dockerfile"
grep -Eq 'FFmpeg is not detected' "$ROOT/install.sh"

printf 'Installer unit checks passed.\n'
