---
name: android-emulator-browser
description: Mirror and lightly control an Android target or iOS Simulator in the visible Codex browser through the authenticated Autonom Mobile Canvas.
---

# Mobile Canvas browser

Stream one explicit target into a localhost browser for visual proof and light
control. Install and launch the app first. Canvas actions use the same Autonom
action pipeline and journal as CLI input; the browser is not a second runner.

## Launch

```bash
autonom canvas serve --platform android --serial <adb-serial> --transport auto

autonom canvas serve --platform ios --target <simulator-udid> --transport screencap
```

Open the printed fragment-token URL in the agent browser panel. The page
exchanges it for an HttpOnly cookie and removes the fragment. Leave the process
running and confirm frames advance before calling the setup successful.

## Transport modes

| Mode | Behavior |
| --- | --- |
| `auto` | Prefer `screenrecord` + ffmpeg H.264→MJPEG; fall back if unavailable |
| `screenrecord` | Require accelerated path; fail loudly when prerequisites missing |
| `screencap` | Multipart JPEG screenshots; no ffmpeg |

Android also exposes authenticated Annex-B H.264 at `/stream.h264`. iOS uses
public `simctl` screenshots with idb input and always chooses the screenshot
fallback; pixel-to-point mapping is handled by the Canvas status channel.

The UI shows active transport and frame health. Use Reconnect after rotation or
when device-side recording stops.

## Input surface

Tap, drag, wheel-as-swipe, Back/Home/Enter/Delete/D-pad/wake, and conservative
ASCII typing. Every action records `human`, `agent`, `replay`, or `system`
origin. The control endpoint supports pause, resume, takeover, and release.
Structural selection still belongs to Autonom `ui` commands.

## Security

Binds to `127.0.0.1` with a random token by default. Do not expose publicly.
Anyone with a forwarded port and the bootstrap token can drive the device.
`--no-auth` is for isolated local debugging only.

## Performance boundary

Good for iteration; not a measurement tool. Use scrcpy for high-fidelity manual
review and Macrobenchmark / Perfetto / Flutter profile mode for claims.

## Evidence to record

Side-panel screenshot, platform and target id, transport, package/activity,
variant, control owner, and the exact flow replayed.
