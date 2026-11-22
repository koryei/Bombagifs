#!/usr/bin/env bash
# Tests for selfbot-ctl.sh: .env writing, unit rendering, and the update/rollback flow against a throwaway git repo.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/bombagif-ctl-test.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
assert_equal() { [[ "$1" == "$2" ]] || fail "$3 (expected '$1', got '$2')"; }
assert_contains() { [[ "$1" == *"$2"* ]] || fail "$3 (missing '$2' in: $1)"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] || fail "$3 (unexpected '$2')"; }

# A fake systemctl: records calls; `is-active` and NRestarts come from files the test controls.
FAKE_BIN="$TEST_ROOT/bin"; mkdir -p "$FAKE_BIN"
export FAKE_STATE="$TEST_ROOT/state"; mkdir -p "$FAKE_STATE"
cat >"$FAKE_BIN/systemctl" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >>"$FAKE_STATE/calls"
case "$1" in
  is-active) cat "$FAKE_STATE/active" 2>/dev/null || echo active ;;
  is-enabled) echo enabled ;;
  show) cat "$FAKE_STATE/restarts" 2>/dev/null || echo 0 ;;
  restart) [[ -f "$FAKE_STATE/crash-on-restart" ]] && echo inactive >"$FAKE_STATE/active" || echo active >"$FAKE_STATE/active" ;;
esac
FAKE
chmod +x "$FAKE_BIN/systemctl"
export SELFBOT_CTL_SYSTEMCTL="$FAKE_BIN/systemctl"
export SELFBOT_HEALTH_SECONDS=2
export SELFBOT_CTL_PIP="$FAKE_BIN/pip"
cat >"$FAKE_BIN/pip" <<'FAKE'
#!/usr/bin/env bash
echo "pip $*" >>"$FAKE_STATE/calls"
FAKE
chmod +x "$FAKE_BIN/pip"

# source the script's functions (main is guarded)
mkdir -p "$TEST_ROOT/unit"
export SELFBOT_UNIT_DIR="$TEST_ROOT/unit"
source "$ROOT/selfbot-ctl.sh"

# --- .env: you bring your own; the script only checks it --------------------------------------------------------
DIR="$TEST_ROOT/envdir"; mkdir -p "$DIR"
cp "$ROOT/.env.example" "$DIR/.env.example"

# no .env: a blank mode-600 template is made, everything required is reported missing
ensure_env
[[ -f "$DIR/.env" ]] || fail "ensure_env should create a template"
assert_equal 600 "$(stat -c %a "$DIR/.env" 2>/dev/null || stat -f %Lp "$DIR/.env")" "template is mode 600"
missing="$(env_missing || true)"
assert_contains "$missing" "SELFBOT_TOKEN" "blank template misses the token"
assert_contains "$missing" "ZIPLINE_URL" "the placeholder Zipline URL does not count"
assert_contains "$missing" "ZIPLINE_TOKEN" "blank template misses the zipline token"

# a pasted-in .env with odd characters, wide permissions and CRLF line endings is accepted and locked down
printf 'SELFBOT_TOKEN="tok.en-with_dots"\r\nZIPLINE_URL=https://zip.example.test\r\nZIPLINE_TOKEN=abc#def\r\n' >"$DIR/.env"
chmod 644 "$DIR/.env"
ensure_env
assert_equal 600 "$(stat -c %a "$DIR/.env" 2>/dev/null || stat -f %Lp "$DIR/.env")" "an imported .env is locked to mode 600"
if ! missing="$(env_missing)"; then fail "a complete .env should pass (missing: $missing)"; fi
assert_equal "" "$missing" "nothing missing"
assert_equal "tok.en-with_dots" "$(python3 -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1])["SELFBOT_TOKEN"])' "$DIR/.env")" "value read as dotenv reads it"
# ensure_env never rewrites an existing file
before="$(cat "$DIR/.env")"; ensure_env; assert_equal "$before" "$(cat "$DIR/.env")" "existing .env untouched"

# one key short: only that one is named
printf 'SELFBOT_TOKEN=a\nZIPLINE_URL=https://zip.example.test\nZIPLINE_TOKEN=\n' >"$DIR/.env"
missing="$(env_missing || true)"; assert_equal "ZIPLINE_TOKEN" "$missing" "only the unset key is named"

# start/restart refuse while required keys are missing, and say how to fix it
: >"$FAKE_STATE/calls"
if out="$(main restart 2>&1)"; then fail "restart must refuse without the required keys"; fi
assert_contains "$out" "ZIPLINE_TOKEN" "restart names the missing key"
assert_contains "$out" "scp .env" "restart explains how to import the .env"
assert_not_contains "$(cat "$FAKE_STATE/calls")" "restart" "systemctl restart was not called"
printf 'SELFBOT_TOKEN=a\nZIPLINE_URL=https://zip.example.test\nZIPLINE_TOKEN=b\n' >"$DIR/.env"
main restart >/dev/null 2>&1 || fail "restart should work with a complete .env"
assert_contains "$(cat "$FAKE_STATE/calls")" "restart bombagif-selfbot.service" "systemctl restart was called"
: >"$FAKE_STATE/calls"; main stop >/dev/null 2>&1
assert_contains "$(cat "$FAKE_STATE/calls")" "stop bombagif-selfbot.service" "stop never needs the .env"

# --- units -------------------------------------------------------------------------------------------------
DIR="/srv/bombagif"; VENV="$DIR/.venv-selfbot"
unit="$(render_unit alice alice)"
assert_contains "$unit" "ExecStart=/srv/bombagif/.venv-selfbot/bin/python /srv/bombagif/selfbot.py" "unit ExecStart"
assert_contains "$unit" "WorkingDirectory=/srv/bombagif" "unit workdir"
assert_contains "$unit" "User=alice" "unit user"
assert_contains "$unit" "Restart=on-failure" "unit restart"
assert_contains "$unit" "SELFBOT_UI=json" "unit ui"
assert_not_contains "$unit" "TOKEN" "unit must not carry secrets"
assert_contains "$(render_update_unit alice alice)" "selfbot-ctl.sh update --quiet" "update unit"
assert_contains "$(render_timer 15)" "OnUnitActiveSec=15min" "timer"
sudoers="$(render_sudoers alice)"
assert_contains "$sudoers" "alice ALL=(root) NOPASSWD: /usr/bin/systemctl restart bombagif-selfbot.service" "sudoers"
assert_not_contains "$sudoers" "ALL, " "sudoers stays narrow"
assert_not_contains "$sudoers" "NOPASSWD: ALL" "sudoers stays narrow"

# --- update flow -------------------------------------------------------------------------------------------
git_ident=(-c user.name=t -c user.email=t@example.test)
make_repos() {
  rm -rf "$TEST_ROOT/origin.git" "$TEST_ROOT/work" "$TEST_ROOT/vm"
  git init -q --bare -b main "$TEST_ROOT/origin.git"
  git clone -q "$TEST_ROOT/origin.git" "$TEST_ROOT/work" 2>/dev/null
  echo "discord.py-self==1" >"$TEST_ROOT/work/requirements-selfbot.txt"
  echo "v1" >"$TEST_ROOT/work/app.txt"
  git -C "$TEST_ROOT/work" add -A && git -C "$TEST_ROOT/work" "${git_ident[@]}" commit -q -m "first"
  git -C "$TEST_ROOT/work" push -q origin HEAD:main 2>/dev/null
  git clone -q "$TEST_ROOT/origin.git" "$TEST_ROOT/vm" 2>/dev/null
  DIR="$TEST_ROOT/vm"
  : >"$FAKE_STATE/calls"; echo active >"$FAKE_STATE/active"; echo 0 >"$FAKE_STATE/restarts"; rm -f "$FAKE_STATE/crash-on-restart"
  export SELFBOT_CTL_SMOKE="true"
}
push_change() {  # push_change <file> <content> <message>
  echo "$2" >"$TEST_ROOT/work/$1"
  git -C "$TEST_ROOT/work" add -A && git -C "$TEST_ROOT/work" "${git_ident[@]}" commit -q -m "$3"
  git -C "$TEST_ROOT/work" push -q origin HEAD:main 2>/dev/null
}
head_of() { git -C "$DIR" rev-parse HEAD; }

# already up to date: nothing restarts
make_repos
out="$(cmd_update 2>&1)"; assert_contains "$out" "Already up to date" "up to date"
assert_not_contains "$(cat "$FAKE_STATE/calls")" "restart" "no restart when up to date"

# a new commit: fast-forward, restart, no pip when requirements are untouched
make_repos; push_change app.txt v2 "second change"
out="$(cmd_update 2>&1)"
assert_contains "$out" "second change" "update shows what is new"
assert_contains "$out" "healthy" "update reports healthy"
assert_equal "v2" "$(cat "$DIR/app.txt")" "working tree updated"
assert_contains "$(cat "$FAKE_STATE/calls")" "restart bombagif-selfbot.service" "restarted"
assert_not_contains "$(cat "$FAKE_STATE/calls")" "pip" "no pip when requirements are unchanged"

# requirements changed: pip runs before the restart
make_repos; push_change requirements-selfbot.txt "discord.py-self==2" "bump dep"
cmd_update >/dev/null 2>&1
calls="$(cat "$FAKE_STATE/calls")"
assert_contains "$calls" "pip -r" "pip ran for a changed requirements file"
[[ "${calls%%pip*}" != "$calls" ]] || fail "pip did not run"
before_pip="${calls%%pip*}"; assert_not_contains "$before_pip" "restart" "pip runs before the restart"

# local edits on the VM: refuse, change nothing
make_repos; push_change app.txt v2 "second change"
echo "edited on the vm" >>"$DIR/app.txt"
old_head="$(head_of)"
if (cmd_update >/dev/null 2>&1); then fail "a dirty tree must refuse to update"; fi
assert_equal "$old_head" "$(head_of)" "dirty tree left alone"

# the new version will not start: rolled back, restarted on the old version, update fails
make_repos; push_change app.txt v2 "breaks things"
old_head="$(head_of)"; touch "$FAKE_STATE/crash-on-restart"
if (cmd_update >/dev/null 2>&1); then fail "a version that does not stay up must fail the update"; fi
assert_equal "$old_head" "$(head_of)" "rolled back to the previous commit"
assert_equal "v1" "$(cat "$DIR/app.txt")" "old files restored"
restarts="$(grep -c '^restart' "$FAKE_STATE/calls")"; assert_equal 2 "$restarts" "restarted once for the update and once for the rollback"

# the pre-restart smoke check fails: never restarted onto the new code, rolled back
make_repos; push_change app.txt v2 "does not import"
old_head="$(head_of)"; export SELFBOT_CTL_SMOKE="false"
if (cmd_update >/dev/null 2>&1); then fail "a failing smoke check must fail the update"; fi
assert_equal "$old_head" "$(head_of)" "rolled back after a failing smoke check"
export SELFBOT_CTL_SMOKE="true"

# a rollback after a dependency bump reinstalls the old requirements
make_repos; push_change requirements-selfbot.txt "discord.py-self==3" "bad bump"
touch "$FAKE_STATE/crash-on-restart"
(cmd_update >/dev/null 2>&1) || true
assert_equal "discord.py-self==1" "$(cat "$DIR/requirements-selfbot.txt")" "requirements rolled back"
assert_equal 2 "$(grep -c '^pip' "$FAKE_STATE/calls")" "pip ran for the update and again for the rollback"

# diverged history: refuse
make_repos; push_change app.txt v2 "remote"
echo local >"$DIR/local.txt"; git -C "$DIR" add -A; git -C "$DIR" "${git_ident[@]}" commit -q -m "local only"
if (cmd_update >/dev/null 2>&1); then fail "diverged history must refuse"; fi

# the script itself
bash -n "$ROOT/selfbot-ctl.sh"
out="$("$ROOT/selfbot-ctl.sh" help)"; assert_contains "$out" "update" "usage mentions update"
if "$ROOT/selfbot-ctl.sh" nonsense >/dev/null 2>&1; then fail "unknown command must fail"; fi

echo "selfbot-ctl checks passed."
