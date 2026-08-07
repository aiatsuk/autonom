#!/usr/bin/env bash
# Install Autonom portable skills into an agent skill root.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
skills_src="$root/plugins/autonom/skills"
prefix="autonom-"
mode="link"
target=""
action="install"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install_skills.sh claude
  ./scripts/install_skills.sh grok
  ./scripts/install_skills.sh --link <skill-root>
  ./scripts/install_skills.sh --copy <skill-root>
  ./scripts/install_skills.sh uninstall claude|grok
  ./scripts/install_skills.sh uninstall --link|--copy <skill-root>

Options:
  --prefix <text>   Directory name prefix (default: autonom-)
  --prefix ""       Install skill directory names unchanged
  -h, --help        Show this help
EOF
}

resolve_preset() {
  case "$1" in
    claude) printf '%s\n' "${HOME}/.claude/skills" ;;
    grok) printf '%s\n' "${HOME}/.grok/skills" ;;
    *)
      echo "Unknown preset: $1 (use claude, grok, or --link/--copy <dir>)" >&2
      exit 2
      ;;
  esac
}

while (($#)); do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    uninstall)
      action="uninstall"
      shift
      ;;
    --link)
      mode="link"
      target="${2:-}"
      [[ -n "$target" ]] || { echo "--link requires a directory" >&2; exit 2; }
      shift 2
      ;;
    --copy)
      mode="copy"
      target="${2:-}"
      [[ -n "$target" ]] || { echo "--copy requires a directory" >&2; exit 2; }
      shift 2
      ;;
    --prefix)
      prefix="${2-}"
      shift 2
      ;;
    claude|grok)
      target="$(resolve_preset "$1")"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$target" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -d "$skills_src" ]]; then
  echo "Skills source missing: $skills_src" >&2
  exit 1
fi

mkdir -p "$target"

# Replace, don't accumulate: drop every previously installed "${prefix}*" entry
# first, so a skill renamed or removed from the package does not survive as a stale
# copy. Without a prefix ours cannot be told apart, so the sweep is skipped.
if [[ "$action" == "install" && -n "$prefix" ]]; then
  for stale in "$target/${prefix}"*; do
    [[ -e "$stale" || -L "$stale" ]] || continue
    rm -rf "$stale"
  done
fi

installed=0
for skill_dir in "$skills_src"/*/; do
  [[ -d "$skill_dir" ]] || continue
  name="$(basename "$skill_dir")"
  dest="$target/${prefix}${name}"

  if [[ "$action" == "uninstall" ]]; then
    if [[ -e "$dest" || -L "$dest" ]]; then
      rm -rf "$dest"
      echo "removed $dest"
      installed=$((installed + 1))
    fi
    continue
  fi

  rm -rf "$dest"
  if [[ "$mode" == "link" ]]; then
    ln -s "$skill_dir" "$dest"
    echo "linked  $dest -> $skill_dir"
  else
    cp -R "$skill_dir" "$dest"
    echo "copied  $dest"
  fi
  installed=$((installed + 1))
done

if [[ "$action" == "uninstall" ]]; then
  echo "Uninstalled $installed Autonom skill entries from $target"
else
  echo "Installed $installed Autonom skills into $target (mode=$mode, prefix=${prefix:-<none>})"
fi
