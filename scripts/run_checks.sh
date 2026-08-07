#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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

while IFS= read -r -d '' script; do
  bash -n "$script"
done < <(find . -type f -name '*.sh' -print0)

if command -v shellcheck >/dev/null 2>&1; then
  shell_scripts=()
  while IFS= read -r -d '' script; do
    shell_scripts+=("$script")
  done < <(find . -type f -name '*.sh' -print0)
  if ((${#shell_scripts[@]})); then
    shellcheck "${shell_scripts[@]}"
  fi
fi

printf '\nAll checks passed.\n'
