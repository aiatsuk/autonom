#!/usr/bin/env bash
# Register Autonom as a Claude Code plugin from this checkout/bundle.
#
# Claude Code discovers plugins through a marketplace manifest
# (.claude-plugin/marketplace.json), so registration is: add this directory as a
# local marketplace, then install the plugin from it. Installed plugins are
# copied into a versioned cache, so after changing the source re-run this
# script or `claude plugin update autonom@autonom`. Without the `claude` CLI
# this falls back to laying loose skills into ~/.claude/skills.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$ROOT/.claude-plugin/marketplace.json"

[ -f "$manifest" ] || { echo "no marketplace manifest at $manifest" >&2; exit 1; }

if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI not found on PATH — installing loose skills into ~/.claude/skills instead." >&2
  exec "$ROOT/scripts/install_skills.sh" claude
fi

echo "[claude] adding marketplace: $ROOT"
if ! claude plugin marketplace add "$ROOT"; then
  if claude plugin marketplace list 2>/dev/null | grep -q "autonom"; then
    echo "[claude] marketplace 'autonom' already registered — continuing"
  else
    echo "failed to add marketplace from $ROOT" >&2
    exit 1
  fi
fi

echo "[claude] installing plugin: autonom@autonom"
if ! claude plugin install autonom@autonom; then
  if claude plugin list 2>/dev/null | grep -q "autonom"; then
    echo "[claude] plugin already installed — updating instead"
  else
    echo "failed to install autonom@autonom" >&2
    exit 1
  fi
fi

# "Already installed" leaves the versioned cache as it was, so a re-run after
# editing the source would silently keep serving the old copy — refresh it.
claude plugin update autonom@autonom || true

echo "[claude] done — start a new Claude session so the plugin loads (skills appear as autonom:<name>)."
