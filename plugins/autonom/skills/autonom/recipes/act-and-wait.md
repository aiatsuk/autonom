# Recipe: act, wait, read the reply

## Act

```bash
autonom ui tap --text "Orders"                    # by meaning; iOS labels are in --desc
autonom ui tap --resource-id com.example.app:id/submit
autonom ui tap --text "Item" --index 1            # the second of several matches
autonom ui tap --x 540 --y 1210                   # only what the tree does not list
autonom ui tap --text "Photo" --duration 600      # long press, milliseconds
autonom ui type "test-user"                       # into the focused field
autonom ui fill "new text" --resource-id com.example.app:id/name   # Android: replace the text
autonom ui swipe --from 540,1600 --to 540,600     # scroll down; --duration is SECONDS
autonom ui key BACK                               # Android key names; iOS: HOME, LOCK
```

`ui type --sensitive` keeps a password out of every reply and artifact.

## Wait in the same call

```bash
autonom ui tap --text "Pay" --until text="Paid"                         # a node comes
autonom ui tap --text "Refresh" --until gone:id=com.example.app:id/spinner
autonom ui tap --text "Refresh" --until 'request=*/v1/orders*'          # capture is on
autonom ui tap --text "Pay" --until text="Paid" --until-timeout-ms 30000
```

`text=`, `desc=`, `id=` match like the selector flags (contains, any case).
Repeat `--until`: all must hold. Default limit: 10 s.

On their own, when no action is involved:

```bash
autonom ui wait --text "Welcome" --timeout-ms 15000
autonom ui wait --text "Loading" --gone
autonom ui wait --settled                           # the screen stopped changing
```

## Read the reply

```text
ui tap ok at 540,1210 (n14)
until met in 840 ms: text "Paid"
screen: com.example.app .PayActivity, 31 nodes, keyboard up
changes: +2 -1 ~0
+ n9 text "Paid" ...
- n9 button "Pay" ...
```

- `changes: none`: the tap did nothing. Wrong node, or the app ignored it.
- `! until_not_met` (exit 1): the input was sent. Read the screen (`autonom ui
  tree`), then decide. Do not send it again.
- `permission dialog "..."` or `alert dialog` on the `screen:` line: something
  is over the app. Autonom never dismisses it; tap the button you mean.
- `--screen` adds the whole outline; `--json` prints every key as JSON.

## Several known steps in one call

```bash
echo '[{"tapOn":{"text":"Profile"}},{"assertVisible":{"text":"Sign out"}},"back"]' | autonom ui do
```

Flow v1 steps, exact selectors, each assertion waits by itself, stops at the
first failing step. Use it for a path you have already walked once.

## When it refuses

- `no_matching_node`: read `ui tree`; on iOS try `--desc`.
- `ambiguous_selector`: add `--index N`, `--mode exact` or `--role button`.
- `element_offscreen`: swipe towards it, then tap.

More: the `mobile-screen` skill.
