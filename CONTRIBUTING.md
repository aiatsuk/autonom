# Contributing

1. Keep skills focused and progressively load references or scripts only when
   the task needs them. Skill bodies must stay portable across Codex, Claude,
   Grok, and other `SKILL.md` consumers.
2. Avoid hard-coded claims that a toolchain version is current. Add official
   documentation links and inspect the active project instead.
3. Add tests for selector, parser, or bridge behavior before changing it.
4. Run `./scripts/run_checks.sh` before committing. CI (`checks` workflow) runs
   the same script on every PR and push to main across ubuntu and macOS —
   local green is still required, CI green is what merges.
5. Tests that mutate `os.environ` must use `tests/env_isolation.py`
   (`EnvSandboxMixin.set_env` / `sandbox_home`) so ambient values are restored;
   the env-hygiene guard fails the suite otherwise.
6. Keep bundled helpers dependency-light and safe for paths containing spaces.
7. Keep `LICENSE` accurate for this repository’s license terms.
8. When renaming packaging surfaces, update Codex marketplace metadata,
   `scripts/install_skills.sh`, docs, and tests together.
9. Releasing: bump the three version files in one commit
   (`scripts/autonom_lib/__init__.py`, both `plugins/autonom/.*-plugin/plugin.json`),
   add the `CHANGELOG.md` section, check version-anchored prose
   (e.g. `docs/CAPABILITIES.md`'s history notes), then tag `v<version>` and
   push the tag — the `release` workflow validates tag↔version agreement,
   re-runs checks, and publishes the tarball with `SHA256SUMS`.
   `make publish` only pushes a branch; it never releases.
