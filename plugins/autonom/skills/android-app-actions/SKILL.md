---
name: android-app-actions
description: Design, implement, and validate Android deep links, verified App Links, launcher shortcuts, external intents, and Assistant-facing actions through one typed and sanitized routing model.
---

# Android App Actions

Use one routing model for every external entry into the app. Prefer a small
surface over exposing the full navigation graph.

## Steps

1. List one to three high-value destinations or verbs the user actually needs.
2. Map each to the thinnest surface that fits: App Link / deep link, static or
   dynamic shortcut, notification action, widget action, or Assistant capability.
3. Funnel all external entry points through a single typed destination or
   domain-command type.
4. Validate and sanitize host, path, and query parameters before navigation or
   business logic runs.
5. Exercise cold start, warm start, duplicate launch, back stack, bad input, and
   missing content on a real device.

## Smoke invocation

```bash
adb -s <serial> shell am start -W \
  -a android.intent.action.VIEW \
  -d "https://example.com/path?item=123" \
  <package>
```

For Flutter, keep URI parsing and destination mapping unit-testable in Dart;
manifest verification and system launch remain Android concerns.

## Hard rules

- Do not open the entire navigation tree to the outside world.
- Do not invent parallel routers for links, shortcuts, notifications, and widgets.
- Never trust inbound ids, hosts, paths, or query values.
- Compilation of XML or manifests is not proof — invoke the action on a target
  and confirm the destination.
