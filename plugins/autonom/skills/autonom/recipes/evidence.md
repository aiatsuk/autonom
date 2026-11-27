# Recipe: evidence without extra calls

## A screenshot with the step that reaches the state

```bash
autonom ui tap --text "Pay" --until text="Paid" --shot "payment accepted"
# ...
# shot: ~/.autonom/sessions/<id>/shots/0004_101500_payment-accepted.jpg
```

`--shot LABEL` takes the screenshot after the wait, in the same call, and files
it in the session. The reply only names the path: you do not need to open the
image to know the step worked. The `changes:` lines say what the screen shows.

Open an image only to judge how something looks (layout, colour, clipping).
On Android an agent-session screenshot is a small JPEG, not in screen pixels:
tap from the tree, not from the picture.

```bash
autonom screenshot --label "empty state"           # a screenshot on its own
autonom shots list --max 10                        # what was captured, with labels
```

## Notes and the journal

```bash
autonom note add "500 on orders shows the generic banner; retry works" --task orders
autonom journal --max 20                           # the last steps of this session
autonom journal --kind note                        # only the notes
```

Every command of the session is journaled by itself, with its arguments
scrubbed of secrets. Write a note for what you concluded, not for what you ran.

## Logs, bounded

```bash
autonom logs tail --package com.example.app --max-lines 40 --grep 'E/|Exception'
```

Always bound a read (`--max-lines`, `--max`, `--grep`). Big artifacts (a HAR, a
trace, a full log) stay on disk: cite the path.

## What counts as proof

- The reply's `changes:` after an action, or `ui find`, proves what is on
  screen. An exit code of 0 alone does not.
- A before/after pair needs the same state twice: `--shot` on the step
  before the change and on the step after it.
- Say when a screenshot shows mocked data.

More: the `mobile-screen` skill (screenshots), `mobile-flow` (a repeatable
flow with a report), `ticket-pretest` (a packed verdict for QA).
