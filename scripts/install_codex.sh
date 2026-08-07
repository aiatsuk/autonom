#!/usr/bin/env bash
# Register Autonom as a Codex plugin from this checkout/bundle.
#
# Codex discovers plugins through a marketplace manifest
# (.agents/plugins/marketplace.json), so registration is: add this directory as a
# local marketplace, then add the plugin from it. If the `codex` CLI is present
# this runs those commands; otherwise it prints them, because inventing a plugin
# directory layout would be worse than an honest manual step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$ROOT/.agents/plugins/marketplace.json"

[ -f "$manifest" ] || { echo "no marketplace manifest at $manifest" >&2; exit 1; }

if command -v codex >/dev/null 2>&1; then
  echo "[codex] adding marketplace: $ROOT"
  codex plugin marketplace add "$ROOT"
  echo "[codex] adding plugin: autonom@autonom"
  codex plugin add autonom@autonom
  codex plugin list
  echo "[codex] done — start a new Codex thread so the skill index reloads."
else
  echo "codex CLI not found on PATH. Register manually:" >&2
  echo "  codex plugin marketplace add $ROOT" >&2
  echo "  codex plugin add autonom@autonom" >&2
  exit 0
fi
