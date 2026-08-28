#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Belt: redirect the machine store for the whole run so even a future
# un-isolated test cannot touch the operator's real ~/.autonom. This does not
# replace per-test isolation (tests/env_isolation.py) — a direct
# `python3 -m unittest tests.test_x` run would still depend on it.
AUTONOM_CHECK_HOME="$(mktemp -d)"
export AUTONOM_HOME="$AUTONOM_CHECK_HOME"
trap 'rm -rf "$AUTONOM_CHECK_HOME"' EXIT

python3 scripts/validate_plugin.py .
python3 -m compileall -q scripts tests plugins/autonom/skills

while IFS= read -r -d '' module; do
  node --check "$module"
done < <(find plugins tests -type f -name '*.mjs' -print0)

python3 -m unittest discover -s tests -v

# The suite above may run headless, where the consent gate's interactive branch
# is never taken. Re-run it with a stdin that claims to be a TTY and raises on
# read, so a test that would block a developer's terminal fails here instead.
python3 tests/tty_guard.py >/dev/null || {
  echo "A test read the terminal; re-run 'python3 tests/tty_guard.py' for details." >&2
  exit 1
}

node --test tests/*.test.mjs

# Collect the shell scripts once (a crashed release build can leave a staged
# copy of everything under dist/, so prune it) and lint the same set twice.
shell_scripts=()
while IFS= read -r -d '' script; do
  shell_scripts+=("$script")
done < <(find . -path ./dist -prune -o -type f -name '*.sh' -print0)

for script in "${shell_scripts[@]}"; do
  bash -n "$script"
done

if command -v shellcheck >/dev/null 2>&1; then
  if ((${#shell_scripts[@]})); then
    shellcheck "${shell_scripts[@]}"
  fi
elif [ "${AUTONOM_REQUIRE_SHELLCHECK:-0}" = "1" ]; then
  # CI sets AUTONOM_REQUIRE_SHELLCHECK=1 so a runner without shellcheck fails
  # loudly instead of silently skipping the lint. Local runs stay lenient.
  echo "shellcheck is required (AUTONOM_REQUIRE_SHELLCHECK=1) but not installed." >&2
  exit 1
fi

printf '\nAll checks passed.\n'
