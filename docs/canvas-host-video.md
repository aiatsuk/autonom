# macOS host video capture

Canvas can opt in to macOS VideoToolbox H.264 capture for an Android emulator. Without an explicit configuration, existing Android scrcpy behavior is unchanged. The feature is limited to macOS and requires the Android emulator, Xcode command-line tools and a hardware VideoToolbox encoder.

Create `~/.config/autonom/host-video.json` with a unique AVD name, emulator serial and local gRPC port:

```json
{
  "version": 1,
  "devices": [
    { "avd": "My_AVD", "serial": "emulator-5556", "grpcPort": 8556 }
  ]
}
```

Starting that AVD through `autonom devices boot` enables its token-protected loopback gRPC endpoint. The endpoint provides emulator control as well as screenshots; configure it only when that access is acceptable. Canvas verifies the AVD, serial, port and live emulator discovery record, then uses complete owned RGBA frames. If capture fails, Canvas falls back to Android encoding. The Swift helper is compiled into a local cache when first needed.

Optional `"manageCutout": true` suppresses the built-in display-cutout drawing while host capture is attached, allowing another model frame to be shown without a duplicate cutout. It requires root on the emulator and changes a single Android resource overlay; it is disabled on detach, shutdown or capture failure. Leave it unset if the emulator or environment does not permit that change.

Resolution and rotation changes recreate the hardware encoder as needed. This path does not guarantee 60 FPS: actual output depends on Android rendering, host load and capture throughput. During a focus-profile transition the existing control session can briefly restart.

Run `node --test tests/host-video.test.mjs tests/canvas-display-chrome.test.mjs` and `python3 -m unittest tests.test_host_video tests.test_host_video_native` for focused checks.
