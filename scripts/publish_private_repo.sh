#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="${1:-aiatsuk/autonom}"
cd "$root"

command -v gh >/dev/null 2>&1 || {
  echo "GitHub CLI is required. Install gh and run gh auth login first." >&2
  exit 1
}
gh auth status >/dev/null

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "This directory is not an initialized git repository." >&2
  exit 1
}
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Refusing to publish a dirty worktree. Review and commit changes first." >&2
  exit 1
fi

if visibility="$(gh repo view "$repo" --json visibility --jq .visibility 2>/dev/null)"; then
  [[ "$visibility" == "PRIVATE" ]] || {
    echo "Repository $repo already exists but is not private; refusing to push." >&2
    exit 1
  }
  remote_url="$(gh repo view "$repo" --json sshUrl --jq .sshUrl)"
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$remote_url"
  else
    git remote add origin "$remote_url"
  fi
  git push -u origin "$(git branch --show-current)"
else
  gh repo create "$repo" \
    --private \
    --description "Universal test and debug harness for AI agents (Codex, Claude, Grok)" \
    --source=. \
    --remote=origin \
    --push
fi

gh repo view "$repo" --json nameWithOwner,visibility,url
