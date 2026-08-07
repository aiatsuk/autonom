---
name: android-emulator-browser
description: Mirror and lightly control an Android emulator or device inside the visible Codex browser using a token-protected localhost MJPEG bridge with accelerated and fallback transports.
---

# Android Emulator Browser

Stream the device into a localhost MJPEG bridge for visual proof and light
control. Install and launch the app first, then start the bridge on one serial.

## Launch

```bash
node <skill-root>/scripts/android-emulator-browser.mjs \
  --serial <adb-serial> \
  --transport auto
```

Open the printed tokenized URL in the agent browser panel. Leave the process
running and confirm frames are advancing before calling the setup successful.

## Transport modes

| Mode | Behavior |
| --- | --- |
| `auto` | Prefer `screenrecord` + ffmpeg H.264→MJPEG; fall back if unavailable |
| `screenrecord` | Require accelerated path; fail loudly when prerequisites missing |
| `screencap` | Multipart JPEG screenshots; no ffmpeg |

The UI shows active transport and frame health. Use Reconnect after rotation or
when device-side recording stops.

## Input surface

Tap, long-press, drag, wheel-as-swipe, Back/Home/Enter/Delete/D-pad/wake, and
conservative ASCII typing. Structural selection still belongs to UI Automator /
Autonom `ui` commands — this bridge is visual proof, not a test runner.

## Security

Binds to `127.0.0.1` with a random token by default. Do not expose publicly.
Anyone with a forwarded port and the token can drive the device. `--no-auth`
is for isolated local debugging only.

## Performance boundary

Good for iteration; not a measurement tool. Use scrcpy for high-fidelity manual
review and Macrobenchmark / Perfetto / Flutter profile mode for claims.

## Evidence to record

Side-panel screenshot, adb serial, transport, package/activity, variant, and
the exact flow replayed.
