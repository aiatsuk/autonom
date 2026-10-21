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
| Group | .group / .tools-group / .act-group | 12px inset/radius, 10px outer gap |
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
