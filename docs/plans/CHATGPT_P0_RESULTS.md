# ChatGPT P0 results: desktop host contract

**Status:** filled 2026-10-10; re-verified 2026-10-11 on the fixed probe copy (the `chatgpt-global` and
`chatgpt-devices` columns now come from run `probe-20261011-145407-fac1`), from the observed results files
only (contract section 10). A cell never claims more than its results file says. Check the filled doc with:

```
python3 integrations/chatgpt/scripts/probe_results.py check --profile live --doc docs/plans/CHATGPT_P0_RESULTS.md \
  --results harness-chromium=EV/harness/results-chromium.json \
  --results harness-webkit=EV/harness/results-webkit.json \
  --results harness-devices=EV/live/devices-harness/results-chromium.json \
  --results harness-forwarded=EV/live/forwarded/results-chromium.json \
  --results chatgpt-global=EV/live/results-chatgpt-global.json \
  --results chatgpt-thread=EV/live/results-chatgpt-thread.json \
  --results chatgpt-devices=EV/live/results-chatgpt-devices.json \
  --evidence-dir EV/live
```

`EV` is the run's evidence directory. The empty template is checked with
`probe_results.py check-template --doc docs/plans/CHATGPT_P0_RESULTS.md`.

## 1. Environment

| Item | Value |
| --- | --- |
| ChatGPT desktop build (`CFBundleShortVersionString`, `CFBundleVersion`) | `chatgpt-global` and `chatgpt-devices`: 26.1007.21159 (run `probe-20261011-145407-fac1`, 2026-10-11, on the fixed probe copy installed after integration fix round 1). `chatgpt-thread`: 26.1007.21159 (run `probe-20261010-193021-db30`, 2026-10-10, on the pre-fix probe copy). The original 2026-10-10 global and devices file (run `probe-20261010-184120-4149`, host 26.930.51102, pre-fix copy) is archived in `EV/live/archive-20261010/`. The app was 26.930.31730 (12947) at the start of 2026-10-10 and updated itself between 19:03Z and 19:30Z that day. |
| macOS version (`sw_vers`) | macOS 27.0 (26A428) |
| Mode | Codex mode (the app's mode switcher read "current mode: Codex"). MCP client in both current ChatGPT files: `codex-mcp-client` 0.162.0-alpha.17.2 (the archived 2026-10-10 global file: 0.160.0). Work mode was not exercised. |
| Account surface | ChatGPT desktop app, signed-in owner account, local Personal marketplace |
| Node version | v26.7.0 |
| Python version (`/usr/bin/python3`) | Python 3.9.6 (the launcher ran the server with Homebrew Python 3.14) |
| Probe version | 0.1.0 |
| Date | 2026-10-10 (harness columns, `chatgpt-thread`); 2026-10-11 (`chatgpt-global`, `chatgpt-devices`) |

## 2. Capability matrix

One row per check of the probe page (27 checks), one column per results file. Each cell is `pass`,
`fail` or `not exercised`, optionally followed by ` · <value>`. Columns:

- `harness-chromium`, `harness-webkit`: the fake MCP Apps host harness with the in-process proxy as the
  second origin (`tests/live/chatgpt_probe_host_live.mjs --browser both --remote-proxy`).
- `harness-devices`: the harness in Chromium with the two allowed test devices (`--device`).
- `harness-forwarded`: the harness in Chromium with a real `ssh -L` forward (`--ssh-forward localhost`).
- `chatgpt-global`, `chatgpt-thread`: ChatGPT desktop, opened from the sidebar and from a conversation.
- `chatgpt-devices`: ChatGPT desktop, *Run device checks* pressed in the same page instance as the
  `chatgpt-global` re-check. It is the same run file (`probe-20261011-145407-fac1`, byte-identical copy) saved
  after that press, so its cells other than the two device rows repeat `chatgpt-global` and are not independent
  evidence. Both device rows stayed `not exercised` because they need `net.ws.*`, which the host CSP blocks
  (screenshot `chatgpt-devices.png`).

Cells observed by the operator rather than the page (`observed_by: operator` in the files):

- harness columns: `bridge.context` (the fake host's scripted answer);
- `chatgpt-global` and `chatgpt-devices`: `host.entry` (answered "Opened from: Sidebar (global)"),
  `bridge.context` (value `probe-a891-teal`) and `model.tool`;
- `chatgpt-thread`: `host.entry`, `bridge.context` and `model.tool`.

The operator marked `bridge.context` and `model.tool` only after seeing the chat's answer on screen; for the
re-check the chat answered "Probe nonce: probe-a891-teal (from the latest app context). probe_read returned
success (ok: true)" (screenshot `chatgpt-global-model-check.png`).

| Check | harness-chromium | harness-webkit | harness-devices | harness-forwarded | chatgpt-global | chatgpt-thread | chatgpt-devices |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `net.fetch.local` | pass | pass | pass | pass | fail | fail | fail |
| `net.fetch.remote` | pass | pass | pass | not exercised | fail | fail | fail |
| `net.ws.local` | pass | pass | pass | pass | not exercised | not exercised | not exercised |
| `net.ws.remote` | pass | fail | pass | not exercised | not exercised | not exercised | not exercised |
| `input.rtt.local` | pass | pass | pass | pass | not exercised | not exercised | not exercised |
| `input.rtt.remote` | pass | not exercised | pass | not exercised | not exercised | not exercised | not exercised |
| `media.decode.local` | pass | pass | pass | pass | not exercised | not exercised | not exercised |
| `media.decode.remote` | pass | not exercised | pass | not exercised | not exercised | not exercised | not exercised |
| `net.reconnect.local` | pass | pass | pass | pass | not exercised | not exercised | not exercised |
| `net.reconnect.remote` | pass | not exercised | pass | not exercised | not exercised | not exercised | not exercised |
| `net.csp.local` | pass | pass | pass | pass | fail | fail | fail |
| `net.csp.remote` | pass | pass | pass | not exercised | fail | fail | fail |
| `host.init` | pass | pass | pass | pass | pass | pass | pass |
| `host.entry` | not exercised | not exercised | not exercised | not exercised | pass | pass | pass |
| `host.theme` | pass | pass | pass | pass | pass | pass | pass |
| `host.size` | pass | pass | pass | pass | pass | pass | pass |
| `bridge.read` | pass | pass | pass | pass | pass | pass | pass |
| `bridge.write` | pass | pass | pass | pass | pass | pass | pass |
| `bridge.error` | pass | pass | pass | pass | pass | pass | pass |
| `bridge.stale` | pass | pass | pass | pass | pass | pass | pass |
| `bridge.context` | pass | pass | pass | pass | pass | pass | pass |
| `bridge.message` | pass | pass | pass | pass | pass | pass | pass |
| `bridge.media` | pass | pass | pass | pass | pass | pass | pass |
| `model.tool` | not exercised | not exercised | not exercised | not exercised | pass | pass | pass |
| `media.webcodecs` | pass | pass | pass | pass | pass | pass | pass |
| `device.android.local` | not exercised | not exercised | pass | not exercised | not exercised | not exercised | not exercised |
| `device.ios.remote` | not exercised | not exercised | pass | not exercised | not exercised | not exercised | not exercised |

## 3. Routes

Each route has two results: the command path (tool calls through the host bridge) and the media path
(fetch, WebSocket and decoded video from the page).

| Route | Origin | Command path | Media path | Matrix rows |
| --- | --- | --- | --- | --- |
| Local loopback | `http://127.0.0.1:47811` | pass (all `bridge.*` rows pass in `chatgpt-global` and `chatgpt-thread`) | fail in ChatGPT: `net.fetch.local` fail (`fetch_failed`, clue `csp:connect-src`), `net.csp.local` fail (1 `securitypolicyviolation` event, clue `csp:connect-src`) in both ChatGPT files; dependent rows not exercised. Passes in all four harness columns. | `net.*.local`, `input.rtt.local`, `media.decode.local` |
| Bridge only | none (tool calls only) | pass | pass: `bridge.media` 138.8 fps (median call 17.2 ms, 60 frames) in `chatgpt-global` (re-check run `probe-20261011-145407-fac1`), 72.7 fps (median call 48.4 ms, 60 frames) in `chatgpt-thread`; `media.webcodecs` pass (avc1.42C01E, avc1.42E01F, avc1.640028) | `bridge.*`, `bridge.media` |
| Forwarded endpoint | `http://127.0.0.1:47812` | pass | fail in ChatGPT (`net.fetch.remote` and `net.csp.remote` fail, same `csp:connect-src` clue). `harness-forwarded`: not exercised (ssh to localhost failed host-key verification; ssh settings were not changed). In-process proxy: pass in `harness-chromium`; `harness-webkit` `net.ws.remote` fail (closed with 1006). | `*.remote` in `harness-forwarded` |
| Remote HTTPS host | none | not exercised (no second host) | not exercised (no second host) | none |

Capability each failing route needs:

- **Local loopback and forwarded endpoint:** ChatGPT's page sandbox blocked `fetch` to `http://127.0.0.1:47811`
  and `:47812` with a `connect-src` violation although the UI resource declared both origins in
  `_meta.ui.csp.connectDomains`. The page therefore needs no direct network path at all: media and input
  must travel through the MCP server (tool calls over the host bridge), which the `bridge.media` rows prove.
- **Remote HTTPS host:** not exercised. Needed capability: the MCP server (not the page) connects to an
  authenticated remote Autonom host over HTTPS; whether ChatGPT honours a declared **HTTPS** origin in
  `connectDomains` for a direct page connection is still unverified and is a P3 check.
- **WebKit through a proxy:** `harness-webkit` closed the proxied WebSocket with 1006. ChatGPT desktop renders
  plugin pages with its bundled Chromium (`Codex Framework` 152), so this does not affect the chosen route;
  it is recorded for Safari-based standalone use.

## 4. Transport and topology decision for P3 and P4

**Chosen topology: bridge-only.** The page never opens its own network connection. All commands, screen
frames and input go page → host bridge → Autonom MCP server (local stdio process launched by the plugin) →
Autonom CLI / Canvas host on this Mac; remote hosts are reached by the MCP server over authenticated HTTPS,
never by the page.

Justification from the matrix: `net.fetch.local`, `net.fetch.remote`, `net.csp.local` and `net.csp.remote` fail
in `chatgpt-global`, `chatgpt-thread` and `chatgpt-devices` (the re-check on the fixed probe copy gives the
same result as the 2026-10-10 runs, so the topology is unchanged); `bridge.read`, `bridge.write`, `bridge.error`,
`bridge.stale`, `bridge.context`, `bridge.message`, `bridge.media`, `model.tool` and `media.webcodecs` pass in
both ChatGPT columns. `bridge.media` reached 138.8 fps (global re-check) and 72.7 fps (thread) with small test frames;
real device frames are larger, so P4 must measure frame size, rate and call latency on real streams and use
reduced-rate thumbnails plus one focused stream.

## 5. Installation requirements

- First install (2026-10-10, pre-fix copy): `install_probe.py install` (run with `AUTONOM_HOME` and
  `XDG_STATE_HOME` unset) copied the plugin to `~/plugins/autonom-chatgpt-probe`, backed up and extended the
  existing Personal marketplace (`~/.agents/plugins/marketplace.json`; the `delivery-harness` entry unchanged)
  and wrote the config (`EV/live/install.json`). Plugins → Personal showed *Autonom probe* under "Created by
  you"; Install cached it at `~/.codex/plugins/cache/personal/autonom-chatgpt-probe/0.1.0/` and added
  `[plugins."autonom-chatgpt-probe@personal"]` to `~/.codex/config.toml`. The stdio server started at once
  through `bin/launch.sh` (Homebrew Python 3.14).
- Thread entry (run `probe-20261010-193021-db30`, pre-fix copy): in a conversation, New tab → More tools →
  "Open Autonom probe"; Run all; "Opened from" answered Conversation (thread).
- Re-check (2026-10-11, about 14:50-15:20Z, fixed probe copy): the operator uninstalled the old plugin in
  ChatGPT, installed the integration copy with `install_probe.py install` (`EV/live/recheck/install.json`),
  restarted ChatGPT, installed it in Plugins (screenshot `chatgpt-install.png`), opened Plugins → Autonom
  probe → Open plugin (global entry, full-width view, screenshot `chatgpt-global-wide.png`), pressed Run all,
  answered "Opened from: Sidebar (global)", asked the chat for the nonce and a `probe_read` call (screenshot
  `chatgpt-global-model-check.png`), pressed Passed for "Model context" and "Model calls a tool", then pressed
  Run device checks (screenshot `chatgpt-devices.png`: device rows not exercised, Needs `net.ws.*`). The saved
  run file is `EV/live/recheck/results-chatgpt-global-recheck.json`.
- The app was in **Codex mode** throughout; Work mode was not exercised. No allowed-origin change was
  attempted, because the refusal came from the host CSP, not from the media server's origin check.
- The operator drove the app through macOS Accessibility; the page-saved results files are the evidence.
  The narrow global screenshot `chatgpt-global-narrow.png` shows run `probe-20261011-134301-fc93`
  (pre-fix copy, `EV/live/extra-runs/`): the global entry as a narrow panel beside the chat. That run also
  reported `entry: global`, `net.fetch.local` fail with `csp:connect-src`, and `bridge.media` pass at 71.6 fps
  (median call 71.8 ms).

## 6. Revised estimates

- P1/P2 (sessions and shared control): unaffected by P0; in progress in its own run.
- P3 (MCP adapter and host API): larger than planned. The MCP server must carry media and input as well as
  commands (bridge-only), and remote hosts are reached server-side; the stdio server and tool envelope from
  this probe are reusable.
- P4 (workspace UI): media arrives as tool results, not a WebSocket; the Canvas decoder and input code are
  reusable but the transport layer is new. Measure real frame sizes first.
- No delivery-date commitment, as in the plan.

## 7. Evidence

| Column | File | Run | sha256 |
| --- | --- | --- | --- |
| `harness-chromium` | `EV/harness/results-chromium.json` | `probe-20261010-174522-894c` | `d463fe87323c650930c216e4783c2101d75593c2e6ebf411d1b99b23535a9e47` |
| `harness-webkit` | `EV/harness/results-webkit.json` | `probe-20261010-174533-dd99` | `fdcecfea28c89a02e9c6313846487256fa879f7a287440a799ffc4e04d90ae58` |
| `harness-devices` | `EV/live/devices-harness/results-chromium.json` | `probe-20261010-174824-7ea7` | `20c5a44be0b8ff6d9aa182d3db462b63dfc06f04b5333b68cb13665f400ff24f` |
| `harness-forwarded` | `EV/live/forwarded/results-chromium.json` | `probe-20261010-174916-bcfa` | `e91e21a11353b8138ebf289cf26dcc530ff4e8eb021c8a83c687ee7a08b04452` |
| `chatgpt-global` | `EV/live/results-chatgpt-global.json` | `probe-20261011-145407-fac1` | `331d316c35bd986cf7e39beeb2e7f4fa952209552492ef8478574fa213bc01d8` |
| `chatgpt-thread` | `EV/live/results-chatgpt-thread.json` | `probe-20261010-193021-db30` | `b9093e82e4a944e60fa64fc6957686b4d040843176c203f34eb7f71cec55d9b4` |
| `chatgpt-devices` | `EV/live/results-chatgpt-devices.json` | `probe-20261011-145407-fac1` | `331d316c35bd986cf7e39beeb2e7f4fa952209552492ef8478574fa213bc01d8` |

Other files:

| File | What | sha256 |
| --- | --- | --- |
| `EV/live/recheck/results-chatgpt-global-recheck.json` | re-check run `probe-20261011-145407-fac1` (source of both current ChatGPT global and devices files) | `331d316c35bd986cf7e39beeb2e7f4fa952209552492ef8478574fa213bc01d8` |
| `EV/live/recheck/install.json` | install of the fixed probe copy | `473559e408ef1d8a2e63a0a59a738afb2c987f412d2d93cb1adee8c10545cd0b` |
| `EV/live/archive-20261010/results-chatgpt-global.json` | replaced 2026-10-10 global file, run `probe-20261010-184120-4149`, host 26.930.51102 | `a9d7006f11de58f6b75ce0da635e305ca4b7ecae523aeca0ea4ebd233c57d15c` |
| `EV/live/archive-20261010/results-chatgpt-devices.json` | replaced 2026-10-10 devices file, same run | `a9d7006f11de58f6b75ce0da635e305ca4b7ecae523aeca0ea4ebd233c57d15c` |
| `EV/live/results-chatgpt-global-run1.json` | 2026-10-10 copy of run `probe-20261010-184120-4149` taken before the operator answers | `1961bf0ef0eef041f081ed2b59c9095076246142da3a9e8c70c6590c6e02c335` |
| `EV/live/extra-runs/probe-20261010-185935-6729.json` | extra run, second in-conversation instance (superseded) | `88f83db3e0c7b3944b9996b7693974b7e13949f2aec3aa0b8d144e1c6e044f09` |
| `EV/live/extra-runs/probe-20261010-190313-debe.json` | extra run, "Run again" on that instance (superseded) | `da5a9077fdf5af74c294219c7ca4ce601a3feec7d054f873fce334825bf46a68` |
| `EV/live/extra-runs/probe-20261011-134301-fc93.json` | global entry on the pre-fix copy (narrow screenshot) | `3fe7317cbfe97829b6e750020c0649455efd727c6f98e34bb48d556a4f38eb79` |
| `EV/live/screenshots/chatgpt-install.png` | re-check: plugin installed in Plugins | `c5282e7596009071628d6246ab8b78b5b91af076ba5a876087c307682740c1c8` |
| `EV/live/screenshots/chatgpt-global-wide.png` | re-check: global entry, full-width view | `2a007331fe572b3a055b87d17868dd1a420e1a8a961f1f4d00e984210b378fad` |
| `EV/live/screenshots/chatgpt-global-model-check.png` | re-check: chat answer with nonce and `probe_read` result | `9fb396254ddb7e43894a5aca954015323a2551212866323ccfd2ca2459297ef9` |
| `EV/live/screenshots/chatgpt-devices.png` | re-check: after Run device checks | `f5d2df0c488f151cce920ad1a38025eda12c1285fc3b23e8d430d9c624abd93d` |
| `EV/live/screenshots/chatgpt-global-narrow.png` | run `probe-20261011-134301-fc93`, global entry as a narrow panel | `7fedb9df3190e85c27dab02e461a920f6adbf8575a96aa93e8057d32867ba3b0` |
| `EV/live/screenshots/chatgpt-thread-wide.png` | run `probe-20261010-193021-db30`, thread entry | `4b8461f5592c07aab40425752830ce4bc6d9775c3622076b06a38bd2f8be1713` |

Also in `EV/live/`: `environment.json`, `status-before.json`, `install-dry-run.json`, `install.json`,
`ssh-unavailable.json`, `entry-note.json`, `operator-notes.md`, `results-manifest.txt` (sha256 of each
page-saved source file and its copy, including the 2026-10-11 replacement and archive lines) and the harness
logs. No results file is a stub.

## 8. Not exercised and why

- `harness-chromium` `host.entry`: Needs the operator: the host did not report the entrypoint
- `harness-chromium` `model.tool`: Needs the operator: ask the chat to call probe_read
- `harness-chromium` `device.android.local`: Run after the synthetic checks pass
- `harness-chromium` `device.ios.remote`: Run after the synthetic checks pass
- `harness-webkit` `input.rtt.remote`: Needs net.ws.remote
- `harness-webkit` `media.decode.remote`: Needs net.ws.remote
- `harness-webkit` `net.reconnect.remote`: Needs net.ws.remote
- `harness-webkit` `host.entry`: Needs the operator: the host did not report the entrypoint
- `harness-webkit` `model.tool`: Needs the operator: ask the chat to call probe_read
- `harness-webkit` `device.android.local`: Run after the synthetic checks pass
- `harness-webkit` `device.ios.remote`: Needs net.ws.remote
- `harness-devices` `host.entry`: Needs the operator: the host did not report the entrypoint
- `harness-devices` `model.tool`: Needs the operator: ask the chat to call probe_read
- `harness-forwarded` `net.fetch.remote`: ssh forward unavailable: Host key verification failed.
- `harness-forwarded` `net.ws.remote`: ssh forward unavailable: Host key verification failed.
- `harness-forwarded` `input.rtt.remote`: ssh forward unavailable: Host key verification failed.
- `harness-forwarded` `media.decode.remote`: ssh forward unavailable: Host key verification failed.
- `harness-forwarded` `net.reconnect.remote`: ssh forward unavailable: Host key verification failed.
- `harness-forwarded` `net.csp.remote`: ssh forward unavailable: Host key verification failed.
- `harness-forwarded` `host.entry`: Needs the operator: the host did not report the entrypoint
- `harness-forwarded` `model.tool`: Needs the operator: ask the chat to call probe_read
- `harness-forwarded` `device.android.local`: Run after the synthetic checks pass
- `harness-forwarded` `device.ios.remote`: ssh forward unavailable: Host key verification failed.
- `chatgpt-global` `net.ws.local`: Needs net.fetch.local
- `chatgpt-global` `net.ws.remote`: Needs net.fetch.remote
- `chatgpt-global` `input.rtt.local`: Needs net.ws.local
- `chatgpt-global` `input.rtt.remote`: Needs net.ws.remote
- `chatgpt-global` `media.decode.local`: Needs net.ws.local
- `chatgpt-global` `media.decode.remote`: Needs net.ws.remote
- `chatgpt-global` `net.reconnect.local`: Needs net.ws.local
- `chatgpt-global` `net.reconnect.remote`: Needs net.ws.remote
- `chatgpt-global` `device.android.local`: Needs net.ws.local
- `chatgpt-global` `device.ios.remote`: Needs net.ws.remote
- `chatgpt-thread` `net.ws.local`: Needs net.fetch.local
- `chatgpt-thread` `net.ws.remote`: Needs net.fetch.remote
- `chatgpt-thread` `input.rtt.local`: Needs net.ws.local
- `chatgpt-thread` `input.rtt.remote`: Needs net.ws.remote
- `chatgpt-thread` `media.decode.local`: Needs net.ws.local
- `chatgpt-thread` `media.decode.remote`: Needs net.ws.remote
- `chatgpt-thread` `net.reconnect.local`: Needs net.ws.local
- `chatgpt-thread` `net.reconnect.remote`: Needs net.ws.remote
- `chatgpt-thread` `device.android.local`: Needs net.ws.local
- `chatgpt-thread` `device.ios.remote`: Needs net.ws.remote
- `chatgpt-devices` `net.ws.local`: Needs net.fetch.local
- `chatgpt-devices` `net.ws.remote`: Needs net.fetch.remote
- `chatgpt-devices` `input.rtt.local`: Needs net.ws.local
- `chatgpt-devices` `input.rtt.remote`: Needs net.ws.remote
- `chatgpt-devices` `media.decode.local`: Needs net.ws.local
- `chatgpt-devices` `media.decode.remote`: Needs net.ws.remote
- `chatgpt-devices` `net.reconnect.local`: Needs net.ws.local
- `chatgpt-devices` `net.reconnect.remote`: Needs net.ws.remote
- `chatgpt-devices` `device.android.local`: Needs net.ws.local
- `chatgpt-devices` `device.ios.remote`: Needs net.ws.remote
