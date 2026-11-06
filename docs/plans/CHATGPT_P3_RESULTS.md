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

- Date (UTC): 2026-11-06 (iOS 11:08–11:09, Android 11:09–11:10)
- Repository commit: 146c960 plus the live script's `stopApp` step (committed with this results doc)
- macOS: 27.2
- Python (server): 3.14.7
- Python (CLI): 3.14.7
- Node: v26.7.0
- ChatGPT desktop: 26.1021.31345 installed; not driven in this run
- Canvas stream settings (Android, iOS): defaults, no Display choice

ChatGPT steps:

- chat.list_sessions: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)
- chat.start_session: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)
- chat.read_screen: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)
- chat.tap_element: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)
- entry.global: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)
- entry.thread: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)
- chat.stop_session: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)
- uninstall_restore: not run: driving ChatGPT desktop waits for the owner's go-ahead (2026-11-06)

Other evidence files (kept outside the repository with the results files):

- `mcp-live-android.json` sha256 fecdeb3dc3357318ec97c37060bf9d6cb7c829ba53a2670efa248ae1f3742129
- `mcp-live-ios.json` sha256 616d6655ee7e6945a09cf7d2f7515a0c2874d85ebe6f9cdd2a91e7f2a23dc81a

<!-- autonom-results:begin -->

## Environment

| Item | Value |
|---|---|
| Adapter version | 0.1.0 |
| Android target | emulator-5584 |
| iOS target | 34A85293-39A2-436E-B669-12574874EC75 |

## Device runs

### Android

| Check | Status | Detail |
|---|---|---|
| hosts | pass | one online local host, version 0.37.0 |
| devices | pass | listed, state device, booted here |
| start | pass | created, then session_exists, then attached to the same session |
| foreign_host | pass | host_not_found, no new screenshot |
| screen | pass | PNG 1080x2400, 508866 bytes |
| tree | pass | 28 nodes |
| flow_passed | pass | passed, 5 steps, 1 report artifacts listed |
| tap_changes_screen | pass | tapped text 'Battery'; the tree changed |
| op_reconciled | pass | found 1 journal entries |
| pause_refuses_agent | pass | paused at generation 2 (was 1); the tap was refused control_held |
| resume_reenables | pass | press_key completed at generation 3 |
| cancel_run | pass | cancelled in 0.2 s, no flow process left |
| media_focus | pass | 29.9 frames/s over 20.0 s, 1 resyncs, 0 gaps, 0 dropped; 31 scrolls during the measurement |
| media_thumb | pass | 10 key frames in 11.0 s (need 4), median interval 1.1 s, 0 non-key |
| input_tap | pass | released (completed), centre tap completed |
| artifacts | pass | 12 artifacts (image, journal, json); 2 images fetched |
| no_secrets | pass | 664 server lines and the log checked |
| stop | pass | stopped; streams closed; device detached; Canvas stopped |

### iOS

| Check | Status | Detail |
|---|---|---|
| hosts | pass | one online local host, version 0.37.0 |
| devices | pass | listed, state Booted |
| start | pass | created, then session_exists, then attached to the same session |
| foreign_host | pass | host_not_found, no new screenshot |
| screen | pass | PNG 1206x2622, 83140 bytes |
| tree | pass | 5 nodes |
| flow_passed | pass | passed, 5 steps, 1 report artifacts listed |
| tap_changes_screen | pass | tapped desc 'General'; the tree changed |
| op_reconciled | pass | found 1 journal entries |
| pause_refuses_agent | pass | paused at generation 2 (was 1); the tap was refused control_held |
| resume_reenables | pass | press_key completed at generation 3 |
| cancel_run | pass | cancelled in 0.2 s, no flow process left |
| media_focus | pass | 36.4 frames/s over 20.0 s, 0 resyncs, 0 gaps, 0 dropped; 16 scrolls during the measurement |
| media_thumb | pass | 6 key frames in 10.7 s (need 4), median interval 2.0 s, 0 non-key |
| input_tap | pass | released (completed), centre tap completed |
| artifacts | pass | 12 artifacts (image, journal, json); 2 images fetched |
| no_secrets | pass | 815 server lines and the log checked |
| stop | pass | stopped; streams closed; device detached; Canvas stopped |

## Measurements

| Profile | Platform | frames/s | bytes/s | median call ms | p95 call ms | largest packet | key frame bytes | resyncs |
|---|---|---|---|---|---|---|---|---|
| focus | android | 29.9 | 758598.4 | 33.3 | 39.1 | 50316 | 50316 | 1 |
| thumb | android | 0.9 | 44007.5 | 561.5 | 1023.8 | 48433 | 48433 | 9 |
| focus | ios | 36.4 | 381939.4 | 16.6 | 20.2 | 93187 | 93187 | 0 |
| thumb | ios | 0.6 | 37279.6 | 1008.3 | 1014.8 | 66677 | 66677 | 0 |
| focus | chatgpt | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| thumb | chatgpt | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## ChatGPT run

| Check | Status | Detail |
|---|---|---|
| bridge.list_sessions | not_exercised | no diagnostics file |
| media.focus | not_exercised | no diagnostics file |
| media.thumb | not_exercised | no diagnostics file |
| input.tap | not_exercised | no diagnostics file |
| control.pause | not_exercised | no diagnostics file |
| control.resume | not_exercised | no diagnostics file |

## Not exercised and why

| Item | Reason |
|---|---|
| chatgpt bridge.list_sessions | no diagnostics file |
| chatgpt media.focus | no diagnostics file |
| chatgpt media.thumb | no diagnostics file |
| chatgpt input.tap | no diagnostics file |
| chatgpt control.pause | no diagnostics file |
| chatgpt control.resume | no diagnostics file |

## Evidence

| File | sha256 |
|---|---|
| mcp-live-android.json | fecdeb3dc3357318ec97c37060bf9d6cb7c829ba53a2670efa248ae1f3742129 |
| mcp-live-ios.json | 616d6655ee7e6945a09cf7d2f7515a0c2874d85ebe6f9cdd2a91e7f2a23dc81a |

<!-- autonom-results:end -->
