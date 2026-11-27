# Recipe: `! not on the fast path`

A reply that starts with this line was served the slow way. On Android that
means `uiautomator dump` and `adb input` (about 2 s a read) instead of
Autonom's on-device helper (tens of milliseconds). Everything still works, but
every wait and every action costs seconds. Fix it once instead of working
around it with screenshots.

```bash
autonom ui helper status      # is the helper running? its build, pid, idle time
autonom doctor                # are the JDK and Android SDK it is built from found?
```

## By the reason the line gives

| The line says | What to do |
| --- | --- |
| `javac not found`, no SDK, a failed build | Install a JDK (11 or newer) and the Android SDK build-tools, or set `JAVA_HOME` / `ANDROID_HOME`. The helper builds itself on the next call. |
| Another UiAutomation user holds the device (`uiautomation_busy`) | One tool at a time can hold UiAutomation. Appium, uiautomator2 or an instrumentation test is running on this device: stop it if it is yours, or ask its owner. Autonom tries the helper again after 60 s. |
| The helper did not answer, or could not read this screen | `autonom ui helper stop`, then repeat the command: a fresh helper starts. |
| The helper cannot run on this device | Old or unusual image. Work on the slow path, or use another emulator. |

Never stop another person's Appium or test run to get the fast path back: say
that the device is shared and what it costs.

## Giving the device to another tool

The helper holds UiAutomation until 2 minutes after its last call. Before
Appium, uiautomator2 or an instrumentation test uses the device:

```bash
autonom ui helper stop
```

`session stop` stops it too.

## Choosing a backend on purpose

```bash
autonom --ui-backend uiautomator ui tree   # never build, start or ask the helper
autonom --ui-backend helper ui tree        # refuse instead of falling back
```

`session start --ui-backend ...` stores the choice for the session.

## iOS

The line appears when idb could not serve the tree and the XCUITest runner
did. Check `autonom doctor`; the `ios-debugger-agent` skill has the recovery
steps.

## While on the slow path

- Prefer `--until` on the action over separate `ui wait` calls: fewer reads.
- Use `ui do` for known steps: one call, one final read.
- Do not replace tree reads with screenshots: an image costs more to read
  than the slow tree.
