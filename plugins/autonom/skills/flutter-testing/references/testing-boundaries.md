# Flutter testing boundaries

- Unit and widget tests own deterministic Dart and rendering behavior.
- `integration_test` owns complete Flutter flows on a real target.
- System UI, permissions, notifications, settings, and some platform views may
  need Patrol, UI Automator, or Espresso in addition to Flutter integration tests.
- A platform channel needs Dart contract tests, native implementation tests, and
  at least one critical end-to-end device path.
- Goldens are environment-sensitive: pin fonts, locale, pixel ratio, surface
  size, and renderer before treating them as stable.

References:

- <https://docs.flutter.dev/testing/overview>
- <https://docs.flutter.dev/testing/integration-tests>
- <https://docs.flutter.dev/testing/plugins-in-tests>
