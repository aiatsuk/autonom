import assert from "node:assert/strict";
import test from "node:test";
import { Script, createContext } from "node:vm";

import {
  SHORTCUTS,
  SHORTCUT_SYSTEM_OPS,
  actionChords,
  isMacPlatform,
  listedShortcuts,
  shortcutFor,
  shortcutLabel,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-shortcuts.mjs";

/** A keydown of `code` with the modifiers given (all others off). */
function key(code, modifiers = {}) {
  return { code, key: code, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, repeat: false, ...modifiers };
}

// Every chord of QW-01: [code, shift, action, Android only].
const CHORDS = [
  ["KeyH", true, "home", false],
  ["KeyL", false, "lock", false],
  ["ArrowUp", false, "volume-up", false],
  ["ArrowDown", false, "volume-down", false],
  ["ArrowLeft", false, "rotate", false],
  ["ArrowRight", false, "rotate", false],
  ["KeyS", false, "screenshot", false],
  ["KeyR", false, "record", false],
  ["Backspace", false, "back", true],
  ["KeyO", false, "recents", true],
];

test("every chord maps on both platforms with Cmd on macOS and Ctrl elsewhere", () => {
  for (const isMac of [true, false]) {
    const primary = isMac ? { metaKey: true } : { ctrlKey: true };
    for (const platform of ["android", "ios"]) {
      for (const [code, shift, action, androidOnly] of CHORDS) {
        const expected = androidOnly && platform === "ios" ? null : action;
        assert.equal(shortcutFor(key(code, { ...primary, shiftKey: shift }), platform, isMac), expected,
          `${platform} ${isMac ? "mac" : "other"} ${code}`);
      }
    }
  }
});

test("the other modifier, Alt, AltGraph, a different Shift or no modifier never match", () => {
  for (const [code, shift] of CHORDS) {
    // Cmd on macOS only: Ctrl there, and the Windows/Meta key elsewhere, are not the modifier.
    assert.equal(shortcutFor(key(code, { ctrlKey: true, shiftKey: shift }), "android", true), null, `mac Ctrl ${code}`);
    assert.equal(shortcutFor(key(code, { metaKey: true, shiftKey: shift }), "android", false), null, `other Meta ${code}`);
    // Both together, Alt (the workspace's Ctrl+Alt hotkeys and AltGr) and AltGraph.
    assert.equal(shortcutFor(key(code, { metaKey: true, ctrlKey: true, shiftKey: shift }), "android", true), null);
    assert.equal(shortcutFor(key(code, { metaKey: true, altKey: true, shiftKey: shift }), "android", true), null);
    assert.equal(shortcutFor(key(code, { ctrlKey: true, altKey: true, shiftKey: shift }), "android", false), null);
    assert.equal(shortcutFor(key(code, { ctrlKey: true, shiftKey: shift, getModifierState: (name) => name === "AltGraph" }),
      "android", false), null);
    // Shift must be exactly as the chord says.
    assert.equal(shortcutFor(key(code, { metaKey: true, shiftKey: !shift }), "android", true), null, `shift flipped ${code}`);
    assert.equal(shortcutFor(key(code, { shiftKey: shift }), "android", true), null, `no modifier ${code}`);
  }
  // Keys outside the map and malformed events.
  for (const code of ["KeyA", "KeyC", "KeyV", "KeyX", "KeyZ", "Digit1", "Enter", "Escape", "Tab"]) {
    assert.equal(shortcutFor(key(code, { metaKey: true }), "android", true), null, code);
    assert.equal(shortcutFor(key(code, { ctrlKey: true }), "ios", false), null, code);
  }
  assert.equal(shortcutFor(null, "android", true), null);
  assert.equal(shortcutFor({ metaKey: true }, "android", true), null);
  assert.equal(shortcutFor(key("KeyH", { metaKey: true, shiftKey: true }), "windows-phone", true), null);
});

test("keys are physical: a Cyrillic layout still has Cmd+S", () => {
  assert.equal(shortcutFor({ ...key("KeyS", { metaKey: true }), key: "ы" }, "android", true), "screenshot");
});

test("device actions press the buttons' system operations", () => {
  assert.deepEqual(SHORTCUT_SYSTEM_OPS, { home: "home", lock: "power", "volume-up": "volume-up", "volume-down": "volume-down",
    rotate: "rotate", back: "back", recents: "app-switch" });
  const actions = new Set(SHORTCUTS.map((entry) => entry.action));
  for (const action of actions) assert.ok(action in SHORTCUT_SYSTEM_OPS || action === "screenshot" || action === "record", action);
});

test("labels: glyphs on macOS, words elsewhere; 10 listed on Android and 6 on iOS", () => {
  const mac = SHORTCUTS.map((entry) => shortcutLabel(entry, true));
  assert.deepEqual(mac, ["⌘⇧H", "⌘L", "⌘↑", "⌘↓", "⌘←", "⌘→", "⌘S", "⌘R", "⌘⌫", "⌘O"]);
  const other = SHORTCUTS.map((entry) => shortcutLabel(entry, false));
  assert.deepEqual(other, ["Ctrl+Shift+H", "Ctrl+L", "Ctrl+Up", "Ctrl+Down", "Ctrl+Left", "Ctrl+Right", "Ctrl+S", "Ctrl+R",
    "Ctrl+Backspace", "Ctrl+O"]);
  assert.equal(listedShortcuts("android").length, 10);
  assert.deepEqual(listedShortcuts("ios").map(({ entry }) => entry.action),
    ["home", "lock", "volume-up", "volume-down", "screenshot", "record"]);
  assert.deepEqual(listedShortcuts("ios").map(({ index }) => index), [0, 1, 2, 3, 6, 7]);
  assert.equal(actionChords("home", "android", true), "⌘⇧H");
  assert.equal(actionChords("rotate", "android", true), "⌘← or ⌘→");
  assert.equal(actionChords("rotate", "ios", true), "", "iOS lists no rotate chord");
  assert.equal(actionChords("back", "ios", false), "");
  assert.equal(actionChords("record", "ios", false), "Ctrl+R");
});

test("macOS detection: User-Agent Client Hints first, then navigator.platform", () => {
  assert.equal(isMacPlatform({ platform: "MacIntel" }), true);
  assert.equal(isMacPlatform({ platform: "iPad" }), true);
  assert.equal(isMacPlatform({ userAgentData: { platform: "macOS" }, platform: "" }), true);
  assert.equal(isMacPlatform({ userAgentData: { platform: "Windows" }, platform: "MacIntel" }), false);
  assert.equal(isMacPlatform({ platform: "Win32" }), false);
  assert.equal(isMacPlatform({ platform: "Linux x86_64" }), false);
  assert.equal(isMacPlatform({}), false);
  assert.equal(isMacPlatform(null), false);
  assert.equal(isMacPlatform(undefined), false);
});

test("the functions work inlined in the page with only SHORTCUTS beside them", () => {
  const source = [
    `const SHORTCUTS=${JSON.stringify(SHORTCUTS)};`,
    isMacPlatform.toString(), shortcutFor.toString(), shortcutLabel.toString(), listedShortcuts.toString(), actionChords.toString(),
    `result=[isMacPlatform({platform:"MacIntel"}),shortcutFor({code:"KeyH",metaKey:true,shiftKey:true},"ios",true),` +
      `shortcutFor({code:"KeyO",ctrlKey:true},"android",false),shortcutLabel(SHORTCUTS[8],true),actionChords("rotate","android",false)];`,
  ].join("\n");
  assert.doesNotMatch(source, /`/, "no backticks: the page script is a template literal");
  const sandbox = { result: null };
  new Script(source).runInContext(createContext(sandbox));
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.result)), [true, "home", "recents", "⌘⌫", "Ctrl+Left or Ctrl+Right"]);
});
