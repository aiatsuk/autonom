#!/usr/bin/env bash
# Build a self-contained, transferable Autonom bundle: the CLI, its stdlib-only
# library (including the real mitmproxy addon file), every skill, and the plugin
# manifest — plus an install.sh that lays it onto a target machine and into an
# agent with one command.
#
# A tarball, not a compiled binary, on purpose:
#   - the CLI is dependency-free Python, so there is nothing to compile;
#   - the network addon must exist as a real file on disk, because mitmdump loads
#     it by path (-s <file>) — a single-file executable cannot provide that.
# The "binary" experience is one file to copy and one command to install.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

version="$(grep -oE '"[0-9]+\.[0-9]+\.[0-9]+"' scripts/autonom_lib/__init__.py | tr -d '"' | head -1)"
[ -n "$version" ] || { echo "cannot read version from scripts/autonom_lib/__init__.py" >&2; exit 1; }

name="autonom-${version}"
stage="dist/${name}"
rm -rf "$stage"
mkdir -p "$stage"

# Curated runnable set — no tests, no caches, no VCS.
cp -R scripts "$stage/scripts"
cp -R plugins "$stage/plugins"
cp -R .agents "$stage/.agents"
cp -R .claude-plugin "$stage/.claude-plugin"
for extra in marketplace.json README.md CHANGELOG.md LICENSE; do
  [ -e "$extra" ] && cp "$extra" "$stage/"
done
find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$stage" -name '*.pyc' -delete 2>/dev/null || true

# The installer travels inside the bundle verbatim — the same file as the
# repository root's install.sh, which detects bundle context by the absence of
# .git and only then copies itself to AUTONOM_PREFIX.
cp install.sh "$stage/install.sh"
chmod +x "$stage/install.sh"

tarball="dist/${name}.tgz"
tar czf "$tarball" -C dist "$name"
rm -rf "$stage"

size="$(du -h "$tarball" | cut -f1)"
echo "built ${tarball} (${size})"
echo
echo "hand off: copy ${tarball} to the target any way you like, then:"
echo "  tar xzf ${name}.tgz && ./${name}/install.sh   # checkbox picker (or --all / claude codex grok)"
