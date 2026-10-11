# Edge-case kit reference

The 18 stable ids `autonom pretest check add --kind edge --case <id>` accepts.
Source of truth: `scripts/autonom_lib/pretest/kit.py` (`KIT`); this page mirrors
its ids, titles, risks and verbs (grouped by area here; `kit.py` order is the
table in `SKILL.md`) and adds how to set each case up, how to undo it, and what
to record. When the two disagree, `kit.py` and
`autonom pretest kit` win; fix this page.

`autonom` means `python3 <autonom-root>/scripts/autonom.py`. Every case is one
check: add it, `check begin`, set the case up, do the ticket's action, look,
record, undo the setup, `check end`. A case that cannot be set up on the target
ends `not_covered` with the reason in `--note`; never fake it.

The pack's `kit` table marks a case covered only when a check naming it actually
began. Cases nobody named are listed as not covered. That is expected: pick the
cases the diff can plausibly break, not all 18.

## Network

### `offline` — No network

- Risk: the screen hangs, crashes or shows stale data as if fresh when the device is offline.
- Verbs: `simulator network offline`, `simulator network online`.
- Platform: Android emulator only (Wi-Fi and mobile data off). iOS refuses
  `simulator network` with `unsupported_capability`.
- Look for: an offline message or cached data marked as such, no endless
  spinner, recovery after `online` without a restart.

### `slow` — Slow network

- Risk: spinners never end, taps fire twice, or timeouts are missing on a slow link.
- Verbs: `simulator network shape --value speed=edge --value delay=edge`, then
  restore with `simulator network shape --value speed=full --value delay=none`.
- Platform: Android emulator only. Shaping is the only way to slow traffic
  today; a mock cannot delay its answer.
- Look for: a loading state, buttons disabled while a request runs, no
  duplicate request in `network requests list`.

### `server-error` — Server error (5xx)

- Risk: a 500 response is swallowed, shown as empty data, or crashes the screen.
- Verbs: `network start`, `network attach` (both with `--i-understand-mitm`),
  `network mock add --match '<url glob>' --status 500`, then
  `network mock remove <id>`.
- Platform: both; iOS capture has the limits described in `mobile-network`.
- Look for: an error message and a retry, not an empty list. Screenshots taken
  under the mock are flagged as mocked data; say so in the finding.

### `timeout` — Request never answers

- Risk: a request that never completes leaves the screen stuck with no retry.
- Verbs: `network start`, `network attach`,
  `simulator network shape --value speed=gsm --value delay=gprs`,
  `network requests list` (look at `duration_ms` of the slow requests); restore
  with `simulator network shape --value speed=full --value delay=none`.
- Platform: the shaping step is Android emulator only; on iOS end the check
  `not_covered`.
- Current limits: shaping slows every request, it cannot make one hang. A mock
  cannot delay or hold its answer, and a request that never completes is not
  recorded (the capture writes a request when it finishes, so neither
  `network requests list` nor `pretest health` can show it). Do not build this
  case on mock delays or on spotting unfinished requests.
- Look for: a loading state that ends, a timeout message and a way out, in
  screenshots. When the evidence cannot show a real hang, end the check
  `inconclusive` with these limits in `--note`. Mock delays and recording of
  unfinished requests are a planned follow-up.

## App lifecycle

### `background-resume` — Background and resume

- Risk: state, timers or input are lost when the app goes to the background and returns.
- Verbs: `ui key HOME`, `session launch <app-id>` (brings the app back without
  clearing it).
- Platform: both.
- Look for: the same screen, typed text and scroll position after return.

### `process-death` — Process death and restore

- Risk: the app loses the user's place or data after the system kills the process.
- Verbs: `session force-stop <app-id>`, `session launch <app-id>`.
- Platform: both (iOS terminates the app).
- Look for: a sensible restart point, no lost saved data, no crash on start.

### `rotation` — Rotation

- Risk: layout breaks or input is lost when the screen rotates (Android).
- Verbs: a flow with `setOrientation: landscape`, then one with
  `setOrientation: portrait`, run with `flow run`.
- Platform: Android only (`setOrientation` is an Android-only flow command).
- Look for: clipped or overlapping layout, lost input, a screen reset.

### `deep-link` — Deep link

- Risk: a link opens the wrong screen, loses its parameters, or fails when the app is cold.
- Verbs: `open '<app link>'` (warm), `session force-stop <app-id>`,
  `open '<app link>'` (cold).
- Platform: both. On Android the handler is checked against `--allow-app`; a
  link no app has verified opens the system app chooser and is refused
  (`app_not_allowed`, `chooser: true`): use the app's own scheme or a verified
  App Link. On iOS `open` is counted but the handler cannot be checked.
- Look for: the right screen with the right item, in both warm and cold starts.

## Display and system settings

### `dark-mode` — Dark mode

- Risk: text or icons become unreadable in the dark color scheme.
- Verbs: `simulator appearance dark`, `simulator appearance light`.
- Platform: both.
- Look for: unreadable text, invisible icons, hard-coded white backgrounds.

### `large-text` — Large text

- Risk: text is clipped or overlaps at large accessibility font sizes.
- Verbs: `simulator text-size 1.3` (Android font scale),
  `simulator text-size accessibility-extra-large` (iOS), restore with
  `simulator text-size 1.0` (Android) or `large` (iOS).
- Platform: both, with different values.
- Look for: cut text, overlapping rows, buttons pushed off screen.

### `locale` — Another locale

- Risk: untranslated strings, wrong number or date formats, or overflow in longer languages.
- Verbs: `simulator keyboard pin --value locale=<locale>`, `simulator keyboard reset`.
- Platform: iOS only (`simulator keyboard` is refused on Android).
- Look for: untranslated strings, number and date formats, longer text overflowing.

### `permission-denied` — Permission denied

- Risk: the feature crashes or dead-ends when a permission is refused.
- Verbs: `permissions revoke <service> <app-id>`, `permissions reset <service> <app-id>`.
- Platform: both (an iOS privacy service, or an Android permission name).
- Look for: an explanation and a path to settings, no crash, no silent dead end.

## Input

### `empty-input` — Empty input

- Risk: submitting an empty field is accepted, crashes, or shows no message.
- Verbs: `ui tap --text '<field>'`, `ui tap --text '<submit>'`.
- Look for: a validation message, the submit not sent (`network requests list`).

### `long-input` — Very long input

- Risk: long text overflows the layout, is cut silently, or breaks the request.
- Verbs: `ui tap --text '<field>'`, `ui type '<500 characters>'`.
- Look for: overflow, silent truncation, a failed request.

### `emoji-input` — Emoji input

- Risk: emoji are dropped, garble the text, or break length counting.
- Verbs: `ui tap --text '<field>'`, `ui type '<text with emoji>'`.
- Look for: dropped or broken characters, a wrong character counter.

### `rtl-input` — Right-to-left input

- Risk: Arabic or Hebrew text is mirrored wrongly or mixes badly with numbers.
- Verbs: `ui tap --text '<field>'`, `ui type '<right-to-left text>'`.
- Look for: wrong alignment, numbers in the wrong order, a broken cursor.

## Interaction timing

### `double-tap` — Double tap

- Risk: a fast second tap submits twice, opens two screens, or charges twice.
- Verbs: a flow with `doubleTapOn: <button>` run with `flow run`, or
  `ui tap --text '<button>'` twice, quickly.
- Look for: two identical requests, two screens pushed, a duplicate item.
  Never double-tap anything that pays or buys; the forbidden-text guard refuses
  it anyway.
- During a pre-test every point tap reads a fresh UI tree first, so taps can be
  slower than the race. Pass only with an observed request count; otherwise
  end the check `inconclusive` with that reason in `--note`.

### `fast-back` — Back while loading

- Risk: going back during a load crashes or leaves a callback updating a dead screen.
- Verbs: `ui tap --text '<action>'`, `ui key BACK`. (`ui find` does not wait; to
  check the previous screen after BACK, run `ui wait --settled` first.)
- Platform: `ui key BACK` is Android only; on iOS use the screen's own back
  control (`ui tap --desc 'Back'`).
- Look for: a crash (`pretest health`), an error toast on the previous screen.

## Recording a case

- `--why` on the check says why this case matters for this diff.
- A `--kind ticket` check whose acceptance point is one of these cases takes
  `--case <id>` too, so the kit table counts it.
- Every action of the case is counted against the budget, including setup.
- A finding names the case's observed behaviour in `--actual` and the expected
  behaviour in `--expected`, with a screenshot and, for network cases, the
  `request:<id>` ref.
- Undo the setup inside the check, before `check end`, so the next check starts
  from a clean device.
