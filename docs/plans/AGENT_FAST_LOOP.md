# Agent fast loop: wait in the call, short replies, visible slow path

- Status: active
- Source: owner request, 2026-11-27
- Last updated: 2026-11-27 Europe/Moscow

## Goal

An agent that drives a device through Autonom spends less wall time and fewer
tokens per step, on any app:

1. it waits inside a call instead of sleeping between calls;
2. an action answers with a few lines that say what changed;
3. a reply says first when the fast Android path is not in use and how to get it back;
4. an evidence screenshot is taken by the action's own call;
5. the skills an agent loads for this loop are short;
6. a persistent MCP server removes the per-call process start;
7. the Flutter skills say when to hot reload, hot restart or rebuild.

## Relevant context

- `scripts/autonom.py`: parser (`build_parser`), `cmd_ui_wait`, the action verbs
  (`cmd_ui_tap|swipe|gesture|type|fill|key|do`), `_ObservePlan`, `emit`, the
  verb classification tables.
- `scripts/autonom_lib/observe.py` (the screen after an action), `ui.py`
  (`settle`, `snapshot`, selection), `ui_android.py` (`provenance`, fallback),
  `network/store.py` (recorded flows), `screenshot.py` (`capture_evidence`).
- Agent profile: `session start --profile agent` (outline trees, observed actions,
  small screenshots).
- Skills: `plugins/autonom/skills/{autonom,mobile-screen,mobile-network,mobile-session}`
  (about 107 KB together), `flutter-debugger-agent`.
- MCP precedent: `integrations/chatgpt/autonom-chatgpt`.
- Docs checked against the parser: `docs/CAPABILITIES.md` (`tests/test_docs_cli_surface.py`),
  `docs/COMPATIBILITY.md`, `docs/USAGE.md`, `CHANGELOG.md`.

## Constraints

- Stdlib-only Python 3 and Node.js; no package manager.
- The default profile keeps today's replies byte for byte; changes of the agent
  profile are listed under Changed in the changelog and in `docs/COMPATIBILITY.md`.
- An action's single input stays single: a wait or a screenshot never sends input.
- Flags added now are matched only when spelled in full.
- No commits or pushes without the owner's request.

## Done when

- Each item below has code, tests, and an entry in `CHANGELOG.md` and the docs.
- `make check` passes.
- The loop is tried once on a real emulator or simulator, or the report says it was not.

## Verification

- Unit and CLI tests against the fakes in `tests/fakes` for every new flag and reply.
- `make check` once at the end (it creates temporary certificates and loopback
  listeners in its network tests: run only with the owner's consent).

## Out of scope

- Working through another tool's already running UiAutomator2 server (item 3, later).
- In-app extensions that need a special build (state, configs, in-app mocks).

## Decisions and rationale

- **Element waits live on `ui wait`** with the selector flags `ui find` and `ui tap`
  take, plus `--gone`. One resolution core, no new selector grammar.
- **Request waits live on `network requests wait`** with the filters of
  `network requests list`: the proxy is Autonom's own, so it works for any app.
- **`--until` on action verbs** takes `text=`, `desc=`, `id=`, `gone:<one of those>` and
  `request=GLOB`; repeatable, all must hold. The request cursor is taken before the
  input, so a fast response is not missed.
- **An unmet wait is not a failed input**: the reply keeps `ok: true`, says the
  condition was not met and the command exits 1, so nothing invites a second input.
- **`--timeout` on `ui wait` is refused** instead of being read as `--timeout-ms`.
- **Short text replies are the agent profile's default** for the act and wait verbs;
  `--json` gives today's JSON line and `--screen` adds the whole outline.
- **The fast-path line reuses `fallback_reason` and `fallback_hint`**: first line of a
  text reply, first keys after `ok` of a JSON reply. No new key.
- **`--shot` is `autonom screenshot --label`** through one shared function, so the file,
  the index entry and the agent-screenshot rules are the same.
- **The start skill became the loop** (5 KB) with five recipes; the old map moved to
  `references/system-map.md`. The three verb skills keep their depth and gain the
  no-sleep rule.
- **The MCP server runs the CLI's own `main` in its process.** No second
  implementation of any verb; the tools only build argv and word the reply. It is a
  verb (`autonom mcp serve`), so every installed CLI has it, and the plugin declares
  it with `command: autonom`.
- **MCP replies do not depend on the session profile**: the action tools pass
  `--observe --json` and word the reply with `brief`.

## Current status

All seven items are implemented with tests and documentation. Not committed.

## Completed

- Item 1: `ui wait` by selector and `--gone`, `network requests wait`, `--until` and
  `--until-timeout-ms` on the action verbs, refusal of `--timeout`
  (`scripts/autonom_lib/wait.py`, `network/store.py` `Tail`, `tests/test_wait.py`).
- Item 2: text replies in an agent-profile session, `--json`, `--screen`
  (`scripts/autonom_lib/brief.py`, `tests/test_brief.py`). Tests that read JSON in an
  agent session pass `--json`; the ChatGPT adapter passes `--json` too.
- Item 3: the fast-path line and key order.
- Item 4: `--shot LABEL`.
- Item 5: the short start skill, five recipes, the no-sleep rule in the verb skills
  (`tests/test_skill_loop.py` parses every taught command with the real parser).
- Item 6: `autonom mcp serve` (`scripts/autonom_lib/mcp_server.py`,
  `tests/test_mcp_server.py`), declared in `plugins/autonom/.mcp.json`.
- Item 7: hot reload, hot restart and rebuild in `flutter-debugger-agent`.

## Next step

Run one scenario end to end with an agent (the model in the loop) on the released CLI
and on the working tree, and count calls, time and tokens per action; then run the full
`make check`.

## Failed approaches

- A request wait that only looked at requests recorded after the call began missed
  responses that came before the wait started; the action's cursor (`--until
  request=`) and `--since-id` cover it.
- Refusing never-ending verbs in the MCP server by reading their argv words was
  unreliable (option values look like words); the handlers refuse themselves.

## Verification evidence

2026-11-27, on `main` at 05deb9c:

- Plugin validation, `compileall`, `node --check`, `bash -n` and `git diff --check` pass.
- The new test modules and the ones this change touches pass: `test_wait` (54),
  `test_brief` (40), `test_mcp_server` (24), `test_skill_loop` (10), and the observe,
  agent-profile, Android helper, journal, flow compiler, bare-host, documentation and
  ChatGPT adapter suites. No JavaScript changed.
- The full gate (`make check`) is left to CI: `test_network` and
  `test_transparent_capture` create temporary certificates and keys.
- Real Android emulator (API 34, the on-device helper serving every measured call),
  Settings, open one section and go back, 10 repeats, medians:

  | Call | Released 0.38.1 | Working tree, CLI | Working tree, MCP |
  | --- | --- | --- | --- |
  | observed tap | 2022 ms | 1585 ms | 1408 ms |
  | tap, then wait for a node, then screenshot | 2853 ms (three calls: tap, tree, screenshot) | 1994 ms (one call) | 1045 ms (one call) |
  | observed BACK | 1743 ms | 1402 ms | 1213 ms |
  | read the screen | 393 ms | 296 ms | 71 ms |
  | wait for a node that is already there | n/a | 299 ms | 91 ms |

  The plain tap path did not change between the two CLIs: read that row as run-to-run
  spread, not as a gain. Model time is not in these numbers.
- With another tool holding UiAutomation on the same emulator, `ui tree` took 3.9 s and
  failed with `backend_failed` (incomplete hierarchy) without naming the cause. The error
  envelope now carries `fallback_reason` and `fallback_hint` first
  (`tests/test_brief.py`, `BusyUiAutomationTests`).

## Open risks

- Agent-profile replies changed shape (text by default): wrappers that parse them
  need `--json`.
- Polling a tree through `uiautomator dump` costs a second or more per read; waits
  are only cheap on the helper and on iOS idb.
- The MCP server is declared for every plugin user: a host without `autonom` on its
  `PATH` lists it as failed. It has not been tried in a real agent host yet.
- After a `uiautomation_busy` failure the helper is not tried again for 60 s, so a
  session keeps the slow path for a minute after the other tool has gone.
- An observed tap waits for half a second of quiet: about 1.4 s a tap on the emulator
  measured. With `--until` the wait ends when the condition holds (about 1.0 s).
