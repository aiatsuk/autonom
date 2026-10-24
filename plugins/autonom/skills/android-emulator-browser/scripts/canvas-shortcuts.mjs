/**
 * Simulator.app-style keyboard shortcuts of the Canvas device page (QW-01..QW-03): a pure chord
 * map shared by the page script (inlined with Function.prototype.toString, so the functions use
 * no imports and no names but their parameters and SHORTCUTS) and by the server-rendered list.
 *
 * The primary modifier is Cmd on macOS and Ctrl elsewhere; the other one, Alt (the AltGr layer
 * and the workspace's Ctrl+Alt hotkeys) and AltGraph never match. Keys are matched by
 * KeyboardEvent.code, so a chord works the same on every keyboard layout.
 */

/**
 * Every chord: `action` names what it does, `code` the physical key, `shift` whether Shift is
 * part of the chord, `match` the platforms it acts on and `list` those whose Shortcuts list
 * shows it. Rotate matches on iOS too, where the page says it is not available yet.
 */
export const SHORTCUTS = Object.freeze([
  Object.freeze({ action: "home", label: "Home", code: "KeyH", shift: true, match: ["android", "ios"], list: ["android", "ios"] }),
  Object.freeze({ action: "lock", label: "Lock", code: "KeyL", shift: false, match: ["android", "ios"], list: ["android", "ios"] }),
  Object.freeze({ action: "volume-up", label: "Volume up", code: "ArrowUp", shift: false, match: ["android", "ios"], list: ["android", "ios"] }),
  Object.freeze({ action: "volume-down", label: "Volume down", code: "ArrowDown", shift: false, match: ["android", "ios"], list: ["android", "ios"] }),
  Object.freeze({ action: "rotate", label: "Rotate left", code: "ArrowLeft", shift: false, match: ["android", "ios"], list: ["android"] }),
  Object.freeze({ action: "rotate", label: "Rotate right", code: "ArrowRight", shift: false, match: ["android", "ios"], list: ["android"] }),
  Object.freeze({ action: "screenshot", label: "Screenshot", code: "KeyS", shift: false, match: ["android", "ios"], list: ["android", "ios"] }),
  Object.freeze({ action: "record", label: "Start or stop recording", code: "KeyR", shift: false, match: ["android", "ios"], list: ["android", "ios"] }),
  Object.freeze({ action: "back", label: "Back", code: "Backspace", shift: false, match: ["android"], list: ["android"] }),
  Object.freeze({ action: "recents", label: "Recent apps", code: "KeyO", shift: false, match: ["android"], list: ["android"] }),
]);

/** The `data-system` operation of the device button each device action presses. */
export const SHORTCUT_SYSTEM_OPS = Object.freeze({
  home: "home",
  lock: "power",
  "volume-up": "volume-up",
  "volume-down": "volume-down",
  rotate: "rotate",
  back: "back",
  recents: "app-switch",
});

/** Whether a navigator-like object describes macOS (or iPadOS/iOS with a keyboard): Cmd is the modifier there. */
export function isMacPlatform(nav) {
  if (!nav || typeof nav !== "object") return false;
  const data = nav.userAgentData && typeof nav.userAgentData.platform === "string" ? nav.userAgentData.platform : "";
  const legacy = typeof nav.platform === "string" ? nav.platform : "";
  return /mac|iphone|ipad|ipod/i.test(data || legacy);
}

/**
 * The action of a keydown `event` on a `platform` ("android" or "ios") page, or null when the
 * chord is not a shortcut there: the primary modifier alone (Cmd on macOS, Ctrl elsewhere),
 * Shift exactly as the chord says, and never Alt, AltGraph or the other modifier.
 */
export function shortcutFor(event, platform, isMac, table = SHORTCUTS) {
  if (!event || typeof event.code !== "string") return null;
  const primary = isMac ? event.metaKey : event.ctrlKey;
  const other = isMac ? event.ctrlKey : event.metaKey;
  if (!primary || other || event.altKey) return null;
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) return null;
  for (const entry of table) {
    if (entry.code === event.code && entry.shift === Boolean(event.shiftKey) && entry.match.includes(platform)) return entry.action;
  }
  return null;
}

/** The chord of a SHORTCUTS entry as shown in tooltips and the list: "⌘⇧H" on macOS, "Ctrl+Shift+H" elsewhere. */
export function shortcutLabel(entry, isMac) {
  const glyphs = { ArrowUp: ["↑", "Up"], ArrowDown: ["↓", "Down"], ArrowLeft: ["←", "Left"], ArrowRight: ["→", "Right"], Backspace: ["⌫", "Backspace"] };
  const key = glyphs[entry.code] || [entry.code.replace(/^Key/, ""), entry.code.replace(/^Key/, "")];
  return isMac ? "⌘" + (entry.shift ? "⇧" : "") + key[0] : "Ctrl+" + (entry.shift ? "Shift+" : "") + key[1];
}

/** The SHORTCUTS entries the Shortcuts list of a `platform` page shows, with their indexes. */
export function listedShortcuts(platform, table = SHORTCUTS) {
  return table.map((entry, index) => ({ entry, index })).filter(({ entry }) => entry.list.includes(platform));
}

/** The chords of one action, joined for a tooltip: "⌘S", or "⌘← or ⌘→" for rotate. */
export function actionChords(action, platform, isMac, table = SHORTCUTS) {
  return listedShortcuts(platform, table).filter(({ entry }) => entry.action === action)
    .map(({ entry }) => shortcutLabel(entry, isMac)).join(" or ");
}
