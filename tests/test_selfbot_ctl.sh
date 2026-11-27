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
export SELFBOT_CTL_SUDOERS_DIR="$TEST_ROOT/etc/sudoers.d"
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
assert_contains "$(render_update_path_unit)" "PathExists=/srv/bombagif/data/update-request" "path unit watches the request"
assert_contains "$(render_update_path_unit)" "WantedBy=paths.target" "path unit is enabled by paths.target"

# --- auto-update -------------------------------------------------------------------------------------------
# A fake root: rewrite the absolute system paths into the test root and drop the root ownership flags, so the
# real /etc is never touched. It is a binary rather than a function so the script the update re-invokes gets it.
mkdir -p "$TEST_ROOT/etc/sudoers.d" "$TEST_ROOT/etc/systemd/system"
export TEST_ROOT
cat >"$FAKE_BIN/sudo" <<'FAKE'
#!/usr/bin/env bash
while [[ "${1:-}" == -* ]]; do shift; done
[[ $# -eq 0 ]] && exit 0
args=()
for arg in "$@"; do
  case "$arg" in
    /etc/*) args+=("$TEST_ROOT$arg") ;;
    *) args+=("$arg") ;;
  esac
done
if [[ "${args[0]}" == "install" ]]; then
  filtered=("install"); skip=0
  for arg in "${args[@]:1}"; do
    if (( skip > 0 )); then skip=$((skip - 1)); continue; fi
    case "$arg" in
      -o|-g) skip=1 ;;
      *) filtered+=("$arg") ;;
    esac
  done
  args=("${filtered[@]}")
fi
exec "${args[@]}"
FAKE
chmod +x "$FAKE_BIN/sudo"
export SELFBOT_CTL_SUDO="$FAKE_BIN/sudo"
DIR="/srv/bombagif"

: >"$FAKE_STATE/calls"
cmd_auto on 15 >/dev/null 2>&1 || fail "auto on should succeed"
assert_contains "$(cat "$FAKE_STATE/calls")" "enable --now bombagif-selfbot-update.timer" "the timer is enabled"
[[ -f "$UNIT_DIR/bombagif-selfbot-update.service" ]] || fail "auto on writes the update service"
[[ -f "$UNIT_DIR/bombagif-selfbot-update.timer" ]] || fail "auto on writes the timer"
assert_contains "$(cat "$UNIT_DIR/bombagif-selfbot-update.timer")" "OnUnitActiveSec=15min" "the timer uses the requested interval"
assert_contains "$(cat "$UNIT_DIR/bombagif-selfbot-update.service")" "selfbot-ctl.sh update --quiet" "the timer runs a quiet update"
[[ -f "$UNIT_DIR/bombagif-selfbot-update.path" ]] || fail "auto on writes the path unit !update asks through"
assert_contains "$(cat "$UNIT_DIR/bombagif-selfbot-update.path")" "PathExists=/srv/bombagif/data/update-request" "the path unit watches the request"
assert_contains "$(cat "$FAKE_STATE/calls")" "enable --now bombagif-selfbot-update.path" "the path unit is enabled"
sudoers_file="$TEST_ROOT/etc/sudoers.d/bombagif-selfbot"
[[ ! -f "$sudoers_file" ]] || fail "nothing grants the bot sudo any more"

# an interval under a minute is refused before anything is installed
: >"$FAKE_STATE/calls"
if (cmd_auto on 0 >/dev/null 2>&1); then fail "auto on 0 must be refused"; fi
assert_not_contains "$(cat "$FAKE_STATE/calls")" "enable" "a refused interval enables nothing"

# auto off takes the timer, the units and any stale sudoers entry away again
echo "stale rule" >"$sudoers_file"
cmd_auto off >/dev/null 2>&1 || fail "auto off should succeed"
assert_contains "$(cat "$FAKE_STATE/calls")" "disable --now bombagif-selfbot-update.timer" "the timer is disabled"
[[ ! -f "$UNIT_DIR/bombagif-selfbot-update.timer" ]] || fail "auto off removes the timer"
[[ ! -f "$UNIT_DIR/bombagif-selfbot-update.service" ]] || fail "auto off removes the update service"
[[ ! -f "$UNIT_DIR/bombagif-selfbot-update.path" ]] || fail "auto off removes the path unit"
[[ ! -f "$sudoers_file" ]] || fail "auto off removes a stale sudoers entry"

# update-service installs the unit and the path unit without the timer, so !update works on its own
: >"$FAKE_STATE/calls"
echo "stale rule" >"$sudoers_file"
cmd_update_service >/dev/null 2>&1 || fail "update-service should succeed"
[[ -f "$UNIT_DIR/bombagif-selfbot-update.service" ]] || fail "update-service writes the update unit"
[[ -f "$UNIT_DIR/bombagif-selfbot-update.path" ]] || fail "update-service writes the path unit"
[[ ! -f "$UNIT_DIR/bombagif-selfbot-update.timer" ]] || fail "update-service must not install the timer"
assert_contains "$(cat "$FAKE_STATE/calls")" "daemon-reload" "update-service reloads systemd"
assert_contains "$(cat "$FAKE_STATE/calls")" "enable --now bombagif-selfbot-update.path" "the watcher is enabled"
[[ ! -f "$sudoers_file" ]] || fail "update-service clears the sudoers rule the bot cannot use"

# --if-needed is what an update runs: silent when the units are already there, never fatal without root
: >"$FAKE_STATE/calls"
cmd_update_service --if-needed >/dev/null 2>&1 || fail "--if-needed should succeed"
assert_not_contains "$(cat "$FAKE_STATE/calls")" "daemon-reload" "--if-needed does nothing when the units are current"
rm -f "$UNIT_DIR/bombagif-selfbot-update.path"
out="$(SELFBOT_CTL_SUDO=/bin/false cmd_update_service --if-needed 2>&1)" || fail "--if-needed must not die without root"
assert_contains "$out" "update-service" "it says how to fix a missing watcher"
[[ ! -f "$UNIT_DIR/bombagif-selfbot-update.path" ]] || fail "--if-needed must not write without root"
cmd_update_service >/dev/null 2>&1 || fail "update-service should still work"

# --- waiting for idle before a restart ---------------------------------------------------------------------
DIR="$TEST_ROOT/idle"; mkdir -p "$DIR/data"
busy_file="$DIR/data/busy.json"
export SELFBOT_IDLE_STEP_SECONDS=0 SELFBOT_IDLE_WAIT_SECONDS=0

# no marker at all: nothing to wait for
rm -f "$busy_file"
out="$(wait_for_idle 2>&1)"; assert_equal "" "$out" "no marker means idle"

# a fresh marker with jobs: reported, then the wait is capped so an update never hangs
printf '{"jobs": 2, "at": %s}' "$(date +%s)" >"$busy_file"
out="$(wait_for_idle 2>&1)"
assert_contains "$out" "Still busy with 2 job" "a busy bot is reported"
assert_contains "$out" "updating anyway" "the wait is capped"

# a stale marker (the bot died mid-job) is ignored
printf '{"jobs": 2, "at": %s}' "$(( $(date +%s) - 600 ))" >"$busy_file"
out="$(wait_for_idle 2>&1)"; assert_not_contains "$out" "Busy" "a stale marker is ignored"

# it re-checks after the step and returns as soon as the work is done
printf '{"jobs": 1, "at": %s}' "$(date +%s)" >"$busy_file"
( sleep 0.5; printf '{"jobs": 0, "at": %s}' "$(date +%s)" >"$busy_file" ) &
out="$(SELFBOT_IDLE_STEP_SECONDS=1 SELFBOT_IDLE_WAIT_SECONDS=10 wait_for_idle 2>&1)" || fail "it should return once idle"
wait
assert_contains "$out" "Busy with 1 job" "the wait is announced"
assert_not_contains "$out" "updating anyway" "it never hit the cap"

# --- update flow -------------------------------------------------------------------------------------------
git_ident=(-c user.name=t -c user.email=t@example.test)
make_repos() {
  rm -rf "$TEST_ROOT/origin.git" "$TEST_ROOT/work" "$TEST_ROOT/vm"
  git init -q --bare -b main "$TEST_ROOT/origin.git"
  git clone -q "$TEST_ROOT/origin.git" "$TEST_ROOT/work" 2>/dev/null
  # a machine-wide commit/push policy hook must not police these throwaway repos
  git -C "$TEST_ROOT/work" config core.hooksPath ""
  echo "discord.py-self==1" >"$TEST_ROOT/work/requirements-selfbot.txt"
  echo "v1" >"$TEST_ROOT/work/app.txt"
  git -C "$TEST_ROOT/work" add -A && git -C "$TEST_ROOT/work" "${git_ident[@]}" commit -q -m "first"
  git -C "$TEST_ROOT/work" push -q origin HEAD:main 2>/dev/null
  git clone -q "$TEST_ROOT/origin.git" "$TEST_ROOT/vm" 2>/dev/null
  git -C "$TEST_ROOT/vm" config core.hooksPath ""
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

# an update installs the units !update needs, so a host that predates them starts working after one update
make_repos
cp "$ROOT/selfbot-ctl.sh" "$TEST_ROOT/work/selfbot-ctl.sh"
git -C "$TEST_ROOT/work" add -A && git -C "$TEST_ROOT/work" "${git_ident[@]}" commit -q -m "add the ctl script"
git -C "$TEST_ROOT/work" push -q origin HEAD:main 2>/dev/null
git -C "$DIR" pull -q --ff-only 2>/dev/null
rm -f "$UNIT_DIR/bombagif-selfbot-update.path" "$UNIT_DIR/bombagif-selfbot-update.service"
push_change app.txt v2 "second change"
cmd_update >/dev/null 2>&1 || fail "update should succeed"
[[ -f "$UNIT_DIR/bombagif-selfbot-update.path" ]] || fail "update must install the path unit !update asks through"
[[ -f "$UNIT_DIR/bombagif-selfbot-update.service" ]] || fail "update must install the update unit"

# an update clears the request that started it, so a reload cannot retrigger it forever
make_repos
mkdir -p "$DIR/data"; echo 1 >"$DIR/data/update-request"
push_change app.txt v2 "second change"
cmd_update >/dev/null 2>&1 || fail "update should succeed"
[[ ! -f "$DIR/data/update-request" ]] || fail "update must clear the request it consumed"

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

# --- update notice: what the bot DMs the allowlist after an unattended update ---------------------------------
notice_of() { printf '%s' "$DIR/data/update-notice.json"; }

# an up-to-date check writes nothing
make_repos
cmd_update >/dev/null 2>&1
[[ ! -f "$(notice_of)" ]] || fail "an up-to-date check must not write a notice"

# a successful update records the old and new version plus every commit it brought
make_repos; push_change app.txt v2 "second change"
cmd_update >/dev/null 2>&1
notice="$(notice_of)"
[[ -f "$notice" ]] || fail "a successful update should leave a notice for the bot"
assert_equal 600 "$(stat -c %a "$notice" 2>/dev/null || stat -f %Lp "$notice")" "the notice is mode 600"
assert_contains "$(cat "$notice")" '"outcome": "updated"' "the notice says the update went through"
assert_contains "$(cat "$notice")" 'second change' "the notice lists the new commit"
assert_contains "$(cat "$notice")" "\"to\": \"$(git -C "$DIR" rev-parse --short HEAD)\"" "the notice names the new version"

# a rollback is recorded too, so the allowlist hears that nothing changed
make_repos; push_change app.txt v2 "breaks things"
touch "$FAKE_STATE/crash-on-restart"
(cmd_update >/dev/null 2>&1) || true
assert_contains "$(cat "$(notice_of)")" '"outcome": "rolled-back"' "a rollback is recorded"
assert_contains "$(cat "$(notice_of)")" 'breaks things' "the rollback notice names what was tried"
rm -f "$FAKE_STATE/crash-on-restart"; echo active >"$FAKE_STATE/active"  # leave the fake service healthy for what follows

# a busy bot is given a moment before the restart, then updated anyway
make_repos; push_change app.txt v2 "second change"
mkdir -p "$DIR/data"; printf '{"jobs": 1, "at": %s}' "$(date +%s)" >"$DIR/data/busy.json"
out="$(cmd_update 2>&1)"
assert_contains "$out" "Still busy with 1 job" "the update waits for in-flight work"
assert_contains "$out" "healthy" "it still updates once the wait is capped"

# --- timezone -------------------------------------------------------------------------------------------------
cat >"$FAKE_BIN/timedatectl" <<'FAKE'
#!/usr/bin/env bash
echo "td $*" >>"$FAKE_STATE/calls"
case "$1" in
  show) cat "$FAKE_STATE/tz" 2>/dev/null || echo Etc/UTC ;;
  list-timezones) printf 'America/New_York\nAsia/Kolkata\nEtc/UTC\n' ;;
  set-timezone) echo "$2" >"$FAKE_STATE/tz" ;;
esac
FAKE
chmod +x "$FAKE_BIN/timedatectl"
export SELFBOT_CTL_TIMEDATECTL="$FAKE_BIN/timedatectl"
rm -f "$FAKE_STATE/tz"; : >"$FAKE_STATE/calls"
assert_contains "$(main timezone 2>&1)" "Etc/UTC" "shows the current zone"
main timezone America/New_York >/dev/null 2>&1 || fail "a known zone should be accepted"
assert_equal "America/New_York" "$(cat "$FAKE_STATE/tz")" "zone was set"
assert_contains "$(cat "$FAKE_STATE/calls")" "restart bombagif-selfbot.service" "the running bot is restarted to pick it up"
for bad in "Mars/Olympus" "../etc/passwd" 'x; rm -rf /' ""; do
  [[ -z "$bad" ]] && continue
  if (main timezone "$bad" >/dev/null 2>&1); then fail "timezone '$bad' must be rejected"; fi
done
assert_equal "America/New_York" "$(cat "$FAKE_STATE/tz")" "a rejected zone changes nothing"

# --- training form ---------------------------------------------------------------------------------------------
DIR="$TEST_ROOT/training"; mkdir -p "$DIR"
printf '# keep me\nSELFBOT_TOKEN=secret-token\nSELFBOT_SCOUT_MODE=off\n' >"$DIR/.env"
chmod 644 "$DIR/.env"
echo active >"$FAKE_STATE/active"; : >"$FAKE_STATE/calls"
main training on >/dev/null 2>&1 || fail "training on should work"
assert_equal 600 "$(stat -c %a "$DIR/.env" 2>/dev/null || stat -f %Lp "$DIR/.env")" "training keeps .env mode 600"
assert_equal "dry-run" "$(env_get SELFBOT_SCOUT_MODE)" "training sets dry-run"
assert_equal "always" "$(env_get SCOUT_REVIEW)" "training reviews always"
assert_equal "30" "$(env_get SCOUT_REVIEW_PER_HOUR)" "training raises the review rate"
assert_equal "5" "$(env_get SCOUT_REVIEW_BATCH)" "training batches the review DMs"
assert_equal "5" "$(env_get SCOUT_REVIEW_BATCH_MINUTES)" "training sends a partial batch quickly"
assert_equal "0" "$(env_get SCOUT_NEAR_MISS_DECIDE_FUNNY)" "training reviews every judged run"
assert_equal "0" "$(env_get SCOUT_NEAR_MISS_FINAL_FUNNY)" "training reviews every judged run"
assert_equal "12" "$(env_get SCOUT_EXAMPLES)" "training shows more examples"
assert_equal "on" "$(env_get SCOUT_MANUAL)" "training keeps manual clips on by default"
assert_equal "secret-token" "$(env_get SELFBOT_TOKEN)" "the token is untouched"
assert_contains "$(cat "$DIR/.env")" "# keep me" "comments are kept"
assert_contains "$(cat "$FAKE_STATE/calls")" "restart bombagif-selfbot.service" "training restarts the bot"
main training on >/dev/null 2>&1
assert_equal 1 "$(grep -c '^SCOUT_REVIEW=' "$DIR/.env")" "a key is replaced, not duplicated"
main training off >/dev/null 2>&1 || fail "training off should work"
assert_equal "dry-run" "$(env_get SCOUT_REVIEW)" "off restores the review default"
assert_equal "6" "$(env_get SCOUT_REVIEW_PER_HOUR)" "off restores the review rate"
assert_equal "10" "$(env_get SCOUT_REVIEW_BATCH)" "off restores the batch size"
assert_equal "15" "$(env_get SCOUT_REVIEW_BATCH_MINUTES)" "off restores the batch timer"
assert_equal "0.6" "$(env_get SCOUT_NEAR_MISS_DECIDE_FUNNY)" "off restores the near-miss bar"
assert_equal "6" "$(env_get SCOUT_NEAR_MISS_FINAL_FUNNY)" "off restores the near-miss bar"
assert_equal "8" "$(env_get SCOUT_EXAMPLES)" "off restores the example count"
assert_equal "dry-run" "$(env_get SELFBOT_SCOUT_MODE)" "off leaves the mode alone"
status="$(main training status 2>&1)"
assert_contains "$status" "SCOUT_REVIEW=dry-run" "status shows the settings"
assert_not_contains "$status" "secret-token" "status never prints secrets"
if (main training sideways >/dev/null 2>&1); then fail "an unknown training action must fail"; fi

# --- voice form ------------------------------------------------------------------------------------------------
DIR="$TEST_ROOT/voice"; mkdir -p "$DIR"
printf 'SELFBOT_TOKEN=secret-token\n' >"$DIR/.env"
chmod 600 "$DIR/.env"
echo active >"$FAKE_STATE/active"; : >"$FAKE_STATE/calls"
main voice on >/dev/null 2>&1 || fail "voice on should work"
assert_equal "on" "$(env_get SCOUT_VOICE)" "voice on turns call clips on"
assert_contains "$(cat "$FAKE_STATE/calls")" "--no-deps discord-ext-voice-recv" "voice receive is installed without its discord.py dependency"
assert_contains "$(cat "$FAKE_STATE/calls")" "PyNaCl" "the voice encryption library is installed"
assert_contains "$(cat "$FAKE_STATE/calls")" "davey==0.1.7" "the DAVE library Discord requires for voice is installed"
assert_contains "$(cat "$FAKE_STATE/calls")" "restart bombagif-selfbot.service" "voice on restarts the bot"
main voice off >/dev/null 2>&1 || fail "voice off should work"
assert_equal "off" "$(env_get SCOUT_VOICE)" "voice off turns call clips off"
status="$(main voice status 2>&1)"
assert_contains "$status" "SCOUT_VOICE=off" "voice status shows the setting"
assert_contains "$status" "libopus" "voice status reports whether libopus is available"
assert_contains "$status" "davey" "voice status reports whether davey is installed"
assert_not_contains "$status" "secret-token" "voice status never prints secrets"
if (main voice sideways >/dev/null 2>&1); then fail "an unknown voice action must fail"; fi

# --- logs: entries written before the switch to text logs still read as text -----------------------------------
DIR="$ROOT"
cat >"$FAKE_BIN/journalctl" <<'FAKE'
#!/usr/bin/env bash
echo "journalctl $*" >>"$FAKE_STATE/calls"
printf 'Nov 24 23:49:35 host python[1]: {"time": "2026-11-25T04:49:35+00:00", "level": "INFO", "logger": "bombagif.scout", "message": "judged a run"}\n'
printf 'Nov 24 23:50:00 host python[1]: 23:50:00 INFO    scout      already text\n'
FAKE
chmod +x "$FAKE_BIN/journalctl"
export SELFBOT_CTL_JOURNALCTL="$FAKE_BIN/journalctl"
export SELFBOT_CTL_PYTHON="$(command -v python3)"
out="$(cmd_logs)"
assert_contains "$out" "INFO    scout      judged a run" "logs reformats a JSON entry"
assert_not_contains "$out" '{"time"' "logs hides the JSON"
assert_contains "$out" "python[1]: INFO    scout      already text" "logs drops the clock journald already printed"
assert_contains "$(cat "$FAKE_STATE/calls")" "-u bombagif-selfbot.service" "logs reads the service journal"
unset SELFBOT_CTL_JOURNALCTL SELFBOT_CTL_PYTHON

# --- status: says what is running, and where the one-screen view is ----------------------------------------------
DIR="$TEST_ROOT/statusdir"; mkdir -p "$DIR"
out="$(cmd_status 2>&1)"
assert_contains "$out" "service:" "status reports the service"
assert_contains "$out" "./selfbot-ctl.sh dashboard" "status points at the dashboard"

# --- dashboard: one command, pointed at the snapshot ------------------------------------------------------------
DIR="$TEST_ROOT/dash"; mkdir -p "$DIR/data"
cat >"$FAKE_BIN/python" <<'FAKE'
#!/usr/bin/env bash
echo "python $*" >>"$FAKE_STATE/calls"
FAKE
chmod +x "$FAKE_BIN/python"
export SELFBOT_CTL_PYTHON="$FAKE_BIN/python"
: >"$FAKE_STATE/calls"
main dashboard --once >/dev/null 2>&1 || fail "dashboard should run"
calls="$(cat "$FAKE_STATE/calls")"
assert_contains "$calls" "dashboard.py" "dashboard runs the renderer"
assert_contains "$calls" "--status $DIR/data/status.json" "dashboard points at the snapshot"
assert_contains "$calls" "--service bombagif-selfbot" "dashboard reports on the service"
assert_contains "$calls" "--once" "dashboard passes flags through"
unset SELFBOT_CTL_PYTHON

# the script itself
bash -n "$ROOT/selfbot-ctl.sh"
out="$("$ROOT/selfbot-ctl.sh" help)"; assert_contains "$out" "update" "usage mentions update"
assert_contains "$out" "training" "usage mentions training"
assert_contains "$out" "voice" "usage mentions voice"
assert_contains "$out" "dashboard" "usage mentions dashboard"
if "$ROOT/selfbot-ctl.sh" nonsense >/dev/null 2>&1; then fail "unknown command must fail"; fi

echo "selfbot-ctl checks passed."
