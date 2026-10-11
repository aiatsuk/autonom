#!/bin/sh
# Start the Autonom probe MCP server with the first Python >= 3.9 found.
#
# ChatGPT desktop starts this script with launchd's short PATH, so the usual
# install locations are appended first. Nothing is ever written to stdout
# here: stdout belongs to the MCP message stream of the server.
set -eu

PLUGIN_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PATH="${PATH:-}:/opt/homebrew/bin:/usr/local/bin:${HOME:-}/.local/bin:/usr/bin:/bin"
export PATH

# usable CANDIDATE: true when CANDIDATE is an executable Python >= 3.9.
usable() {
  [ -n "$1" ] || return 1
  [ -x "$1" ] || return 1
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    </dev/null >/dev/null 2>&1
}

py=""
if [ -n "${AUTONOM_PROBE_PYTHON:-}" ] && usable "$AUTONOM_PROBE_PYTHON"; then
  py=$AUTONOM_PROBE_PYTHON
fi
if [ -z "$py" ]; then
  for name in python3.14 python3.13 python3.12 python3.11; do
    candidate=$(command -v "$name" 2>/dev/null || true)
    if usable "$candidate"; then
      py=$candidate
      break
    fi
  done
fi
if [ -z "$py" ]; then
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if usable "$candidate"; then
      py=$candidate
      break
    fi
  done
fi
if [ -z "$py" ]; then
  candidate=$(command -v python3 2>/dev/null || true)
  if usable "$candidate"; then
    py=$candidate
  fi
fi

if [ -z "$py" ]; then
  printf '%s\n' '{"ok": false, "error_code": "python_not_found", "error": "No Python >= 3.9 found for the Autonom probe", "hint": "Install Python 3 (python.org or Homebrew) or set AUTONOM_PROBE_PYTHON"}' >&2
  exit 127
fi

exec "$py" "$PLUGIN_DIR/server/probe_server.py"
