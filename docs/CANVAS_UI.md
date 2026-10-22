# Canvas UI components

Canvas is a compact desktop interface at every window width. Self-contained
HTML pages use standard HTML controls and serialized JavaScript, with no framework.

## Ownership

- canvas-ui.mjs: icon registry, markup/DOM factories, tabs, primitive styles,
  geometry, interaction states and motion tokens.
- canvas-dropdown.mjs: one controller for select-backed pickers and display
  presets; placement, keyboard navigation, dismissal and menu animation.
- canvas-theme-page.mjs: palette, preference persistence and dropdown
  installation. Includes the primitive stylesheet once.
- Page modules: domain layout, device actions, validation, loading and polling.
  Their existing IDs/classes remain compatibility hooks.

## Components

| Component | Shared entry point / classes | Contract |
| --- | --- | --- |
| Button | canvasButton, .btn | 32px, native button semantics |
| Small button | .btn.small | 28px, same interaction states |
| Primary / destructive | .primary, .danger | neutral primary; tinted destructive |
| Icon button | canvasIconButton, .icon / .ws-icon | label + tooltip; 32px |
| Icon | CANVAS_ICONS, canvasIcon, canvasIconData | one path registry, 24px viewbox |
| Field row | .field / .tools-field / .act-field | shared geometry and 6px gap |
| Group | .group / .tools-group / .act-group | 12px inset/gap, 10px radius |
| Tabs | canvasTabList, canvasTabPanel, bindCanvasTabs, canvasSelectTab | one panel, roving focus, arrows/Home/End |
| Workspace tabs | canvasTabKey | shared navigation; host retains rename/close |
| Dropdown | CanvasDropdown, installCanvasSelects | shared trigger/menu behavior |
| Panel | .side / .tools / .act | shared bounded overlay at <=760px |
| Status | .tools-msg / .act-msg | common spacing and error presentation |

Aliases identify where a component is used; they share one CSS definition.
Static markup uses the same component classes as DOM-created buttons.
Dock and tab-close buttons are explicit size variants. Device commands,
polling, validation and destructive confirmation remain domain behavior.

## Interaction rules

- One 120ms ease-out token for controls and menus. Press feedback never
  changes layout or scales controls. Reduced motion disables transitions.
- Keyboard focus has an accent outline; menu items use a neutral highlight
  and an outline in forced-colors mode.
- Native disabled blocks activation. aria-busy exposes pending operations;
  the action owner decides when an operation is allowed or disabled.
- Tabs wrap, skip disabled items and leave modified shortcuts to the host.
- Escape dismisses the innermost menu; Tab preserves normal focus order.
- DOM factories use textContent for device/user text. Markup helper attributes
  and panel bodies are trusted source strings only.
- Add icons to the registry, not inline SVG or independently encoded data URIs.
- Add variants centrally. Page CSS may arrange components, but should not
  override their base geometry or interaction states.

## Verification

    node --test tests/canvas-ui.test.mjs tests/canvas-dropdown.test.mjs tests/canvas-theme-page.test.mjs tests/canvas-tools-page.test.mjs tests/canvas-actions.test.mjs tests/canvas-workspace-page.test.mjs
    node --test --test-name-pattern='^(page:|display: the Android page)' tests/browser-scrcpy.test.mjs
    make check

Inspect both themes, narrow/wide panels, tabs, dropdown dismissal and disabled
controls. Presentation checks do not need device/network mutation.

## Workspace parity

The standalone device page and its embedded Workspace view use the same panel
markup and controls. Capture contains capture/gallery and Activity in either
host; app/deeplink controls belong to Tools. Volume stays in Inspector, and
input ownership controls are not part of the desktop UI. Workspace adds only
tabs, layouts, device identity and attachment/detachment. Device panels report
their actual open state to the shell so closing with Escape or a close button
does not leave the toolbar selected. Panel layout follows the tile viewport:
a side column above 760px and matching bounded overlays below it.

### Model-resolved device frames and exports

Device settings offer a Device frame on/off switch. Enabled frames resolve the
explicit Android display model's installed SDK skin; Device default and custom
sizes resolve the running AVD model. The same selected model is retained by captures.
Pixel Fold (open) uses 2208 × 1840 and its `default` skin; Pixel Fold (folded)
uses 1080 × 2092 and its `closed` skin. Both use 420 dpi. These presets change
screen dimensions and artwork; they do not emulate physical hinge/posture sensors.
On Android emulators, selecting Fold temporarily disables the originally active
Pixel model framework/SystemUI overlays so the host phone camera inset cannot
letterbox the Fold display after rotation. Selecting another preset (including
Device default) or stopping Canvas restores those exact overlays. Application
overlays and physical devices are not modified.
Missing skins, unsupported layouts, unavailable orientation and incompatible
proportions leave the screen unframed and show "Frame unavailable for <model>".
There is no Generic visual fallback and no model is inferred from aspect ratio.
The Device dimensions menu contains only Small phone, Pixel 10, Pixel Fold (open),
Pixel Fold (folded), and Device default. Pixel 10 uses 1080 × 2424 at 420 dpi and reports unavailable
when its original artwork is missing. Device default restores the native screen size and density. The legacy tablet
command remains an internal API option, outside the menu. Explicit model artwork requires no AVD query.
iOS resolves the Simulator device type and its installed DeviceKit body/mask.
Camera content already present in the framebuffer is not drawn a second time.

`autonom_lib/device_frames.py` owns `DeviceFrameDescriptor`: id, model, source,
bodySize, screenRect, layers, screenMask, orientations and fallbackReason.
The browser component and PNG/MP4 compositor consume that same geometry. Local
SDK artwork is not bundled: normalized variants are content-versioned under
`$XDG_CACHE_HOME/autonom/device-frames` (default `~/.cache`). The browser receives
image data, never local paths. Geometry changes request a descriptor once; tile
resizing and video frames do not add rendering operations to the stream.

Screenshot and Record snapshot the selected mode/model. Off leaves native captures
untouched. The compositor does not resize the captured screen; PNG has transparent
surrounds and H.264 MP4 has a white surround. `ffmpeg` and `ffprobe` are required.
Composition failures preserve the original capture and show a warning.

Verified: real Pixel 6 Pro portrait/landscape PNGs, asymmetric screen positioning,
rotated camera mask, on/off and incompatible proportions. Synthetic original-skin
video preserves 60 fps, frame count and duration. iPhone 17 Pro uses the exact
phone11.devicechrome identified by its Simulator profile, plus that profile's PDF
screen mask. Live Canvas, PNG and MP4 were inspected; the 1368x2730 body contains
the unchanged 1206x2622 screen at (81,54). DeviceKit PDF assets are rasterized with
system CoreGraphics once per source version; they are never bundled or distributed.

Open acceptance items: the existing native recorders are not rotation-safe during
an active recording. Android's codec can reject the device's resolution and its
`screenrecord` fallback changes the capture aspect ratio, which leaves capture unframed.
Replacing or extending that recording backend is needed before claiming full video
parity across rotations. A controlled browser FPS/latency/CPU comparison is also
still required; unit tests establish absence of per-frame frame-component work,
not a measured compositor performance result.

### Device language

Device settings uses the shared dropdown for the system language. Opening settings reads the current language from the selected device; an unlisted current language is retained in the options. Android applies changes through the shell configuration API and verifies them. iOS asks before restarting Simulator, preserves the region and other preferences, and refuses a restart during recording. This is independent of per-app language overrides.
