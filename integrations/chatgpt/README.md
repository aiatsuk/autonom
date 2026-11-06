# Autonom probe for ChatGPT desktop (development only)

`autonom-chatgpt-probe` is a disposable development plugin. It measures what ChatGPT desktop (Work
mode) lets a local Autonom integration do: MCP tool calls, the MCP Apps bridge, model context, loopback
`fetch` and WebSocket from the app page, WebCodecs H.264 decoding and reconnects. It is **not** part of
the Autonom plugin, it is not listed in either root marketplace and it is not in the release bundle
(`scripts/build_release.sh` does not copy `integrations/`).

Layout:

| Path | What it is |
|---|---|
| `autonom-chatgpt-probe/` | The plugin: manifest, `.mcp.json`, `bin/launch.sh`, the stdio MCP server (`server/`), the probe page (`ui/`) and the loopback media endpoint (`media/`) |
| `.agents/plugins/marketplace.json` | A repo-local `autonom-dev` marketplace for `codex plugin marketplace add integrations/chatgpt` |
| `scripts/install_probe.py` | Install, uninstall and status for the personal marketplace |

## Requirements

- macOS with ChatGPT desktop, Work mode enabled for your account.
- Python 3: the server runs on Python 3.9 or newer (macOS's `/usr/bin/python3` is enough); the
  Autonom CLI and `install_probe.py` need Python 3.11 or newer.
- Node 22 or newer for the media endpoint.
- The Autonom CLI (`./scripts/install_cli.sh` in a checkout), so the probe can run `autonom version`
  and `autonom devices list`. The probe never starts an Autonom session.

## Install, status, uninstall

Run these from the repository root. `--json` prints one JSON object; `--dry-run` checks everything
and prints the planned actions without writing anything.

```sh
python3 integrations/chatgpt/scripts/install_probe.py status --json
python3 integrations/chatgpt/scripts/install_probe.py install --dry-run --json
python3 integrations/chatgpt/scripts/install_probe.py install --json \
  --allow-device android:emulator-5584 \
  --remote-route forwarded=http://127.0.0.1:47812
python3 integrations/chatgpt/scripts/install_probe.py uninstall --json [--purge-cache]
```

What `install` does:

1. Copies the plugin (a copy, not a symlink) to `~/plugins/autonom-chatgpt-probe` and writes the stamp
   `.autonom-probe-install.json` (version, source commit, file hashes, what the first install created).
2. Backs up `~/.agents/plugins/marketplace.json` byte for byte to
   `~/plugins/backups/autonom-chatgpt-probe/marketplace-<UTC time>.json`, then adds or replaces one entry
   `autonom-chatgpt-probe` (`./plugins/autonom-chatgpt-probe`). Other entries and keys keep their order
   and values. A missing marketplace is created as `personal`.
3. Writes the probe config `<state>/chatgpt-probe/config.json`, where `<state>` is `$AUTONOM_HOME`, else
   `$XDG_STATE_HOME/autonom`, else `~/.local/state/autonom`. Keys named by flags are replaced; other keys
   are kept. ChatGPT starts the server without `AUTONOM_HOME` or `XDG_STATE_HOME`, so run the script
   without them (`env -u AUTONOM_HOME -u XDG_STATE_HOME python3 ...`) when you install for real.

Re-running `install` is safe: one entry, the copy refreshed only when the source changed, and no
marketplace write (and no new backup) when the entry is already identical. Then restart ChatGPT
desktop, open Plugins, choose the Personal marketplace, install *Autonom probe* and enable it in Work
mode.

`uninstall` removes the entry (after a backup), deletes the marketplace file only when install created
it and no plugins remain, removes the copy only when its stamp is the probe's, and removes the config
only when install created it. `--purge-cache` also removes
`~/.codex/plugins/cache/<marketplace>/autonom-chatgpt-probe/` (any version folder, such as `local` or
`0.1.0`), and only when every plugin manifest inside it names the probe.

Refusals print `{"ok": false, "error_code", "error", "hint"}` and exit 1; bad flags exit 2.

| error_code | Meaning |
|---|---|
| `marketplace_invalid` | The marketplace is not a JSON object with a `plugins` list; nothing is written |
| `entry_conflict` | Another entry is named `autonom-chatgpt-probe` but points elsewhere |
| `target_conflict` | `~/plugins/autonom-chatgpt-probe` exists and was not installed by the script |
| `config_invalid` | The probe config file is not a JSON object |
| `cache_conflict` | The cache folder holds something that is not the probe |
| `device_not_allowed` | `--allow-device android:emulator-5554` (never allowed) |
| `invalid_arguments` | A flag value is out of range or malformed (exit 2) |
| `home_invalid`, `source_invalid`, `write_failed` | Bad `--home`, missing plugin tree, or a failed write |

The script never writes under `~/.codex` except `--purge-cache`, and it never edits
`~/.codex/config.toml`.

## Config keys

All keys are optional; flags of `install` write the first four.

| Key | Flag | Default |
|---|---|---|
| `media_port` | `--media-port N` (1024-65535) | `47811` |
| `remote_routes` | `--remote-route LABEL=ORIGIN` (origin `http://127.0.0.1:N` or `https://host[:port]`, at most 4) | none |
| `allow_origins` | `--allow-origin ORIGIN` (exact `scheme://host[:port]`, or one-label `https://*.suffix`) | `https://web-sandbox.oaiusercontent.com` is always allowed |
| `allowed_devices` | `--allow-device android:<serial>` or `ios:<udid>` | none |
| `autonom_cli`, `cli_python`, `node`, `autonom_root`, `adb` | edit the file (absolute paths) | found automatically |
| `cli_timeout_s` | edit the file (1-120) | `15` |

A bad value never crashes the server: it is reported as `config_error` by `probe_read`.

The media endpoint has 5 s to print its ready line. On a slow machine, such as a CI
runner, the environment variable `AUTONOM_PROBE_MEDIA_READY_TIMEOUT` (seconds, up to 600)
raises that limit.

## Security model

- The media endpoint listens on `127.0.0.1` only and checks the `Host` header (DNS rebinding).
- A new random token is made every time the media endpoint starts. It reaches the page only through
  the tool result's `_meta` (component-only), never through `structuredContent`, `content`, the HTML or
  the model context, and it is never printed or logged. The only copy on disk is the `0600` file
  `<state>/chatgpt-probe/media.json`, used to share one media endpoint between server processes.
- Page origins must match the allowlist exactly (scheme, host and port), or a configured one-label
  `https://*.suffix` wildcard. `null`, `file://` and malformed origins never match.
- Refused origins are recorded (at most 20, with counts and times) so you can see what the sandbox
  origin really is.
- Only devices listed in `allowed_devices` can be relayed; `android:emulator-5554` is always refused.

## Learning the sandbox origin

When the media checks fail with `origin_not_allowed`, ask the chat to call `probe_read` and look at
`media.refused_origins`. Allow the observed exact origin and restart ChatGPT:

```sh
python3 integrations/chatgpt/scripts/install_probe.py install --json \
  --allow-origin https://<observed host> ...same flags as before...
```

Allow exact origins only. Never allow `null` or a broad wildcard.

## Forwarded route

To test a second, declared origin for the media endpoint, forward a port with SSH and declare it as a
remote route:

```sh
ssh -N -L 47812:127.0.0.1:47811 <host>
python3 integrations/chatgpt/scripts/install_probe.py install --json \
  --remote-route forwarded=http://127.0.0.1:47812
```

Then pick the route *forwarded* in the probe page.

## Fake host harness

`tests/live/chatgpt_probe_host_live.mjs` runs the real server and page in headless Chromium and WebKit
behind a fake MCP Apps host (Playwright from `PLAYWRIGHT_ROOT`). It is not part of `node --test`:

```sh
node tests/live/chatgpt_probe_host_live.mjs --dry-run
node tests/live/chatgpt_probe_host_live.mjs --evidence-dir /tmp/probe-evidence --browser both --remote-proxy
python3 integrations/chatgpt/scripts/probe_results.py check --results /tmp/probe-evidence/results-chromium.json --profile harness
```

The unit tests for every part run in `scripts/run_checks.sh` without devices.

## Manual cleanup

`uninstall` cannot reach everything ChatGPT keeps:

1. In ChatGPT desktop, open Plugins and uninstall *Autonom probe*.
2. If `~/.codex/config.toml` still holds an `autonom-chatgpt-probe` entry, remove it by hand (the
   script never edits that file).
3. Run `install_probe.py uninstall --purge-cache` to remove the cached copy, then
   `install_probe.py status --json` to confirm nothing is left.
4. Notes and saved results stay in `<state>/chatgpt-probe/`; delete that folder if you no longer need them.

## Autonom MCP adapter (development)

`autonom-chatgpt` (display name *Autonom (dev)*) is the development adapter that connects ChatGPT
desktop to the local Autonom CLI and Canvas through a real MCP server. Like the probe it is a dev
plugin: it is not in either root marketplace, not in the release bundle and not in the repo-local
`autonom-dev` marketplace. Its server (`autonom-chatgpt/server/`) runs on Python 3.9 or newer with the
standard library only; the Autonom CLI it calls needs Python 3.11 or newer.

### Install, status, uninstall

Run these from the repository root with Python 3.11 or newer. `--json` prints one JSON object;
`--dry-run` checks everything and prints the planned actions without writing anything.

```sh
python3 integrations/chatgpt/scripts/install_workspace.py status --json
python3 integrations/chatgpt/scripts/install_workspace.py install --dry-run --json
env -u AUTONOM_HOME -u XDG_STATE_HOME python3 integrations/chatgpt/scripts/install_workspace.py install --json \
  --allow-target android:emulator-5584 \
  --allow-target ios:<udid> \
  --flow-root /absolute/path/to/flows \
  --autonom-cli "$PWD/scripts/autonom.py"
python3 integrations/chatgpt/scripts/install_workspace.py uninstall --json [--purge-cache]
```

What `install` does:

1. Copies the plugin (a copy, not a symlink) to `~/plugins/autonom-chatgpt` and writes the stamp
   `.autonom-chatgpt-install.json` (version, source commit, file hashes, what the first install created).
2. Backs up `~/.agents/plugins/marketplace.json` byte for byte to
   `~/plugins/backups/autonom-chatgpt/marketplace-<UTC time>.json`, then adds or replaces one entry
   `autonom-chatgpt` (`./plugins/autonom-chatgpt`). Other entries, the probe entry included, keep their
   order and values, so both dev plugins can be installed together. A missing marketplace is created as
   `personal`.
3. Writes the adapter config `<state>/chatgpt-mcp/config.json` (folder `0700`, file `0600`), where
   `<state>` is `$AUTONOM_HOME`, else `$XDG_STATE_HOME/autonom`, else `~/.local/state/autonom`. Keys named
   by flags are replaced; other keys are kept. ChatGPT starts the server without `AUTONOM_HOME` or
   `XDG_STATE_HOME`, so install for real without them, as above.

Re-running `install` is safe: one entry, the copy refreshed only when the source changed, and no
marketplace write (and no new backup) when the entry is already identical. Then restart ChatGPT
desktop, open Plugins, choose the Personal marketplace, install *Autonom (dev)* and enable it in Work
mode. Take every Plugins-screen action only on the item whose visible name reads exactly
*Autonom (dev)*.

`--autonom-cli FILE` pins the CLI the adapter runs (an absolute path of an existing
`scripts/autonom.py`, written as `autonom_cli`). Use it when `autonom` on your `PATH` points at a
checkout that does not have `autonom host info` yet, for example to test a worktree.

`status` reports the marketplace entry, the copy and its stamp, the config, the backups, the adapter's
own cache folder and a read-only ChatGPT inventory `chatgpt: {config_plugins, cache_plugins}`: the sorted
`<plugin>@<marketplace>` names of the `[plugins."<plugin>@<marketplace>"]` table headers in
`~/.codex/config.toml` (only the header lines are read into the output) and of the folders
`~/.codex/plugins/cache/<marketplace>/<plugin>/`. A missing file or folder gives an empty list. Compare
the inventory before and after an install or uninstall to see that no other ChatGPT plugin changed.

`uninstall` removes the entry (after a backup), deletes the marketplace file only when install created
it and no plugins remain, removes the copy only when its stamp is the adapter's, and removes the config
only when install created it. `--purge-cache` also removes
`~/.codex/plugins/cache/<marketplace>/autonom-chatgpt/`, and only when every plugin manifest inside it
names `autonom-chatgpt`.

Refusals print `{"ok": false, "error_code", "error", "hint"}` and exit 1; bad flags exit 2. The codes are
the probe installer's: `marketplace_invalid`, `entry_conflict`, `target_conflict`, `config_invalid`,
`cache_conflict`, `invalid_arguments` (exit 2), `home_invalid`, `source_invalid` and `write_failed`. The
script never writes under `~/.codex` except `--purge-cache`, and it never edits `~/.codex/config.toml`.

### Adapter config keys

All keys are optional. A bad value never crashes the server: that key keeps its default and
`list_hosts` reports a `config_error` warning that names the key.

| Key | Flag | Default |
|---|---|---|
| `flow_roots` | `--flow-root DIR` (absolute, existing, at most 8) | none (`list_flows` is empty) |
| `allowed_targets` | `--allow-target android:<id>` or `ios:<udid>` (at most 32) | empty: any target |
| `canvas_workspace` | `--canvas-workspace NAME` (`[A-Za-z0-9._-]`, 1-40) | `chatgpt` |
| `control_idle_s` | `--control-idle N` (15-3600) | `120` |
| `autonom_cli` | `--autonom-cli FILE` | found automatically (`AUTONOM_CLI`, else `autonom` on the `PATH`) |
| `cli_python`, `autonom_root` | edit the file (absolute paths) | found automatically |
| `agent_lease_ttl_s`, `max_streams`, `max_runs`, `run_max_s` | edit the file | `120`, `4`, `2`, `1800` |
| `frame_budget_bytes`, `stream_buffer_bytes`, `stream_idle_s`, `inline_image_max_bytes` | edit the file | `524288`, `16777216`, `30`, `1048576` |

Keep `canvas_workspace` at its default `chatgpt`. Every `autonom canvas serve` without `--single` is a
workspace Canvas, and a plain `autonom canvas serve --serial X` uses the workspace `default`. With
`canvas_workspace` set to `default`, such a command would attach its device into the adapter's Canvas,
and the adapter's exit would stop that Canvas. A Canvas the adapter starts runs with `--ephemeral` and
`--bootable-only`, so it offers no Boot entries; running devices are listed as before.

### Tools

Every device tool takes an explicit `host_id` and `session_id` (from `list_sessions` or
`start_session`), and every result carries the envelope `autonom.mcp/v1` with an `operation_id` and an
`outcome` (`completed`, `rejected`, `failed` or `outcome_unknown`). Never retry an `outcome_unknown`
mutation without checking `get_operation` and the screen first.

| Family | Tools |
|---|---|
| Hosts and page | `list_hosts`, `open_workspace`, `save_diagnostics` (page only) |
| Sessions | `list_devices`, `list_sessions`, `get_session`, `start_session`, `stop_session` |
| Screen and input | `get_screen`, `get_ui_tree`, `tap_element`, `type_text`, `press_key` |
| Control | `acquire_control`, `release_control`, `get_operation` |
| Flows and evidence | `list_flows`, `start_flow_run`, `get_run`, `cancel_run`, `list_artifacts`, `get_artifact` |
| Media (page only) | `open_stream`, `get_frames`, `close_stream`, `send_input`, `pause_session`, `resume_session` |

The model works under an agent control lease (holder `chatgpt-agent:<host_id>`); a person who presses
Pause in the page takes the lease for the server's own workspace Canvas, so agent input is refused
until Resume.

### Bridge-only media

The page never opens a network connection. The server owns one workspace Canvas (`chatgpt`), reads the
device video from it as a local WebSocket client and hands H.264 packets to the page only through
pull-based `get_frames` calls with byte budgets and backpressure. Taps and keys from the page go back
through `send_input`, which the server sends to the Canvas input routes as human input under the
control lease. The diagnostics page (`ui://autonom/workspace-dev.html`) decodes the stream, measures it,
sends a tap and saves a diagnostics file with `save_diagnostics`.

### Security model

- The Canvas token stays inside the server process: it is never returned in a result or `_meta`,
  printed, logged or written to disk, and the Canvas the server starts has its output discarded because
  it prints its URL with the token.
- Artifacts are served by id only (`art_` plus an HMAC over host, session and relative path with a
  per-install key in `<state>/chatgpt-mcp/artifact-key`). Results never hold absolute paths; network
  captures and secret-looking files are never listed or served.
- The CLI runs as a subprocess from a fixed allowlist of verbs, never through a shell.
- `allowed_targets` limits the devices the adapter may use; diagnostics that contain the Canvas token,
  a bearer header or the home path are refused.

### Adapter manual cleanup

`uninstall` cannot reach everything ChatGPT keeps:

1. In ChatGPT desktop, open Plugins and uninstall *Autonom (dev)* (only the item whose name reads
   exactly *Autonom (dev)*).
2. If `~/.codex/config.toml` still holds a `[plugins."autonom-chatgpt@<marketplace>"]` table, copy the
   file byte for byte to `~/plugins/backups/autonom-chatgpt/`, then remove that one table by hand. Do
   not touch anything else in that file; the script never edits it.
3. Run `install_workspace.py uninstall --purge-cache` to remove the cached copy, then
   `install_workspace.py status --json` to confirm nothing is left (`installed` false, no
   `autonom-chatgpt@...` name in either inventory list).
4. The server keeps its state in `<state>/chatgpt-mcp/` (`artifact-key`, `runs/`, `diagnostics/`,
   `server.log`); delete that folder if you no longer need it.
