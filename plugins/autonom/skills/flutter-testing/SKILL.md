---
name: flutter-testing
description: Design and run a layered Flutter test strategy across unit, widget, golden, integration, platform-channel, and native-UI tests with focused commands and reproducible device setup.
---

# Flutter Testing

Pick the narrowest layer that proves the behavior.

| Layer | Owns |
| --- | --- |
| unit | pure mapping, validation, state, repositories |
| widget | UI states, semantics, forms, callbacks without a full device |
| golden | visual contracts with pinned fonts/locale/size/renderer |
| integration_test | full Flutter flows on a real target |
| native | Kotlin/plugin implementation |
| Patrol / Espresso / UI Automator | permissions, notifications, platform views |

```bash
python3 <marketplace-root>/scripts/flutter_exec.py --root . -- --version
```

Read `references/testing-boundaries.md` when Flutter and native UI meet.

## Loop

1. Read the change, acceptance criteria, nearby tests, past flakes.
2. Draft happy / boundary / failure / lifecycle / a11y / platform cases.
3. Implement highest-signal missing tests first.
4. Run, refine once from failures, then widen the suite.

## Commands

```bash
flutter test test/path/to/test.dart
flutter test --plain-name "specific behavior"
flutter test integration_test/app_test.dart -d <device-id>
```

Use `flutter drive` only when the project’s driver/lab requires it.

## Stability

Prefer type/text/semantics/key finders. Avoid unbounded `pumpAndSettle` when
animations or streams never idle. Control clocks, randomness, network, and
storage.

## Platform channels

Fake the Dart side, unit-test native handlers, and keep one device path for
each critical channel.
