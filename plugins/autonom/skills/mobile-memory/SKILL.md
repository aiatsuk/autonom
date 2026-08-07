---
name: mobile-memory
description: Reuse and record per-app Autonom knowledge for device work. Read the app's overlay and flow runbooks before driving a device, and save a new flow only when it is durable, reusable, and evidence-backed. Triggers on a device task against a known app (a package/bundle id is in play), and after working out a repeatable procedure — make an order, mock a screen, log in, reproduce a bug.
---

# Mobile Memory

Per-app knowledge that a device task needs but the CLI cannot know: the backend,
how the app's network is wired, where its response schemas live, and step-by-step
flows worked out once and replayed after. It exists so the same hour of digging
is not spent twice.

## Where it lives

```
~/.autonom/apps/<package>/
  app.md              identity, backend, network model, TLS, schema roots, gotchas
  flows/<name>.md     a runbook: goal, endpoint, schema source, exact steps, verify
  bodies/<name>.json  ready mock payloads that flows reference
```

**Machine-local and per-person**, plain markdown — not in the app repo, not in
the Autonom repo. The same idea as a personal memory store: it accumulates as you
work and stays yours. The skill (this file) ships with Autonom; the knowledge
does not.

## Before a device task

1. Resolve the package id — from `--app-id`, the current session
   (`autonom session show`), or the repository under test.
2. Read `~/.autonom/apps/<package>/app.md` if it exists. It answers, up front,
   the questions that otherwise surface as wasted time: which backend, does the
   app cache the proxy, is `dart:io` even used, where are the schemas.
3. If the task matches a flow (mock a screen, make an order), read
   `flows/<name>.md` and follow it instead of re-deriving the endpoint, schema
   and gotchas.
4. Nothing there yet? Proceed normally — and consider leaving a flow behind when
   you crack something that will recur.

## When to save — and when not

Save **only** knowledge that is durable, reusable, and evidence-backed: the exact
endpoint, the exact glob, the path to the schema, the gotcha that cost time.
Prefer updating the existing file over adding a near-duplicate.

Do **not** save one-off exploration, obvious single commands, or anything already
in the code. **Never** write secrets — reference "log in as the test driver", not
an account or token. Keep each entry id-centric and concrete (`com.example.rides`,
`POST /api/v1/orders`, `lib/models/**/*_response.dart`), never vague.

A flow is worth a file when it is multi-step and will happen again. A single verb
is not a flow.

## Format

- **app.md** — identity (package, backend, build), how the network works (native
  vs `dart:io`, proxy caching), TLS/CA notes, where schemas live, a *known
  screens* table (screen → endpoint → flow), and gotchas already handled by the
  tool so a reader does not redo them.
- **flows/<name>.md** — Goal · Endpoint · Schema source · Steps (exact CLI) ·
  Verify · Gotchas. Reconstruct a response schema from the freezed model:
  `@JsonKey(name: ...)` is the JSON field, `required` without `?` is mandatory.
- **bodies/<name>.json** — a captured or hand-built payload a flow points at, so
  the shape is not rebuilt each time.

## How it connects

`project-router` detects the app → this skill loads its overlay and any matching
flow → the `mobile-session` / `mobile-screen` / `mobile-network` skills run the
CLI verbs → new knowledge is written back here. The knowledge layer sits beside
the thin verb-skills; it does not replace them.

## Related

- `project-router` — routes here once a device task targets a known app.
- `mobile-session`, `mobile-screen`, `mobile-network` — the verbs a flow drives.
