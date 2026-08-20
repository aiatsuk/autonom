# Autonom — agent instructions

Autonom is a **universal mobile test and debug harness** for AI coding agents.

## How to use this repository

1. Prefer the smallest relevant skill after routing (`project-router`).
2. For device work, use the CLI control plane — installed as `autonom` (from a
   checkout without installing, `python3 <autonom-root>/scripts/autonom.py …`).
   Android and iOS share the verbs:

   ```bash
   autonom doctor
   autonom devices
   autonom devices boot --avd <name>
   autonom session start --serial <id>
   autonom session start --platform ios --target <UDID>
   autonom ui tree
   autonom screenshot
   ```

3. Pick one explicit target when runtime work is involved. Ambiguity is an
   error listing the candidates, never a silent guess.
4. Climb the evidence ladder: code → unit/widget test → integration →
   screenshot/UI tree/logs → profile/memory/network → before/after replay.
5. Prefer accessibility/UI tree selectors over raw coordinates.
6. Separate measured facts from code-backed findings, hypotheses, and open
   uncertainty.
7. Never print secrets, keystores, tokens, or `.env` values.
8. On iOS the visible label is in `desc` (from `AXLabel`), not `text` — select
   with `--desc` or `--resource-id`.
9. An exit code of 0 does not prove a tap changed anything. Compare before/after
   screenshots or trees.

## Skill pack location

Portable skills live in:

```text
plugins/autonom/skills/<skill-name>/SKILL.md
```

Helpers and validation live in `scripts/` and `tests/`.

## Agent compatibility

| Agent | Install path |
| --- | --- |
| Codex | marketplace plugin `autonom@autonom` (`./scripts/install_codex.sh`) |
| Claude Code | marketplace plugin `autonom@autonom` (`./scripts/install_claude.sh`); fallback `./scripts/install_skills.sh claude` |
| Grok | `./scripts/install_skills.sh grok` → `~/.grok/skills/autonom-*` |
| Local/dev | `./scripts/install_skills.sh --link <target-dir>` |

Full docs: `README.md`, `docs/ARCHITECTURE.md`, `docs/USAGE.md`, `docs/INSTALL.md`.

## Cursor Cloud specific instructions

- This repo is **stdlib-only Python 3 + Node.js** with **no package manager** (no
 `pip`, `pyproject.toml`, `requirements.txt`, `package.json`, or lockfiles).
 There is nothing to `pip install`/`npm install`. The startup update script only
 runs `./scripts/install_cli.sh` to symlink `autonom` onto `PATH`.
- The `autonom` CLI is a **symlink** to `scripts/autonom.py`, so repository edits
 take effect on the next invocation — no reinstall needed after changing CLI code.
- `~/.local/bin` is on `PATH` in login shells (via `~/.profile`). In a fresh
 non-login shell where `autonom` is not found, either add that directory to
 `PATH` (`export PATH="$HOME/.local/bin:$PATH"`) or run
 `python3 scripts/autonom.py …` directly.
- **Validation:** `make check` (or `./scripts/run_checks.sh`) runs plugin
 validation, `compileall`, `node --check`, the full Python `unittest` suite
 (~245 tests), Node `--test` suites, and `bash -n`. It takes ~1.5–3 min.
 `shellcheck` is optional; if absent, that lint step is skipped (still passes).
- **No device in this Linux VM:** Android (`adb`/emulator), iOS
 (`xcrun simctl` + `idb`), and `mitmproxy` are not installed, so `autonom doctor`
 reports them missing and device E2E verbs cannot run. Exercise the core
 UI-tree/selector engine offline against a dump file, e.g.
 `autonom ui tree --dump tests/fixtures/ui_dump.xml` and
 `autonom ui find --dump tests/fixtures/ui_dump.xml --desc "Flutter Save Button" --mode exact`.
