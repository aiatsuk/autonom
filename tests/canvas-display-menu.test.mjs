// Unit tests of canvas-display-menu.mjs (QW-04, QW-05): the Display group's markup, the stored
// choice, and its page script run in a node:vm sandbox with a fake control socket, a fake
// browser storage and the few page helpers it uses. No browser, device or server is involved.
import assert from "node:assert/strict";
import test from "node:test";
import { createContext, Script } from "node:vm";

import {
  DISPLAY_RECONNECT_MS,
  DISPLAY_STORAGE_PREFIX,
  displayFpsChoices,
  displayMenuMarkup,
  displayMenuScript,
  displayStorageKey,
  fpsLabel,
  parseStoredChoice,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-display-menu.mjs";

test("markup: Frame rate 60/30/15 (iOS also On change) and Size Full/Half, disabled until the stream runs", () => {
  assert.deepEqual(displayFpsChoices("android"), [60, 30, 15]);
  assert.deepEqual(displayFpsChoices("ios"), [60, 30, 15, 0]);
  assert.equal(fpsLabel(0), "On change");
  assert.equal(fpsLabel(15), "15 fps");
  const android = displayMenuMarkup("android");
  const values = (html, id) => [...html.match(new RegExp(`<select id="${id}"[^>]*>([\\s\\S]*?)</select>`))[1]
    .matchAll(/<option value="([^"]*)"( disabled)?>([^<]*)</g)].map(([, value, disabled, label]) => [value, label, Boolean(disabled)]);
  assert.match(android, /<h3 class="canvas-settings-group" id="display-group-title">Display<\/h3>/);
  assert.deepEqual(values(android, "display-fps"), [["60", "60 fps", false], ["30", "30 fps", false], ["15", "15 fps", false]]);
  assert.deepEqual(values(android, "display-size"), [["full", "Full", false], ["half", "Half", false]]);
  assert.match(android, /<select id="display-fps" aria-label="Frame rate" disabled>/);
  assert.match(android, /<select id="display-size" aria-label="Stream size" disabled>/);
  const ios = displayMenuMarkup("ios");
  assert.deepEqual(values(ios, "display-fps").map(([value, label]) => [value, label]),
    [["60", "60 fps"], ["30", "30 fps"], ["15", "15 fps"], ["0", "On change"]]);
  // An --fps outside the menu is shown, as the rate in use, but cannot be picked.
  assert.deepEqual(values(displayMenuMarkup("android", { fps: 24, fpsExplicit: true }), "display-fps")[0], ["24", "24 fps (--fps)", true]);
  assert.equal(values(displayMenuMarkup("android", { fps: 30, fpsExplicit: true }), "display-fps").length, 3);
  assert.equal(values(displayMenuMarkup("android", { fps: 24 }), "display-fps").length, 3, "a default --fps adds nothing");
});

test("stored choices: one key per device; only values the platform may use are read back", () => {
  assert.equal(DISPLAY_STORAGE_PREFIX, "autonom.canvas.display.");
  assert.equal(displayStorageKey("ios~ABC"), "autonom.canvas.display.ios~ABC");
  assert.deepEqual(parseStoredChoice(JSON.stringify({ fps: 15, size: "half" }), "android"), { fps: 15, size: "half" });
  assert.deepEqual(parseStoredChoice(JSON.stringify({ fps: 0, size: "full", extra: 1 }), "ios"), { fps: 0, size: "full" });
  // fps null: a Size picked alone, the stream keeps its own frame rate.
  assert.deepEqual(parseStoredChoice(JSON.stringify({ fps: null, size: "half" }), "android"), { fps: null, size: "half" });
  for (const [text, platform] of [[JSON.stringify({ fps: 0, size: "full" }), "android"], [JSON.stringify({ fps: 45, size: "full" }), "ios"],
    [JSON.stringify({ fps: 15, size: "quarter" }), "ios"], [JSON.stringify({ fps: "15", size: "full" }), "ios"], ["{", "ios"], ["null", "ios"],
    ["", "ios"], [null, "android"], [JSON.stringify({ size: "half" }), "android"]]) {
    assert.equal(parseStoredChoice(text, platform), null, `${text} ${platform}`);
  }
});

/** A <select> as the script uses it: value, disabled, change listeners. */
function fakeSelect(value) {
  const listeners = [];
  return {
    value, disabled: true,
    addEventListener: (type, listener) => { if (type === "change") listeners.push(listener); },
    change(next) { this.value = next; for (const listener of listeners) listener({ type: "change" }); },
  };
}

/**
 * The Display script of `platform` for device `id`, run over fakes: `storage` is the browser
 * storage (a Map; `blocked` makes every access throw), `sent` the control messages, `clock`
 * the performance clock; `run` evaluates code inside the page.
 */
function displayPage({ platform = "android", id = `${platform}~x`, stored = null, blocked = false, socketOpen = true, fpsShown = "60" } = {}) {
  const storage = new Map();
  if (stored) storage.set(DISPLAY_STORAGE_PREFIX + id, JSON.stringify(stored));
  const nodes = { "display-fps": fakeSelect(fpsShown), "display-size": fakeSelect("full"), "display-stream": { textContent: "" } };
  const sent = [];
  const clock = { now: 1000 };
  const guard = (work) => (...args) => { if (blocked) throw new Error("storage is blocked"); return work(...args); };
  const sandbox = {
    PLATFORM: platform, DEVICE_ID: id,
    $: (name) => nodes[name] ?? null,
    document: { activeElement: null },
    performance: { now: () => clock.now },
    localStorage: {
      getItem: guard((key) => storage.get(key) ?? null),
      setItem: guard((key, value) => storage.set(key, String(value))),
      removeItem: guard((key) => storage.delete(key)),
    },
    view: { session: "streaming", status: null, note: "" },
    stats: { lastFrameAt: 0 },
    controlSocket: socketOpen ? { readyState: 1 } : null,
    sendControl(message) {
      if (!sandbox.controlSocket || sandbox.controlSocket.readyState !== 1) return false;
      sent.push(JSON.parse(JSON.stringify(message)));
      return true;
    },
    note(message) { sandbox.view.note = message; },
    // Timers are kept, not run: a test fires them itself.
    setTimeout(callback, ms) { timers.push({ callback, ms }); return timers.length; },
    renderChrome() { chromeRenders.push(sandbox.view.note); },
  };
  const timers = [];
  const chromeRenders = [];
  const page = createContext(sandbox);
  new Script(displayMenuScript()).runInContext(page);
  const run = (code) => new Script(code).runInContext(page);
  return { sandbox, nodes, storage, sent, clock, run, timers, chromeRenders, key: DISPLAY_STORAGE_PREFIX + id };
}

test("page: a choice is sent, stored per device and shown; the same choice again sends nothing", () => {
  const page = displayPage({ platform: "ios", id: "ios~A" });
  page.run("syncStreamMenu()");
  assert.equal(page.nodes["display-fps"].disabled, false, "enabled while the control socket is open");
  // QW-04-S01 (page side): Frame rate 15 goes out as one stream message.
  page.nodes["display-fps"].change("15");
  assert.deepEqual(page.sent, [{ t: "stream", fps: 15, size: "full" }]);
  assert.deepEqual(JSON.parse(page.storage.get("autonom.canvas.display.ios~A")), { fps: 15, size: "full" });
  page.nodes["display-fps"].change("15");
  assert.equal(page.sent.length, 1);
  // On change is fps 0 on iOS.
  page.nodes["display-fps"].change("0");
  assert.deepEqual(page.sent.at(-1), { t: "stream", fps: 0, size: "full" });
  // The answer's settings show until /status reports them; then /status wins.
  page.run('onStreamReply({t:"stream",ok:true,choice:{fps:0,size:"full"},applied:{fps:0,max_size:null,scale_factor:1}})');
  assert.equal(page.nodes["display-stream"].textContent, "Streaming at frames on change");
  page.run('view.status={stream_settings:{choice:{fps:0,size:"half"},fps:0,max_size:null,scale_factor:0.5}};syncStreamMenu()');
  assert.equal(page.nodes["display-stream"].textContent, "Streaming at frames on change, 50% scale");
  // iOS changes in place: no Reconnecting.
  assert.equal(page.sandbox.view.note, "");
});

test("page: QW-05-S01 a stored choice is shown and sent again whenever the control socket opens", () => {
  const page = displayPage({ platform: "android", id: "android~emulator-5554", stored: { fps: 15, size: "half" } });
  page.run("syncStreamMenu()");
  assert.equal(page.nodes["display-fps"].value, "15");
  assert.equal(page.nodes["display-size"].value, "half");
  assert.deepEqual(page.sent, []);
  page.run("streamMenuOpened()");
  assert.deepEqual(page.sent, [{ t: "stream", fps: 15, size: "half" }]);
  page.run("streamMenuOpened()");
  assert.equal(page.sent.length, 2, "a reconnect sends it again; the server ignores an unchanged choice");
  page.run("syncStreamMenu()");
  assert.notEqual(page.sandbox.view.note, "Reconnecting…", "re-sending the stored choice shows no wait");
  // Another device's choice is not this one's.
  const other = displayPage({ platform: "android", id: "android~emulator-5556" });
  other.storage.set("autonom.canvas.display.android~emulator-5554", JSON.stringify({ fps: 15, size: "half" }));
  other.run("streamMenuOpened()");
  assert.deepEqual(other.sent, []);
  // Without a choice the menu shows what the stream uses.
  other.run('view.status={stream_settings:{choice:null,fps:30,max_size:1024,scale_factor:null}};syncStreamMenu()');
  assert.equal(other.nodes["display-fps"].value, "30");
  assert.equal(other.nodes["display-size"].value, "full");
  assert.equal(other.nodes["display-stream"].textContent, "Streaming at 30 fps, 1024 px on the long side");
});

test("page: a refused choice is forgotten and the menu goes back to the stream's values", () => {
  // A stored fps 0 is not even read on Android; one the server refuses anyway is dropped.
  const blockedRead = displayPage({ platform: "android", stored: { fps: 0, size: "full" } });
  blockedRead.run("streamMenuOpened()");
  assert.deepEqual(blockedRead.sent, []);
  const page = displayPage({ platform: "ios", stored: { fps: 30, size: "half" } });
  page.run("streamMenuOpened()");
  assert.equal(page.sent.length, 1);
  page.run('view.status={stream_settings:{choice:null,fps:60,max_size:null,scale_factor:1}};' +
    'onStreamRefused({t:"error",for:"stream",message:"fps must be one of: 60, 30, 15, 0"})');
  assert.equal(page.storage.has(page.key), false);
  assert.equal(page.nodes["display-fps"].value, "60");
  assert.equal(page.nodes["display-size"].value, "full");
  assert.equal(page.nodes["display-stream"].textContent, "fps must be one of: 60, 30, 15, 0");
  page.run("streamMenuOpened()");
  assert.equal(page.sent.length, 1, "nothing is re-sent after a refusal");
  // The next pick clears the message.
  page.nodes["display-size"].change("half");
  assert.equal(page.nodes["display-stream"].textContent, "Streaming at 60 fps");
});

test("page: an Android change shows Reconnecting… until frames return, at most DISPLAY_RECONNECT_MS", () => {
  assert.equal(DISPLAY_RECONNECT_MS, 10_000, "QW-04: at most 10 s");
  const page = displayPage({ platform: "android" });
  page.run('view.status={stream_settings:{choice:null,fps:60,max_size:1280,scale_factor:null}};syncStreamMenu()');
  page.nodes["display-size"].change("half");
  assert.equal(page.sandbox.view.note, "Reconnecting…");
  assert.equal(page.nodes["display-stream"].textContent, "Reconnecting…");
  // The stream stops for the restart, then streams again; the first frame after that ends it.
  page.clock.now += 300;
  page.run('view.session="starting";syncStreamMenu()');
  assert.equal(page.nodes["display-stream"].textContent, "Reconnecting…");
  page.clock.now += 1200;
  page.run('view.session="streaming";syncStreamMenu()');
  assert.equal(page.nodes["display-stream"].textContent, "Reconnecting…", "streaming but no frame yet");
  page.run("stats.lastFrameAt=performance.now();syncStreamMenu()");
  assert.equal(page.sandbox.view.note, "");
  assert.equal(page.nodes["display-stream"].textContent, "Streaming at 60 fps, 1280 px on the long side");
  // Frames that never return: it gives up after DISPLAY_RECONNECT_MS.
  page.nodes["display-fps"].change("15");
  page.run('view.session="starting";syncStreamMenu()');
  page.clock.now += DISPLAY_RECONNECT_MS - 1;
  page.run("syncStreamMenu()");
  assert.equal(page.nodes["display-stream"].textContent, "Reconnecting…");
  page.clock.now += 2;
  page.run("syncStreamMenu()");
  assert.notEqual(page.nodes["display-stream"].textContent, "Reconnecting…");
});

test("page: the Android Reconnecting… notice ends at 10 s by its own timer, with no render after it (QW-04)", () => {
  assert.ok(DISPLAY_RECONNECT_MS <= 10_000, "QW-04: at most 10 s");
  const page = displayPage({ platform: "android" });
  page.run('view.status={stream_settings:{choice:null,fps:60,max_size:1280,scale_factor:null}};syncStreamMenu()');
  page.nodes["display-fps"].change("30");
  page.run('view.session="starting";syncStreamMenu()');
  assert.equal(page.sandbox.view.note, "Reconnecting…");
  // One timer, at most 10 s after the pick.
  assert.equal(page.timers.length, 1);
  assert.ok(page.timers[0].ms <= 10_000, `timer at ${page.timers[0].ms} ms`);
  assert.equal(page.timers[0].ms, DISPLAY_RECONNECT_MS);
  // The stream never returns and nothing renders: the timer alone ends the notice.
  page.clock.now += page.timers[0].ms;
  page.timers[0].callback();
  assert.equal(page.sandbox.view.note, "");
  assert.notEqual(page.nodes["display-stream"].textContent, "Reconnecting…");
  assert.deepEqual(page.chromeRenders, [""], "the notice line is redrawn empty");
  // A later pick's wait is not ended by an earlier pick's timer.
  page.nodes["display-fps"].change("15");
  page.run('view.session="starting";syncStreamMenu()');
  assert.equal(page.timers.length, 2);
  page.nodes["display-fps"].change("60");
  assert.equal(page.timers.length, 3);
  page.clock.now += 100;
  page.timers[1].callback();
  assert.equal(page.sandbox.view.note, "Reconnecting…", "the newest pick still waits");
  // iOS changes in place: no wait and no timer.
  const ios = displayPage({ platform: "ios" });
  ios.nodes["display-fps"].change("0");
  assert.equal(ios.timers.length, 0);
  assert.notEqual(ios.sandbox.view.note, "Reconnecting…");
});

test("page: without a control socket the menu is disabled; blocked storage keeps the page working", () => {
  const closed = displayPage({ socketOpen: false });
  closed.run("syncStreamMenu()");
  assert.equal(closed.nodes["display-fps"].disabled, true);
  assert.equal(closed.nodes["display-size"].disabled, true);
  assert.equal(closed.nodes["display-stream"].textContent, "Available while the scrcpy or idb stream runs.");
  const blocked = displayPage({ platform: "ios", blocked: true });
  blocked.nodes["display-fps"].change("30");
  assert.deepEqual(blocked.sent, [{ t: "stream", fps: 30, size: "full" }]);
  blocked.run('onStreamRefused({t:"error",for:"stream",message:"no"})');
});

test("page: a Size picked alone keeps the stream's own frame rate (fps null), even one the menu does not offer", async () => {
  const { parseControlMessage } = await import("../plugins/autonom/skills/android-emulator-browser/scripts/browser-lib.mjs");
  // Canvas started with --fps 24: the menu shows the disabled "24 fps (--fps)" entry.
  const page = displayPage({ platform: "android", id: "android~emulator-5554", fpsShown: "24" });
  page.run('view.status={stream_settings:{choice:null,fps:24,max_size:1280,scale_factor:null}};syncStreamMenu()');
  assert.equal(page.nodes["display-fps"].value, "24");
  page.nodes["display-size"].change("half");
  assert.deepEqual(page.sent, [{ t: "stream", fps: null, size: "half" }]);
  // The server accepts it on both platforms (QW-04-S03 refuses only values outside the menu).
  assert.deepEqual(parseControlMessage(JSON.stringify(page.sent[0]), { platform: "android" }), { t: "stream", fps: null, size: "half" });
  assert.deepEqual(JSON.parse(page.storage.get(page.key)), { fps: null, size: "half" });
  // The menu goes on showing the rate in use.
  page.run('view.status={stream_settings:{choice:{fps:null,size:"half"},fps:24,max_size:640,scale_factor:null}};syncStreamMenu()');
  assert.equal(page.nodes["display-fps"].value, "24");
  assert.equal(page.nodes["display-size"].value, "half");
  // A Frame rate pick sets the rate; a later Size pick keeps that rate.
  page.nodes["display-fps"].change("30");
  assert.deepEqual(page.sent.at(-1), { t: "stream", fps: 30, size: "half" });
  page.nodes["display-size"].change("full");
  assert.deepEqual(page.sent.at(-1), { t: "stream", fps: 30, size: "full" });
  // A reload sends the stored fps-null choice as it is.
  const reloaded = displayPage({ platform: "android", id: "android~emulator-5554", stored: { fps: null, size: "half" } });
  reloaded.run("streamMenuOpened()");
  assert.deepEqual(reloaded.sent, [{ t: "stream", fps: null, size: "half" }]);
  // A background tile shows its background 30 fps; Size there does not make 30 the choice.
  const tile = displayPage({ platform: "ios", id: "ios~T", fpsShown: "30" });
  tile.run('view.status={stream_settings:{choice:null,fps:30,max_size:null,scale_factor:0.5}};syncStreamMenu()');
  tile.nodes["display-size"].change("half");
  assert.deepEqual(tile.sent, [{ t: "stream", fps: null, size: "half" }]);
});
