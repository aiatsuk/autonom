# Recipe: own a device session

```bash
autonom doctor                                  # only when something is missing: names the fix
autonom devices                                 # Android and iOS in one list, with `running`
autonom devices boot --avd Pixel_9              # an emulator that is not running yet

# Android
autonom session start --serial emulator-5554 --app-id com.example.app --launch --profile agent
# iOS Simulator (boots it when needed)
autonom session start --platform ios --udid <UDID> --app-id com.example.app --launch --profile agent
```

- `--profile agent` is what makes replies short and makes actions report the
  new screen. Always pass it for a session you drive.
- `--install PATH` installs an APK or `.app` first. `--label TEXT` names the
  session for people.
- One live session per device. A second device: add `--alongside`, then pass
  `--serial`/`--udid` (or `--session-id ID`) on every command so each goes to
  its own session. `autonom session list` shows the live ones.

## While it runs

```bash
autonom session launch com.example.app --fresh   # relaunch from a cold start
autonom session force-stop com.example.app
autonom session clear com.example.app            # wipe app data
autonom open 'myapp://orders/42'                 # deep link; Android names who took it
autonom session show                             # the session, its artifacts directory
```

After a launch, wait for the first screen in the same breath instead of
sleeping:

```bash
autonom ui wait --text "Sign in" --timeout-ms 20000
```

## Stop

```bash
autonom session stop        # stops the proxy, the log stream and the Android helper
```

Stop the session you started. A stopped session stays readable (`journal`,
`shots list`, `logs tail` with `--session-id ID`).

## When it refuses

- `ambiguous_target`: several devices; pass `--serial` or `--udid`.
- `session_already_active`: stop it, or add `--alongside` for another device.
- `control_held`: a person took the device in the Mobile Canvas. Do not retry
  in a loop; wait until `autonom control status` reads `free`.
- `autonom` not found: load the `autonom-setup` skill.

More: the `mobile-session` skill (install, permissions, location, push,
slim iOS simulators, files).
