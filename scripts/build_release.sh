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

resolve_version() {
  # The same resolver validate_plugin.py trusts — not a second regex parser.
  # release.yml calls `--print-version` so there is exactly one copy of this.
  python3 -c 'import sys; sys.path.insert(0, "scripts"); import autonom_lib; print(autonom_lib.__version__)'
}

run_checks=1
for arg in "$@"; do
  case "$arg" in
    --no-check) run_checks=0 ;;
    --print-version) resolve_version; exit 0 ;;
    *) echo "unknown argument: $arg (supported: --no-check, --print-version)" >&2; exit 2 ;;
  esac
done

# Pre-flight: the bundle must carry these; failing here beats failing after
# staging (or, worse, shipping without them — CHANGELOG.md did exactly that
# while it did not exist and the `[ -e ] &&` loop stayed silent).
for required in LICENSE CHANGELOG.md README.md install.sh; do
  [ -e "$required" ] || { echo "missing required release file: $required" >&2; exit 1; }
done

version="$(resolve_version)"
[ -n "$version" ] || { echo "cannot read version from scripts/autonom_lib/__init__.py" >&2; exit 1; }

# A stale dist/ stage would be swept by run_checks.sh's shell lint and by
# validate_plugin.py's markdown scan — clear it before checks, not after.
rm -rf dist

if [ "$run_checks" = "1" ]; then
  ./scripts/run_checks.sh
fi

name="autonom-${version}"
stage="dist/${name}"
mkdir -p "$stage"

# Curated runnable set — no tests, no caches, no VCS.
cp -R scripts "$stage/scripts"
cp -R plugins "$stage/plugins"
cp -R .agents "$stage/.agents"
cp -R .claude-plugin "$stage/.claude-plugin"
cp README.md CHANGELOG.md LICENSE "$stage/"
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

(cd dist && if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${name}.tgz" > SHA256SUMS
else
  shasum -a 256 "${name}.tgz" > SHA256SUMS
fi)

size="$(du -h "$tarball" | cut -f1)"
echo "built ${tarball} (${size}) + dist/SHA256SUMS"
echo
echo "hand off: copy ${tarball} to the target any way you like, then:"
echo "  tar xzf ${name}.tgz && ./${name}/install.sh   # checkbox picker (or --all / claude codex grok)"
