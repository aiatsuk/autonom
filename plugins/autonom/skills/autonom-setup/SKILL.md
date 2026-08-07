---
name: autonom-setup
description: Install or hand off the Autonom harness — the CLI plus all skills — onto a new machine or into another agent. Build a single transferable bundle, copy it, install with one command, verify, and register it with Claude, Grok, or Codex. Triggers when setting up Autonom somewhere new, packaging it for handoff, or asking how to get it onto another device or agent.
---

# Autonom Setup

Autonom is a plugin: 23 skills plus a dependency-free CLI they drive. This skill
packages the whole thing into one file you can carry to another machine and
install with one command — the skills travel with the CLI and land in whatever
agent is there.

Not a compiled binary, on purpose: the CLI is stdlib-only Python (nothing to
compile), and the network addon must be a real file on disk because `mitmdump`
loads it by path. So the transferable artifact is a **tarball** — one file to
copy, one command to install.

## Build the bundle

```bash
./scripts/build_release.sh
# -> dist/autonom-<version>.tgz  (~200 KB: CLI + library + all skills + manifest)
```

The bundle carries its own `install.sh`, so the target needs nothing but
`python3` (already present on macOS and Linux) and, for real device work, the
device tools (`adb`, `xcrun`, `idb`, `mitmdump`) that `doctor` checks.

## Install on another machine

Copy `dist/autonom-<version>.tgz` over any transport, then:

```bash
tar xzf autonom-<version>.tgz
./autonom-<version>/install.sh          # checkbox picker: tools, Claude, Codex, Grok
./autonom-<version>/install.sh --all    # headless: everything
./autonom-<version>/install.sh claude   # or any explicit subset
```

`install.sh` copies the bundle to a stable home (`AUTONOM_PREFIX`, default
`~/.local/share/autonom`) so the extracted folder can be deleted afterwards,
puts `autonom` on `PATH`, installs the selected device tools and agents, and
verifies with `autonom doctor`. Headless runs never prompt — no flags means
CLI only. Overrides: `AUTONOM_PREFIX`, `AUTONOM_BIN_DIR`.

## Into each agent

`install.sh <target>`, or run the layer directly:

```bash
./scripts/install_claude.sh              # Claude Code plugin (marketplace + install)
./scripts/install_codex.sh               # Codex plugin (marketplace + add)
./scripts/install_skills.sh grok         # ~/.grok/skills
./scripts/install_skills.sh --link <dir> # any skill-root, by symlink
./scripts/install_skills.sh --copy <dir> # by copy (no live repo)
```

Claude and Codex install Autonom as a **plugin** from the bundle's marketplace
manifests and copy it into a versioned cache. After the source changes, re-run
the script — or `claude plugin update autonom@autonom` /
`codex plugin add autonom@autonom` — so the cache catches up; the version is
bumped on every release exactly so those caches notice. Skills load namespaced
(`autonom:mobile-network`). Without the `claude` CLI the Claude path falls back
to loose `autonom-*` skills in `~/.claude/skills`.

## Verify it actually works

`doctor` reports what is present; a green install still means *installed*, not
*proven*. Confirm the chain end to end on a connected device:

```bash
autonom doctor                     # tools + capabilities + session + orphans
autonom devices                    # a real device/simulator is listed
autonom session start --platform android --target <id>
autonom ui tree --max-nodes 5      # a real tree comes back
autonom session stop
```

If any step fails, `doctor`'s per-tool `state` and install hint name what is
missing — do not proceed as if it worked.

## Knowledge stays behind

The bundle ships the **skills**, not the per-app knowledge under
`~/.autonom/apps/` (see `mobile-memory`). That store is machine-local and
per-person; it accumulates on each machine as work happens and is not part of
the handoff.

## Related

- `mobile-session`, `mobile-screen`, `mobile-network` — the verbs the CLI exposes.
- `mobile-memory` — per-app knowledge, kept out of the bundle on purpose.
- `toolchain-doctor` — reading `autonom doctor` output.
