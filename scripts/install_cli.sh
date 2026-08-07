#!/usr/bin/env bash
# Put `autonom` on PATH.
#
# The CLI is a single stdlib-only Python script, so this is a symlink and not a
# package install: no virtualenv, no pip, no build step, and an edit in the
# repository takes effect on the next invocation. `autonom.py` resolves its own
# library directory through `Path(__file__).resolve()`, so the link works from
# anywhere.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_cli="$root/scripts/autonom.py"
bindir="${AUTONOM_BIN_DIR:-$HOME/.local/bin}"
action="install"
mode="link"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install_cli.sh              # symlink into ~/.local/bin
  ./scripts/install_cli.sh --copy       # copy a launcher instead of linking
  ./scripts/install_cli.sh --bin-dir <dir>
  ./scripts/install_cli.sh uninstall

Environment:
  AUTONOM_BIN_DIR   target directory (default: ~/.local/bin)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    uninstall) action="uninstall" ;;
    --copy) mode="copy" ;;
    --link) mode="link" ;;
    --bin-dir) shift; bindir="${1:?--bin-dir needs a directory}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

target="$bindir/autonom"

if [ "$action" = "uninstall" ]; then
  if [ -e "$target" ] || [ -L "$target" ]; then
    rm -f "$target"
    echo "removed $target"
  else
    echo "nothing to remove at $target"
  fi
  exit 0
fi

[ -f "$source_cli" ] || { echo "not found: $source_cli" >&2; exit 1; }
mkdir -p "$bindir"
chmod +x "$source_cli"

# Refuse to clobber an unrelated file. A stale symlink of our own is fine to
# replace; someone else's `autonom` is not ours to overwrite.
if [ -e "$target" ] && [ ! -L "$target" ]; then
  if ! head -n 5 "$target" 2>/dev/null | grep -q "autonom"; then
    echo "refusing to overwrite $target (not an Autonom launcher)" >&2
    exit 1
  fi
fi

if [ "$mode" = "link" ]; then
  ln -sfn "$source_cli" "$target"
else
  cat > "$target" <<EOF
#!/usr/bin/env bash
# autonom launcher (copy mode) -> $source_cli
exec python3 "$source_cli" "\$@"
EOF
fi
chmod +x "$target"

echo "installed $target -> $source_cli"

case ":$PATH:" in
  *":$bindir:"*) ;;
  *)
    echo
    echo "WARNING: $bindir is not on your PATH, so 'autonom' will not be found." >&2
    echo "Add this to your shell profile, then open a new shell:" >&2
    echo "  export PATH=\"$bindir:\$PATH\"" >&2
    ;;
esac
