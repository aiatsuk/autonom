# ChatGPT adapter P3a: live results

Status: template. The coordinator fills this document from observed files only: the device
results of `tests/live/chatgpt_mcp_live.py` (`mcp-live-android.json`, `mcp-live-ios.json`),
the diagnostics file the workspace page saved in ChatGPT, the `install_workspace.py status
--json` files and the screenshots. Nothing here is written from memory or from a summary.

Everything the results files back is one generated block between the two marker comments
below, and nobody edits it by hand. It is written with

```bash
python3 tests/live/chatgpt_mcp_live.py fill-doc --doc docs/plans/CHATGPT_P3_RESULTS.md \
    --results <evidence>/mcp-live-android.json --results <evidence>/mcp-live-ios.json \
    --results <evidence>/chatgpt-diagnostics.json
```

and checked with the same arguments and `check-doc` in place of `fill-doc` (`--results A B C`
with one flag is the same). `check-doc` renders the block again from the given files with the
renderer `fill-doc` uses and requires the bytes between the markers to be the same: exactly
one begin and one end marker, in order, each alone on its line. It prints
`{"problems": [...]}` and exits 1 on any problem; a difference names the first differing line
with the expected and the found text. Nothing outside the block is parsed, so outside it no
line may contain a vertical bar (no table of any form), copy a marker or still hold a
placeholder, and no credential (an Authorization header value or a `token` query value) may
appear anywhere: the same scan as `check` runs, and `fill-doc` refuses to write a document
with a hit.

In the block a device row is `not_exercised` unless that platform's results file is given, a
measurement is `n/a` unless a given file records it, a page check is `not_exercised` unless
the diagnostics file is given, and every `not_exercised` row has its own row under "Not
exercised and why". The adapter version and the targets are the results files' own values,
and the files must agree on the version. Device runs have one row per live check of section
9, in run order. Measurements are per profile and platform (`android` and `ios` from the
device results, `chatgpt` from the page diagnostics): frames/s and bytes/s over the pull
window, call ms is the `get_frames` round trip, packet sizes in bytes, one decimal or `n/a`.
Evidence lists the results files with their sha256 (Android, iOS, diagnostics). Status values:
`pass`, `fail`, `not_exercised`. Contract: run ws-p3-mcp-adapter, sections 8 and 9; the plan
is `docs/plans/CHATGPT_DEVICE_WORKSPACE.md` (row P3).

The adapter reads the same device stream the Canvas pages show. A browser tab's Display menu
(Device settings: Frame rate and Size) changes that stream for every reader of the device, not
only for that tab: Half size lowers the frame size the adapter's `get_frames` packets carry,
and on iOS On change (fps 0) sends a frame only when the screen changes, so frames/s, bytes/s
and packet sizes drop on a still screen. The choice is kept per device in that browser and sent
again when its page reconnects. Before the device runs, the coordinator closes other Canvas tabs
of the two targets or sets Full and 60 fps there, and records the Canvas `/status`
`stream_settings` of each target under Environment.

## Coordinator record

What no results file holds, written by the coordinator as plain lists: each item gets its
value, or its status (`pass`, `fail` or `not_exercised`) and what was observed. The ChatGPT
steps are the chat steps of the ChatGPT desktop case, both entry points of the page and the
uninstall restore comparison of the status files (the six page checks, validated with
`chatgpt_mcp_live.py check-chatgpt`, are in the block). Other evidence files (status files,
screenshots) are listed with their sha256 (`shasum -a 256 FILE`), named relative to the folder
of the results files.

Environment:

- Date (UTC): pending
- Repository commit: pending
- macOS: pending
- Python (server): pending
- Python (CLI): pending
- Node: pending
- ChatGPT desktop: pending
- Canvas stream settings (Android, iOS): pending

ChatGPT steps:

- chat.list_sessions: pending
- chat.start_session: pending
- chat.read_screen: pending
- chat.tap_element: pending
- entry.global: pending
- entry.thread: pending
- chat.stop_session: pending
- uninstall_restore: pending

Other evidence files:

- pending

<!-- autonom-results:begin -->

## Environment

| Item | Value |
|---|---|
| Adapter version | pending |
| Android target | pending |
| iOS target | pending |

## Device runs

### Android

| Check | Status | Detail |
|---|---|---|
| hosts | pending | pending |
| devices | pending | pending |
| start | pending | pending |
| foreign_host | pending | pending |
| screen | pending | pending |
| tree | pending | pending |
| flow_passed | pending | pending |
| tap_changes_screen | pending | pending |
| op_reconciled | pending | pending |
| pause_refuses_agent | pending | pending |
| resume_reenables | pending | pending |
| cancel_run | pending | pending |
| media_focus | pending | pending |
| media_thumb | pending | pending |
| input_tap | pending | pending |
| artifacts | pending | pending |
| no_secrets | pending | pending |
| stop | pending | pending |

### iOS

| Check | Status | Detail |
|---|---|---|
| hosts | pending | pending |
| devices | pending | pending |
| start | pending | pending |
| foreign_host | pending | pending |
| screen | pending | pending |
| tree | pending | pending |
| flow_passed | pending | pending |
| tap_changes_screen | pending | pending |
| op_reconciled | pending | pending |
| pause_refuses_agent | pending | pending |
| resume_reenables | pending | pending |
| cancel_run | pending | pending |
| media_focus | pending | pending |
| media_thumb | pending | pending |
| input_tap | pending | pending |
| artifacts | pending | pending |
| no_secrets | pending | pending |
| stop | pending | pending |

## Measurements

| Profile | Platform | frames/s | bytes/s | median call ms | p95 call ms | largest packet | key frame bytes | resyncs |
|---|---|---|---|---|---|---|---|---|
| focus | android | pending | pending | pending | pending | pending | pending | pending |
| thumb | android | pending | pending | pending | pending | pending | pending | pending |
| focus | ios | pending | pending | pending | pending | pending | pending | pending |
| thumb | ios | pending | pending | pending | pending | pending | pending | pending |
| focus | chatgpt | pending | pending | pending | pending | pending | pending | pending |
| thumb | chatgpt | pending | pending | pending | pending | pending | pending | pending |

## ChatGPT run

| Check | Status | Detail |
|---|---|---|
| bridge.list_sessions | pending | pending |
| media.focus | pending | pending |
| media.thumb | pending | pending |
| input.tap | pending | pending |
| control.pause | pending | pending |
| control.resume | pending | pending |

## Not exercised and why

| Item | Reason |
|---|---|
| pending | pending |

## Evidence

| File | sha256 |
|---|---|
| pending | pending |

<!-- autonom-results:end -->
