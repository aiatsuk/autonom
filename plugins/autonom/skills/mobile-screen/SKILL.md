---
name: mobile-screen
description: Understand and control what is on an Android or iOS screen for AI agents using compact accessibility/UI trees, semantic find/tap, gestures, screenshots, and key/text input via the Autonom CLI.
---

# Mobile Screen (Android + iOS Simulator)

## Purpose

Answer **what is on screen** and **act on it** without relying on pixel-only guessing.

1. Prefer a **compact UI tree** (UI Automator on Android, accessibility tree via idb on iOS).
2. Confirm with a **screenshot** when visual layout matters or the tree is ambiguous.
3. Act with **semantic selectors** first; coordinates only as fallback.

The compact node schema is identical on both platforms, so the same reasoning works
either way. What differs is **which field carries the visible label** — see below.

## CLI

```bash
# Compact tree (meaningful nodes by default)
python3 <autonom-root>/scripts/autonom.py ui tree
python3 <autonom-root>/scripts/autonom.py --platform ios --target <UDID> ui tree

# Find
python3 <autonom-root>/scripts/autonom.py ui find --text "Login" --mode contains     # Android
python3 <autonom-root>/scripts/autonom.py ui find --desc "Log In" --mode exact       # iOS
python3 <autonom-root>/scripts/autonom.py ui find --resource-id com.apple.settings.general

# Act
python3 <autonom-root>/scripts/autonom.py ui tap --desc "Continue"
python3 <autonom-root>/scripts/autonom.py ui tap --x 540 --y 1600
python3 <autonom-root>/scripts/autonom.py ui type "user@example.com"
python3 <autonom-root>/scripts/autonom.py ui swipe --from 200,600 --to 200,200
python3 <autonom-root>/scripts/autonom.py ui key KEYCODE_BACK      # Android
python3 <autonom-root>/scripts/autonom.py ui key HOME              # iOS

# Evidence
python3 <autonom-root>/scripts/autonom.py screenshot --out /tmp/screen.png
python3 <autonom-root>/scripts/autonom.py record start --name login
python3 <autonom-root>/scripts/autonom.py record stop

# Offline parse of a dump (CI / fixtures) — the platform is detected from the file
python3 <autonom-root>/scripts/autonom.py ui tree --dump tests/fixtures/ui_dump.xml
python3 <autonom-root>/scripts/autonom.py ui tree --dump tests/fixtures/idb_describe_all_sample.json
```

## Compact node schema (identical on both platforms)

```json
{
  "ref": "n5",
  "role": "button",
  "text": null,
  "desc": "General",
  "resource_id": "com.apple.settings.general",
  "bounds": [16, 380, 386, 432],
  "clickable": true,
  "enabled": true
}
```

## Where the visible label lives

**This is the one asymmetry that matters.**

| Platform | Visible label lands in | Select with |
| --- | --- | --- |
| Android | `text` (and `desc` for content-description) | `--text`, `--desc`, `--resource-id` |
| iOS | **`desc`** (from `AXLabel`); `text` comes from `AXValue` and is often `null` | **`--desc`**, `--resource-id` |

On iOS a control labelled "General" has `desc: "General"` and `text: null`, so
`ui find --text "General"` returns **zero matches**. Read the tree first and select
by `--desc`, or by `--resource-id` when the app sets accessibility identifiers.

## Gestures and keys by platform

| Verb | Android | iOS |
| --- | --- | --- |
| `ui tap`, `ui swipe`, `ui type` | yes | yes |
| `ui pinch`, `ui rotate`, `ui shake` | refused with `unsupported_on_platform` | yes |
| `ui key` | `KEYCODE_*` | `HOME`, `LOCK`, `SIDE_BUTTON`, `SIRI`, `APPLE_PAY`, or a numeric HID code |

iOS has no global Back button; tap the navigation bar's back control instead.

## Coordinate space

iOS accessibility frames and `idb ui tap` both use **points**, not pixels. Never
multiply by the display scale. A tap computed outside the target's reported screen
rectangle is refused with `coordinate_space_mismatch` rather than dispatched — on a
3x device a pixel mix-up would otherwise land silently in the wrong place and make
the agent report a defect that does not exist.

## Agent workflow

1. `session start` so the target and artifacts are explicit.
2. `ui tree` → read labels, roles, enabled state. Note which field holds the label.
3. `ui find` / `ui tap` for the next action.
4. `screenshot` after state changes that need visual proof — **compare before/after**;
   an exit code of 0 does not prove the screen changed.
5. Re-dump the tree after navigation; never reuse stale refs.
6. Report **measured** on-screen text and tree facts separately from hypotheses.

## Boundaries

- The iOS tree is the **accessibility** hierarchy, not the SwiftUI/UIKit view tree.
  Its quality depends on the app's labelling.
- A tree with fewer than three meaningful nodes is reported with
  `sparse_accessibility_tree`. That means "the app exposes little", not "the screen
  is empty" — add `Semantics` (Flutter) or `.accessibilityLabel` /
  `.accessibilityIdentifier` (SwiftUI), or fall back to a screenshot.
- When no element carries an identifier, `no_accessibility_identifiers` says so, so
  a zero-match `--resource-id` query is not mistaken for a missing control.
- System dialogs (permissions) need the same selectors or coordinates.
- Browser mirror (`android-emulator-browser`) is visual support, not a substitute
  for the tree.

## Related

- `mobile-session`
- `mobile-network` — correlate on-screen errors with HTTP traffic
- `android-debugger-agent`, `ios-debugger-agent`
