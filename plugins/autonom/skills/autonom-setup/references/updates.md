# Checking and installing Autonom updates

The plugin supplies skills; the separately installed `autonom` CLI supplies
the runtime. Check both. Run an update check once before a new Autonom task,
or on request. Defer installation during an active device session.

## Compare installed and upstream versions

Use the installed agent's native inventory and inspect its marketplace source:

```bash
codex plugin list --marketplace autonom --json
codex plugin marketplace list
claude plugin list --json
claude plugin marketplace list
autonom version
```

Run only the commands for agents present on the machine. If `claude` is absent
from `PATH`, check the native install at `~/.local/bin/claude`; do not silently
replace a plugin with loose skills. Record Autonom's installed version,
enabled state, scope, and source. The CLI may have a different version.

For the GitHub marketplace `aiatsuk/autonom` tracking `main`, check its current
plugin manifest with a bounded, read-only request:

```bash
curl --fail --location --silent --show-error --connect-timeout 10 --max-time 30 \
  https://raw.githubusercontent.com/aiatsuk/autonom/main/plugins/autonom/.codex-plugin/plugin.json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
```

Compare semantic versions numerically (`0.30.10` is newer than `0.30.9`). A
failed request means the check is inconclusive, not that the installed version
is current. For a pinned ref or a directory/bundle source, inspect that source
instead of silently switching it to `main`. The GitHub marketplace follows
the repository; a GitHub Release is not required for an update to be available.

## Refresh the plugin when needed

For an enabled Autonom install from an existing GitHub marketplace, use the
native commands below. They fetch the marketplace and refresh Autonom's
versioned cache; no manual registry or cache edits are needed. Leave disabled
installs disabled unless enabling them is part of the request.

Codex:

```bash
codex plugin marketplace upgrade autonom --json
codex plugin add autonom@autonom --json
codex plugin list --marketplace autonom --json
```

Claude Code (the example is a user-scoped install; keep the installed scope):

```bash
claude plugin marketplace update autonom
claude plugin update autonom@autonom --scope user --json
claude plugin list --json
```

For a fresh GitHub install, register `aiatsuk/autonom` first with
`codex plugin marketplace add aiatsuk/autonom --ref main --json` or
`claude plugin marketplace add aiatsuk/autonom --scope user`, then use
`codex plugin add autonom@autonom --json` or
`claude plugin install autonom@autonom --scope user --json`.

For a directory-sourced marketplace, refresh its maintained checkout or
replace the bundle at its existing source, then run the native plugin refresh
command. A clean checkout can be updated with `git fetch origin` followed by
`git merge --ff-only origin/<configured-branch>`; preserve local edits and
report divergence instead of resetting it. Do not remove and re-add a Claude
marketplace as a routine update: removal also uninstalls its plugins and can
delete saved options, secrets, and data.

## Refresh the CLI separately

For a released marketplace plugin, use the helper beside the loaded setup skill:

```bash
python3 <this-skill-directory>/scripts/install_runtime.py
```

It installs exactly the manifest version from GitHub Releases and validates the
checksum before running it. Use the returned absolute `executable` for `version`
and `doctor`, including from Desktop where PATH may differ from a terminal.
See the parent skill for custom paths, errors, and active-session deferral.
A published release is required for this helper, even when the marketplace
tracks `main`. If it is not published yet, keep the working runtime and report
the mismatch; do not quietly fall back to the latest available release.

If the user deliberately maintains a source checkout, preserve that workflow:
update its configured branch without discarding local edits, then run
`./scripts/install_cli.sh` there. Do not replace a development checkout with a
release runtime unless the user asks to switch. Plugin caches contain skills
and the setup helper; they do not contain the CLI itself.

## Verify and load the update

Read back the native plugin inventory: Autonom must have the expected version
and retain its previous enabled state and scope. Inspect the installed cache's
manifest and compare its skill files with the fetched source; record the source
revision when available. Version equality alone does not prove freshness when
upstream files changed without a bump. If files differ, refresh once and check
again; if they still differ, report the mismatch rather than claiming success.

For a CLI update, confirm `autonom version` and run `autonom doctor` to inspect
dependencies. Installation verification does not require booting a device.
Start a new Codex thread or Claude Code session to load the refreshed skills.
Report the installed plugin and CLI versions separately and any deferred or
failed update.
