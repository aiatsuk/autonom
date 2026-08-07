---
name: compose-performance-audit
description: Audit Compose performance from code and runtime evidence, focusing on invalidation scope, stability, lazy identity, effects, layout, draw work, images, and comparable profiling captures.
---

# Compose Performance Audit

Find unnecessary recomposition, layout, and draw work with code review plus
comparable runtime captures.

## Order of work

1. Name one screen and one user-visible symptom (jank, slow open, scroll hitch).
2. Trace state owners: which state reads invalidate which composables.
3. Check stability of parameters, `key` / identity in lazy lists, and effect
   scopes (`LaunchedEffect`, `DisposableEffect`, `SideEffect`).
4. Look for expensive work in composition, intrinsic measurements, excessive
   modifiers (clip / graphicsLayer / shadow), oversized images, and layout
   thrash.
5. Capture a comparable profile (Macrobenchmark, Perfetto, or Studio) on an
   explicit target and mode.
6. Apply the smallest fix; re-run the same flow.

## Code signals

- Unstable lambdas / objects crossing composition boundaries without remember.
- Reading broad state higher than needed.
- Lazy items without stable keys.
- `derivedStateOf` misused or missing where it would shrink invalidation.
- Layout that measures children twice without need.
- Images decoded at full resolution for small on-screen size.

## Runtime signals

Frame time, recomposition counts (when available), overdraw, and timeline
sections. Emulator numbers are directional; use a physical device for claims
users will feel.

## Report

Flow, device, mode, code findings, capture artifacts, fix, and before/after
metrics with units.
