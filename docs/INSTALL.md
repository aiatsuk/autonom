# Install Autonom on any agent

Autonom ships as portable `SKILL.md` packages plus Claude Code and Codex
marketplace metadata. The same skill bodies work across agents that load skill
directories.

## One-command install

```bash
./install.sh          # interactive checkboxes: device tools, Claude, Codex, Grok
./install.sh --all    # headless: everything
./install.sh --tools --claude   # any subset; positional `claude codex grok` works too
```

The `autonom` CLI is always installed; everything else is opt-in. On a TTY the
picker preselects what the machine can plausibly use (detected `claude`/`codex`
CLIs, an existing `~/.grok`). Without a terminal it never prompts: no flags
means CLI only, plus a note about the available flags. From a git checkout it
installs in place; from an extracted bundle it first copies itself to
`AUTONOM_PREFIX` (default `~/.local/share/autonom`), after which the extracted
folder can be deleted. The sections below run the same layers one at a time.

## Prerequisites

- `git`, `python3`, `node` (for the emulator browser bridge and checks)
- Android SDK / Flutter only when you run those domain skills against a target

Nothing below is needed to install the skills or run `./scripts/run_checks.sh`.
Each tool is discovered at runtime and unlocks one capability; `autonom doctor`
reports exactly which are present and prints the install command for the rest.

### Android targets

Android platform-tools on `PATH`, or pass `--adb /path/to/adb`
(or set `AUTONOM_ADB`).

### iOS Simulator targets (macOS only)

Session, screenshot, logs, deep links, permissions, location, media, and recording
need only Xcode:

```bash
xcode-select -p
xcrun simctl list devices available
```

The accessibility tree (`ui tree`, `ui find`, `ui tap`, gestures) additionally
needs the iOS Development Bridge:

```bash
brew tap facebook/fb
brew trust --formula facebook/fb/idb-companion   # Homebrew refuses untrusted taps
brew install idb-companion

pipx install fb-idb
idb list-targets
```

**Two traps worth knowing, because neither error message explains itself:**

1. `fb-idb` calls `asyncio.get_event_loop()`, which became a hard error in Python
   3.14. If `pipx` picked 3.14, every `idb` invocation dies with
   `RuntimeError: There is no current event loop`. Pin an older interpreter:

   ```bash
   pipx reinstall fb-idb --python /opt/homebrew/opt/python@3.12/bin/python3.12
   ```

2. `brew install idb-companion` fails with *"Refusing to load formula from
   untrusted tap"* until the `brew trust` line above is run. Prefer the
   `--formula` form so the grant covers one formula rather than the whole tap.

`idb_companion` prints an objc class-collision warning on stderr (it loads Apple
private frameworks). It is cosmetic.

#### Remote iOS targets

The idb client can drive a companion on another Mac, so a Linux orchestrator can
use a Mac simulator farm:

```bash
export AUTONOM_IDB_COMPANION=mac-farm-01:10882
```

### Verify what you have

```bash
python3 scripts/autonom.py doctor
```

## Put `autonom` on PATH

Every example can be run as `python3 <autonom-root>/scripts/autonom.py …`, which
needs no installation at all. To get the shorter `autonom …` form:

```bash
./scripts/install_cli.sh                    # symlink into ~/.local/bin
./scripts/install_cli.sh --bin-dir /usr/local/bin
./scripts/install_cli.sh --copy             # a launcher script instead of a symlink
./scripts/install_cli.sh uninstall
```

A symlink rather than a package install: the CLI is one stdlib-only script, so
there is no virtualenv, no pip, and no build step — and an edit in the
repository takes effect on the next invocation. The script resolves its own
library directory, so the link works from any directory.

If `~/.local/bin` is not on your `PATH` the installer says so and prints the
line to add. Verify with:

```bash
autonom version
autonom doctor
```

## Codex

```bash
codex plugin marketplace add aiatsuk/autonom
codex plugin add autonom@autonom
codex plugin list
```

Local checkout:

```bash
codex plugin marketplace add /absolute/path/to/autonom
codex plugin add autonom@autonom
```

Start a new Codex thread after install so the skill index reloads.

## Claude Code

Install as a plugin — skills load namespaced as `autonom:<name>`:

```bash
claude plugin marketplace add aiatsuk/autonom   # or /absolute/path/to/checkout
claude plugin install autonom@autonom
```

From a checkout or extracted bundle, `./scripts/install_claude.sh` runs the
same two commands and is safe to re-run.

Installed plugins are copied into a versioned cache, not referenced live.
After the source changes, run `claude plugin update autonom@autonom` (or
re-run the script); releases bump the version precisely so the cache notices.

Start a new Claude session after install so the plugin loads.

### Fallback: loose skills

Without the `claude` CLI (`install_claude.sh` falls back to this on its own),
the same skill bodies install as plain directories:

```bash
./scripts/install_skills.sh claude
# or explicit:
./scripts/install_skills.sh --link ~/.claude/skills
```

Claude then discovers each skill as `autonom-<name>` (directory-name prefix,
collision-safe). Reinstalling sweeps previous `autonom-*` entries first, so a
skill renamed or removed from the package does not linger as a stale copy.

## Grok

```bash
./scripts/install_skills.sh grok
# or:
./scripts/install_skills.sh --link ~/.grok/skills
```

## Generic / other agents

Any agent that loads directories of the form:

```text
<skill-root>/<skill-name>/SKILL.md
```

can consume Autonom:

```bash
./scripts/install_skills.sh --link /path/to/agent/skills
./scripts/install_skills.sh --copy /path/to/agent/skills   # offline copy instead of symlinks
```

Optional prefix (default `autonom-`):

```bash
./scripts/install_skills.sh --link ~/.my-agent/skills --prefix ""
```

## Verify

```bash
./scripts/run_checks.sh
```

Checks do not need Flutter, the Android SDK, Xcode, idb, or mitmproxy. The suite
includes a sweep that runs every CLI verb with an empty `PATH` and asserts each
one fails with a single machine-readable `error_code` instead of a traceback, so
"green on a bare machine" is an actual check rather than an assumption.

## Uninstall

Claude Code plugin:

```bash
claude plugin uninstall autonom@autonom
claude plugin marketplace remove autonom
```

Loose skills:

```bash
./scripts/install_skills.sh uninstall claude
./scripts/install_skills.sh uninstall grok
./scripts/install_skills.sh uninstall --link /path/to/agent/skills
```

Codex: remove the marketplace plugin with the normal Codex plugin commands.
