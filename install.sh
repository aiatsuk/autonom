#!/usr/bin/env bash
# One-command Autonom install: the CLI always, everything else by choice.
#
#   ./install.sh                      interactive checkbox picker (on a TTY)
#   ./install.sh --all                device tools + Claude + Codex + Grok
#   ./install.sh --claude --codex     any subset, non-interactive
#   ./install.sh claude grok          the same selections, positional
#   ./install.sh --cli-only           just `autonom` on PATH
#   ./install.sh --link <dir>         + skills symlinked into any skill root
#   ./install.sh --copy <dir>         + skills copied (no live repo needed)
#
# From a git checkout everything registers in place, so an edit in the
# repository takes effect on the next invocation. From an extracted bundle the
# files are first copied to a stable home (AUTONOM_PREFIX, default
# ~/.local/share/autonom) and the extracted folder can be deleted afterwards.
#
# Never prompts when stdin or stdout is not a terminal: without flags it then
# installs the CLI only and prints what else is available.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sel_tools=-1
sel_claude=-1
sel_codex=-1
sel_grok=-1
explicit=0
generic_mode=""
generic_dir=""

usage() {
  cat <<'EOF'
Usage:
  ./install.sh                      interactive checkbox picker (on a TTY)
  ./install.sh --all                device tools + Claude + Codex + Grok
  ./install.sh --tools --claude --codex --grok   any subset, non-interactive
  ./install.sh claude codex grok tools           the same selections, positional
  ./install.sh --cli-only           just `autonom` on PATH
  ./install.sh --link <dir>         + skills symlinked into any skill root
  ./install.sh --copy <dir>         + skills copied (no live repo needed)
  ./install.sh --prefix <dir>       bundle home (default ~/.local/share/autonom)

The `autonom` CLI is always installed. Everything else is opt-in:
  tools    device tools bootstrap (adb, mitmproxy, idb) via brew/pipx/apt
  claude   Claude Code plugin autonom@autonom (falls back to ~/.claude/skills)
  codex    Codex plugin autonom@autonom
  grok     skills into ~/.grok/skills

Environment: AUTONOM_PREFIX, AUTONOM_BIN_DIR.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --all|all) sel_tools=1; sel_claude=1; sel_codex=1; sel_grok=1; explicit=1 ;;
    --tools|tools) sel_tools=1; explicit=1 ;;
    --claude|claude) sel_claude=1; explicit=1 ;;
    --codex|codex) sel_codex=1; explicit=1 ;;
    --grok|grok) sel_grok=1; explicit=1 ;;
    --cli-only|cli) explicit=1 ;;
    --link|--copy)
      generic_mode="$1"
      generic_dir="${2:?$1 requires a directory}"
      explicit=1
      shift
      ;;
    --prefix)
      AUTONOM_PREFIX="${2:?--prefix requires a directory}"
      export AUTONOM_PREFIX
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Interactive checkbox picker. Plain ANSI + `read -rsn1`, so it needs nothing
# beyond the bash already running it, and it is only ever offered on a TTY.
# ---------------------------------------------------------------------------
run_picker() {
  local labels=(
    "Device tools: adb, mitmproxy, idb (brew/pipx/apt)"
    "Claude Code plugin  (autonom@autonom)"
    "Codex plugin        (autonom@autonom)"
    "Grok skills         (~/.grok/skills)"
  )
  local state=("$sel_tools" "$sel_claude" "$sel_codex" "$sel_grok")
  local count=4
  local cur=0
  local drawn=0
  local key rest i mark ptr

  # shellcheck disable=SC2016  # the backticks are literal, not a substitution
  printf 'Autonom %s — the `autonom` CLI is installed always. Extras:\n' "$1"
  printf '  arrows/jk move · space toggles · a all · n none · enter installs · q quits\n\n'
  printf '\033[?25l'
  trap 'printf "\033[?25h"' EXIT

  while :; do
    if [ "$drawn" = 1 ]; then
      printf '\033[%dA' "$count"
    fi
    i=0
    while [ "$i" -lt "$count" ]; do
      mark=' '
      ptr='  '
      if [ "${state[i]}" = 1 ]; then mark='x'; fi
      if [ "$i" = "$cur" ]; then ptr='> '; fi
      printf '\r\033[2K %s[%s] %s\n' "$ptr" "$mark" "${labels[i]}"
      i=$((i + 1))
    done
    drawn=1

    IFS= read -rsn1 key || break
    case "$key" in
      $'\x1b')
        rest=''
        IFS= read -rsn2 -t 1 rest || true
        case "$rest" in
          '[A') cur=$(((cur + count - 1) % count)) ;;
          '[B') cur=$(((cur + 1) % count)) ;;
        esac
        ;;
      k) cur=$(((cur + count - 1) % count)) ;;
      j) cur=$(((cur + 1) % count)) ;;
      ' ')
        if [ "${state[cur]}" = 1 ]; then state[cur]=0; else state[cur]=1; fi
        ;;
      a) state=(1 1 1 1) ;;
      n) state=(0 0 0 0) ;;
      '') break ;;
      q)
        printf '\033[?25h\naborted — nothing installed\n'
        exit 130
        ;;
    esac
  done

  printf '\033[?25h\n'
  trap - EXIT
  sel_tools="${state[0]}"
  sel_claude="${state[1]}"
  sel_codex="${state[2]}"
  sel_grok="${state[3]}"
}

if [ "$explicit" = 0 ]; then
  if [ -t 0 ] && [ -t 1 ]; then
    # Preselect what this machine can plausibly use.
    sel_tools=1
    command -v claude >/dev/null 2>&1 && sel_claude=1 || sel_claude=0
    command -v codex >/dev/null 2>&1 && sel_codex=1 || sel_codex=0
    [ -d "$HOME/.grok" ] && sel_grok=1 || sel_grok=0
    if [ -d "$here/.git" ]; then ctx="(checkout)"; else ctx="(bundle)"; fi
    run_picker "$ctx"
  else
    echo "No terminal and no flags: installing the CLI only."
    echo "Extras: --tools --claude --codex --grok (or --all). See --help."
  fi
fi
if [ "$sel_tools" = -1 ]; then sel_tools=0; fi
if [ "$sel_claude" = -1 ]; then sel_claude=0; fi
if [ "$sel_codex" = -1 ]; then sel_codex=0; fi
if [ "$sel_grok" = -1 ]; then sel_grok=0; fi

# ---------------------------------------------------------------------------
# From a checkout, install in place. From an extracted bundle, copy to a
# stable home first — replacing a previous install rather than layering over
# it, and refusing to wipe a directory that is not an Autonom install.
# ---------------------------------------------------------------------------
src="$here"
if [ ! -d "$here/.git" ]; then
  prefix="${AUTONOM_PREFIX:-$HOME/.local/share/autonom}"
  if [ -e "$prefix" ] && [ "$prefix" != "$here" ]; then
    if [ -f "$prefix/scripts/autonom.py" ] || [ -z "$(ls -A "$prefix" 2>/dev/null)" ]; then
      rm -rf "$prefix"
    else
      echo "refusing to replace $prefix: not an Autonom install (set AUTONOM_PREFIX elsewhere)" >&2
      exit 1
    fi
  fi
  if [ "$prefix" != "$here" ]; then
    mkdir -p "$prefix"
    cp -R "$here/." "$prefix/"
  fi
  chmod +x "$prefix/scripts/autonom.py" 2>/dev/null || true
  src="$prefix"
fi

"$src/scripts/install_cli.sh"

if [ "$sel_tools" = 1 ]; then
  echo
  "$src/scripts/bootstrap.sh" --install
fi
if [ "$sel_claude" = 1 ]; then
  echo
  "$src/scripts/install_claude.sh"
fi
if [ "$sel_codex" = 1 ]; then
  echo
  "$src/scripts/install_codex.sh"
fi
if [ "$sel_grok" = 1 ]; then
  echo
  "$src/scripts/install_skills.sh" grok
fi
if [ -n "$generic_mode" ]; then
  echo
  "$src/scripts/install_skills.sh" "$generic_mode" "$generic_dir"
fi

# ---------------------------------------------------------------------------
# Verify honestly: a broken CLI, a working CLI missing device tools, and a
# fully equipped machine are three different outcomes.
# ---------------------------------------------------------------------------
echo
if command -v autonom >/dev/null 2>&1; then
  if ! autonom doctor >/dev/null 2>&1; then
    echo "verify: 'autonom doctor' failed — run it directly to see why"
  elif autonom doctor --strict >/dev/null 2>&1; then
    echo "verify: autonom ok"
  else
    echo "verify: autonom installed; 'autonom doctor' reports missing device tools"
    echo "        run '$src/scripts/bootstrap.sh --install' to fetch the mechanical ones"
  fi
else
  echo "verify: installed under $src but not on PATH — add ~/.local/bin to PATH"
fi
