#!/usr/bin/env bash
# Real-emulator smoke for CI: drives the preinstalled Settings app through the
# autonom CLI — device discovery → session → launch → tree → exact find/tap →
# screenshot → teardown — and asserts machine-readable JSON plus on-disk
# artifacts at every step.
#
# What this proves: the CLI pipeline works against a real UI Automator/adb
# backend and artifacts land where the docs say. What it does NOT prove:
# anything iOS, third-party APK install, the MITM/consent path, other API
# levels or locales, physical devices, performance
# (see docs/plans/PHASE_0_RELEASE_ENGINEERING.md).
#
# Mutating actions (the tap) are never retried; only read-only polling is
# used while the UI settles, and an `ambiguous_selector` refusal aborts
# immediately — a permanent refusal must not be reported as a timeout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

export AUTONOM_HOME="${AUTONOM_HOME:-$(mktemp -d)}"
OUT="$(mktemp -d)"
CLI=(python3 scripts/autonom.py --platform android)

step() { printf '\n=== %s\n' "$*"; }

# assert_json <file> <python-expr>: the parsed payload is bound to `p`;
# a falsy expression fails the script with the payload shown.
assert_json() {
  python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    p = json.load(fh)
if not eval(sys.argv[2], {"p": p}):
    sys.exit("assertion failed: %s\npayload: %s" % (sys.argv[2], json.dumps(p, indent=2)[:2000]))
PY
}

# jget <file> <python-expr>: print the expression's value.
jget() {
  python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    p = json.load(fh)
print(eval(sys.argv[2], {"p": p}))
PY
}

step "1. devices list shows exactly one android device"
"${CLI[@]}" devices >"$OUT/devices.json"
assert_json "$OUT/devices.json" 'p["ok"] and len([d for d in p["devices"] if d["platform"] == "android"]) == 1'

step "2. session start"
"${CLI[@]}" session start >"$OUT/session.json"
assert_json "$OUT/session.json" 'p["ok"]'
SESSION_ID="$(jget "$OUT/session.json" 'p["session_id"]')"
SESSION_DIR="$AUTONOM_HOME/sessions/$SESSION_ID"
echo "session: $SESSION_ID"

step "3. launch the Settings app"
"${CLI[@]}" session launch com.android.settings >"$OUT/launch.json"
assert_json "$OUT/launch.json" 'p["ok"]'

step "4. read-only poll until the Battery row is uniquely visible"
deadline=$(( $(date +%s) + 60 ))
found=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if "${CLI[@]}" ui find --text Battery --mode exact >"$OUT/find.json" 2>"$OUT/find.err"; then
    count="$(jget "$OUT/find.json" 'p.get("count", 0)')"
    if [ "$count" = "1" ]; then
      found=1
      break
    fi
  else
    code="$(jget "$OUT/find.err" 'p.get("error_code", "")' 2>/dev/null || echo unparsed)"
    case "$code" in
      ambiguous_selector|no_active_session|adb_not_found)
        # A permanent refusal must not be reported as a timeout.
        echo "FATAL: 'ui find' failed permanently with $code:" >&2
        cat "$OUT/find.err" >&2
        exit 1 ;;
      unparsed)
        # Non-JSON stderr means the CLI itself crashed (traceback, argparse
        # error) — that is permanent, not a flake to retry for 60 seconds.
        echo "FATAL: 'ui find' produced a non-JSON failure (CLI crash?):" >&2
        cat "$OUT/find.err" >&2
        exit 1 ;;
      *)
        # Likely-transient (a dump race right after launch): keep polling,
        # but leave a trace so a real failure is not misread as flake.
        echo "attempt failed with error_code=$code; retrying" >&2 ;;
    esac
  fi
  sleep 2
done
if [ "$found" != "1" ]; then
  echo "FATAL: the Battery row never became uniquely visible within 60s" >&2
  cat "$OUT/find.err" >&2 2>/dev/null || true
  exit 1
fi

step "5. ui tree saves an artifact under the session"
"${CLI[@]}" ui tree >"$OUT/tree.json"
assert_json "$OUT/tree.json" 'p["ok"] and p["count"] > 0 and p.get("saved")'
TREE_SAVED="$(jget "$OUT/tree.json" 'p["saved"]')"
test -s "$TREE_SAVED"
case "$TREE_SAVED" in
  "$SESSION_DIR"/*) ;;
  *) echo "FATAL: tree artifact $TREE_SAVED is outside $SESSION_DIR" >&2; exit 1 ;;
esac

step "6. tap the Battery row (exact selector, exactly once, no retry)"
"${CLI[@]}" ui tap --text Battery --mode exact >"$OUT/tap.json"
assert_json "$OUT/tap.json" 'p["ok"]'

step "7. post-tap tree is readable"
"${CLI[@]}" ui tree >"$OUT/tree2.json"
assert_json "$OUT/tree2.json" 'p["ok"]'

step "8. screenshot lands and is non-empty"
"${CLI[@]}" screenshot --label smoke >"$OUT/shot.json"
assert_json "$OUT/shot.json" 'p["ok"]'
SHOT_PATH="$(jget "$OUT/shot.json" 'p["path"]')"
test -s "$SHOT_PATH"

step "9. flow run: the same journey as a repeatable Flow v1 file"
"${CLI[@]}" flow run tests/fixtures/flows/settings_smoke.yaml >"$OUT/flowrun.json"
assert_json "$OUT/flowrun.json" 'p["ok"] and p["status"] == "passed"'
EVENTS_PATH="$(jget "$OUT/flowrun.json" 'p["events"]')"
test -s "$EVENTS_PATH"

step "10. session stop + artifact assertions"
"${CLI[@]}" session stop >"$OUT/stop.json"
assert_json "$OUT/stop.json" 'p["ok"]'
# `session stop` itself is not journaled (the current-session pointer is
# already cleared when the choke point runs), so steps 2-8 provide the lines.
if [ ! -s "$SESSION_DIR/journal.ndjson" ]; then
  # Guarded: under set -e a bare redirect failure would exit with only
  # bash's "No such file" instead of this diagnosis.
  echo "FATAL: no journal at $SESSION_DIR/journal.ndjson" >&2
  exit 1
fi
JOURNAL_LINES="$(wc -l <"$SESSION_DIR/journal.ndjson")"
if [ "$JOURNAL_LINES" -lt 6 ]; then
  echo "FATAL: expected >= 6 journal lines, found $JOURNAL_LINES" >&2
  exit 1
fi
if [ ! -d "$SESSION_DIR/shots" ]; then
  echo "FATAL: no shots directory under $SESSION_DIR" >&2
  exit 1
fi
PNG_COUNT="$(find "$SESSION_DIR/shots" -name '*.png' | wc -l)"
if [ "$PNG_COUNT" -lt 1 ]; then
  echo "FATAL: no screenshots under $SESSION_DIR/shots" >&2
  exit 1
fi

printf '\nSmoke passed: session %s, %s journal lines, %s screenshot(s).\n' \
  "$SESSION_ID" "$JOURNAL_LINES" "$PNG_COUNT"
