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
