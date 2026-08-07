# Contributing

1. Keep skills focused and progressively load references or scripts only when
   the task needs them. Skill bodies must stay portable across Codex, Claude,
   Grok, and other `SKILL.md` consumers.
2. Avoid hard-coded claims that a toolchain version is current. Add official
   documentation links and inspect the active project instead.
3. Add tests for selector, parser, or bridge behavior before changing it.
4. Run `./scripts/run_checks.sh` before committing.
5. Keep bundled helpers dependency-light and safe for paths containing spaces.
6. Keep `LICENSE` accurate for this repository’s license terms.
7. When renaming packaging surfaces, update Codex marketplace metadata,
   `scripts/install_skills.sh`, docs, and tests together.
