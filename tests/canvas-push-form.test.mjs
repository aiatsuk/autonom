// Unit tests of the Canvas push form module: the pure build / JSON / summary / status / storage
// functions, the markup and the inlined script as text, and a round trip of the messages the
// page builds through the real tools process (scripts/autonom_canvas_tools.py) and its
// read-only push.compose op, which never reaches a device. No device, Canvas or network.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import test, { describe } from "node:test";
import { Script } from "node:vm";

import {
  pushFormController,
  pushFormCore,
  pushFormMarkup,
  pushFormScript,
  pushFormStyles,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-push-form.mjs";

const ROOT = resolve(import.meta.dirname, "..");
const core = pushFormCore();

/** A form with the given fields over the defaults. */
function form(fields = {}) { return { ...core.defaults(), ...fields }; }
const rows = (...pairs) => pairs.map(([key, value]) => ({ key, value }));
const android = (fields, ctx = {}) => core.build(form(fields), { platform: "android", app_id: "com.example.app", delivery: "app", ...ctx });
const ios = (fields, ctx = {}) => core.build(form(fields), { platform: "ios", app_id: "com.example.App", ...ctx });
const firstError = (result) => (result.errors[0] || {}).message || "";

// --- markup, styles and script as text ---------------------------------------------------------

describe("markup, styles and script", () => {
  test("both platforms: App, Title, Body, Badge, Sound, Silent, Data, Edit as JSON, Preview and Reset, each field labelled", () => {
    for (const platform of ["ios", "android"]) {
      const markup = pushFormMarkup(platform);
      for (const [id, label] of [["tools-push-app", "App"], ["tools-push-title", "Title"], ["tools-push-body", "Body"], ["tools-push-badge", "Badge"],
        ["tools-push-sound", "Sound"], ["tools-push-payload", "Message as JSON"]]) {
        assert.match(markup, new RegExp(`<label class="tools-label" for="${id}">${label}</label>`), `${platform} ${id}`);
      }
      assert.match(markup, /placeholder="Same as the App tab"/);
      assert.match(markup, /<input id="tools-push-sound"[^>]* placeholder="default"/);
      assert.match(markup, /<label class="tools-check"><input id="tools-push-silent" type="checkbox"> Silent \(data only, no banner\)<\/label>/);
      assert.match(markup, /<label class="tools-check"><input id="tools-push-json-mode" type="checkbox"> Edit as JSON<\/label>/);
      assert.match(markup, />\+ Add field</);
      assert.match(markup, /The key your app reads a link from \(for example <code>url<\/code>\) goes here\./);
      assert.match(markup, /<button type="button" class="btn" id="tools-push-preview-run">Preview<\/button>/);
      assert.match(markup, /id="tools-push-reset">Reset<\/button>/);
      assert.match(markup, /<pre class="tools-pre" id="tools-push-preview-body">/);
      assert.match(markup, /<details class="tools-sim-details tools-push-details" id="tools-push-details"><summary>/);
      // A second primary Send in the footer, left of Preview and Reset, sent by the page exactly as
      // the row's Send (data-sim, data-mutate); its result line sits right under the buttons.
      assert.match(markup, new RegExp('<div class="tools-actions"><button type="button" class="btn primary" id="tools-push-send" data-mutate data-sim="push" ' +
        'data-action="send" data-push-footer aria-label="Send push" title="Send push">Send</button>' +
        '<button type="button" class="btn" id="tools-push-preview-run">Preview</button>' +
        '<button type="button" class="btn small tools-push-reset" id="tools-push-reset">Reset</button></div>' +
        '<div class="tools-msg tools-push-status" id="tools-push-status" role="status" aria-live="polite" hidden></div>'));
      assert.equal((markup.match(/data-mutate/g) || []).length, 1, "the footer Send is the form's only leased control");
      assert.equal((markup.match(/data-sim=/g) || []).length, 1);
      // Visual order is tab order: the fields, then the delivery, the JSON switch and the actions.
      const order = ["tools-push-app", "tools-push-title", "tools-push-body", "tools-push-badge", "tools-push-sound", "tools-push-silent",
        "tools-push-data-add", "tools-push-payload", ...(platform === "android" ? ["tools-push-delivery", "tools-push-stopped"] : []),
        "tools-push-json-mode", "tools-push-send", "tools-push-preview-run", "tools-push-reset", "tools-push-status"].map((id) => markup.indexOf(`id="${id}"`));
      assert.deepEqual(order, [...order].sort((a, b) => a - b), platform);
      assert.ok(order.every((at) => at >= 0));
    }
    assert.match(pushFormMarkup("ios"), /for="tools-push-subtitle">Subtitle</);
    assert.doesNotMatch(pushFormMarkup("android"), /tools-push-subtitle/);
    assert.doesNotMatch(pushFormMarkup("ios"), /tools-push-delivery|tools-push-stopped/);
    const androidMarkup = pushFormMarkup("android");
    assert.match(androidMarkup, /<select id="tools-push-delivery"><option value="app">To the app<\/option><option value="shade">Notification shade only<\/option><\/select>/);
    assert.match(androidMarkup, /<label class="tools-check"><input id="tools-push-stopped" type="checkbox"> Reach a stopped app<\/label>/);
  });

  test("the script parses, has no backticks, never writes HTML and keeps the controller's names", () => {
    const script = pushFormScript();
    assert.doesNotThrow(() => new Script(script));
    assert.equal(script.includes("`"), false);
    assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|localStorage/);
    assert.match(script, /^const PUSH_FORM=\(function pushFormCore\(\)/);
    assert.match(script, /const pushFormController=function pushFormController\(env\)/);
    assert.equal(typeof pushFormController, "function");
  });

  test("the styles reuse the drawer's tokens and add no colour or font", () => {
    const css = pushFormStyles();
    assert.doesNotMatch(css, /#[0-9a-f]{3,6}\b|rgb\(|hsl\(|font-family/i);
    assert.match(css, /var\(--t-danger\)/);
    assert.match(css, /var\(--control-small\)/);
    assert.match(css, /text-overflow:ellipsis/, "the summary stays one line");
    assert.equal((css.match(/\{/g) || []).length, (css.match(/\}/g) || []).length);
  });
});

// --- Send's values ----------------------------------------------------------------------------

describe("build: the values Send takes", () => {
  test("the default message on each platform", () => {
    assert.deepEqual(core.defaults(), { title: "Autonom", body: "Hello from the Canvas", subtitle: "", badge: "", sound: "", silent: false, rows: [], json: false, json_text: "" });
    assert.deepEqual(ios({}).values, { app_id: "com.example.App", message: { title: "Autonom", body: "Hello from the Canvas" } });
    assert.deepEqual(android({}).values, { app_id: "com.example.app", message: { title: "Autonom", body: "Hello from the Canvas" }, delivery: "app", include_stopped: false });
  });

  test("a plain message: text is trimmed, blank fields are left out, Subtitle only on iOS", () => {
    const fields = { title: "  Order shipped ", body: "Your order\nis on its way ", subtitle: "Today", badge: " 3 ", sound: " default " };
    assert.deepEqual(ios(fields).values.message, { title: "Order shipped", subtitle: "Today", body: "Your order\nis on its way", badge: 3, sound: "default" });
    assert.deepEqual(android(fields).values.message, { title: "Order shipped", body: "Your order\nis on its way", badge: 3, sound: "default" });
    assert.deepEqual(android({ title: " ", body: "Only a body", sound: "  " }).values.message, { body: "Only a body" });
  });

  test("a silent data message drops the alert fields", () => {
    const silent = { silent: true, subtitle: "x", rows: rows(["order_id", "42"], ["url", "myapp://orders/42"]) };
    assert.deepEqual(ios(silent).values.message, { silent: true, data: { order_id: "42", url: "myapp://orders/42" } });
    assert.deepEqual(android(silent).values.message, { silent: true, data: { order_id: "42", url: "myapp://orders/42" } });
    assert.deepEqual(ios({ silent: true }).values.message, { silent: true }, "Silent alone says something");
  });

  test("data rows: an empty key is skipped, a key is trimmed, a value is kept as typed", () => {
    const result = android({ rows: rows(["", "ignored"], ["  ", "also ignored"], [" url ", " myapp://x "], ["empty", ""]) });
    assert.deepEqual(result.values.message.data, { url: " myapp://x ", empty: "" });
    assert.deepEqual(android({ rows: rows(["", "x"]) }).values.message, { title: "Autonom", body: "Hello from the Canvas" }, "no data object for empty rows");
  });

  test("data rows: a duplicate key and every reserved key are refused, naming the key and the row", () => {
    const duplicate = android({ rows: rows(["url", "a"], ["n", "1"], [" url", "b"]) });
    assert.equal(duplicate.values, null);
    assert.deepEqual(duplicate.errors.map((error) => [error.field, error.index, error.part]), [["data", 2, "key"]]);
    assert.match(firstError(duplicate), /The data key \u201curl\u201d is used twice/);
    for (const key of ["aps", "from", "message_type", "collapse_key", "google.message_id", "google.c.a.e", "gcm.n.e", "gcm.notification.title"]) {
      const result = ios({ rows: rows(["ok", "1"], [key, "x"]) });
      assert.equal(result.values, null, key);
      assert.deepEqual(result.errors.map((error) => [error.field, error.index]), [["data", 1]], key);
      assert.match(firstError(result), new RegExp(`The data key \u201c${key.replace(/\./g, "\\.")}\u201d is reserved`), key);
    }
    for (const key of ["google", "gcm", "apsx", "fromage", "my.google.key"]) assert.ok(android({ rows: rows([key, "x"]) }).values, key);
  });

  test("badge: empty is none, a whole number 0 or more is sent, anything else is refused", () => {
    assert.equal("badge" in ios({ badge: "" }).values.message, false);
    assert.equal(ios({ badge: "0" }).values.message.badge, 0);
    assert.equal(ios({ badge: "3" }).values.message.badge, 3);
    for (const text of ["-1", "1.5", "two", "1e3", "+2", "9007199254740993"]) {
      const result = ios({ badge: text });
      assert.equal(result.values, null, text);
      assert.deepEqual(result.errors.map((error) => error.field), ["badge"], text);
      assert.match(firstError(result), /Badge must be a whole number, 0 or more/, text);
    }
    assert.deepEqual(core.badge(" 12 "), { value: 12 });
  });

  test("a message must say something: a title, a body, data or Silent", () => {
    const result = android({ title: "", body: " ", sound: "default", badge: "1" });
    assert.equal(result.values, null);
    assert.deepEqual(result.errors.map((error) => error.field), ["content"]);
    assert.match(firstError(result), /Add a title, a body or a data field, or tick Silent/);
    assert.ok(android({ title: "", body: "", rows: rows(["k", "v"]) }).values);
  });

  test("JSON mode: a message object is sent as it is; a raw APNs object is a payload on iOS and refused on Android", () => {
    const message = { title: "Sale", category: "promo", data: { n: 1 }, ios: { aps: { "interruption-level": "time-sensitive" } } };
    assert.deepEqual(ios({ json: true, json_text: JSON.stringify(message) }).values, { app_id: "com.example.App", message });
    assert.deepEqual(android({ json: true, json_text: JSON.stringify(message) }).values, { app_id: "com.example.app", message, delivery: "app", include_stopped: false });
    const raw = { aps: { alert: { title: "Raw" }, sound: "default" }, deeplink: "myapp://x" };
    assert.deepEqual(ios({ json: true, json_text: JSON.stringify(raw) }).values, { app_id: "com.example.App", payload: raw });
    const refused = android({ json: true, json_text: JSON.stringify(raw) });
    assert.equal(refused.values, null);
    assert.deepEqual(refused.errors.map((error) => error.field), ["json"]);
    assert.match(firstError(refused), /A raw APNs payload \(an object with an aps key\) works on iOS only/);
    for (const [text, expected] of [["{not json", /The JSON is not valid/], ["[1,2]", /The message must be a JSON object/], ["null", /must be a JSON object/],
      ["\"text\"", /must be a JSON object/], ["{}", /Add a title, a body or data/], ["{\"badge\": 2}", /Add a title, a body or data/]]) {
      const result = ios({ json: true, json_text: text, title: "ignored in JSON mode" });
      assert.equal(result.values, null, text);
      assert.match(firstError(result), expected, text);
    }
    assert.ok(ios({ json: true, json_text: "{\"silent\": true}" }).values, "silent alone");
  });

  test("the text is at most 4 KB in UTF-8, counted as compact JSON, in both modes", () => {
    const fits = (fields) => ios(fields).values !== null;
    // {"title":"Autonom","body":"..."} is 29 bytes around the body; "é" is 2 bytes.
    assert.equal(utf8(JSON.stringify({ title: "Autonom", body: "" })), 29);
    assert.equal(fits({ body: "\u00e9".repeat(2033) }), true, "4095 bytes");
    const over = ios({ body: "\u00e9".repeat(2034) });
    assert.equal(over.values, null);
    assert.deepEqual(over.errors.map((error) => error.field), ["size"]);
    assert.match(firstError(over), /The message is larger than 4 KB \(4097 bytes, at most 4096\)/);
    const raw = JSON.stringify({ aps: { alert: "x".repeat(5000) } });
    assert.match(firstError(ios({ json: true, json_text: raw })), /The payload is larger than 4 KB/);
    // Spaces in the typed JSON do not count: what is sent is compact.
    const spaced = "{" + " ".repeat(200) + "\"title\": \"" + "x".repeat(4000) + "\"\n}";
    assert.ok(utf8(spaced) > 4096);
    assert.ok(ios({ json: true, json_text: spaced }).values);
  });

  test("the app id: from the form or the App tab; required except for shade delivery", () => {
    const missing = android({}, { app_id: "  " });
    assert.equal(missing.values, null);
    assert.deepEqual(missing.errors.map((error) => error.field), ["app"]);
    assert.match(firstError(missing), /Enter a package here or on the App tab/);
    assert.match(firstError(ios({}, { app_id: "" })), /Enter a bundle id here or on the App tab/);
    assert.deepEqual(android({}, { app_id: "", delivery: "shade" }).values, { message: { title: "Autonom", body: "Hello from the Canvas" }, delivery: "shade" });
    assert.deepEqual(android({}, { app_id: " com.example.app " }).values.app_id, "com.example.app");
  });

  test("Android: delivery and Reach a stopped app; shade needs a title or a body and never sends include_stopped", () => {
    assert.deepEqual(android({}, { include_stopped: true }).values, { app_id: "com.example.app", message: { title: "Autonom", body: "Hello from the Canvas" }, delivery: "app", include_stopped: true });
    assert.deepEqual(android({}, { delivery: "shade", include_stopped: true }).values, { app_id: "com.example.app", message: { title: "Autonom", body: "Hello from the Canvas" }, delivery: "shade" });
    assert.equal(android({}, { delivery: "sky" }).values.delivery, "app", "anything but shade is app");
    const dataOnly = android({ title: "", body: "", rows: rows(["k", "v"]) }, { delivery: "shade" });
    assert.equal(dataOnly.values, null);
    assert.match(firstError(dataOnly), /\u201cNotification shade only\u201d needs a title or a body/);
    assert.match(firstError(android({ json: true, json_text: "{\"data\": {\"k\": \"v\"}}" }, { delivery: "shade" })), /needs a title or a body/);
    assert.equal(ios({}, { delivery: "shade", include_stopped: true }).values.delivery, undefined, "iOS sends neither");
  });
});

// --- fields <-> JSON, summary, preview, status, storage ---------------------------------------

describe("fields and JSON", () => {
  test("fields -> JSON -> fields is the same form", () => {
    const cases = [
      { platform: "ios", fields: form() },
      { platform: "ios", fields: form({ title: "T", subtitle: "S", body: "B", badge: "0", sound: "default", rows: rows(["url", "myapp://x"], ["order_id", "42"]) }) },
      { platform: "android", fields: form({ title: "", body: "", silent: true, badge: "7", rows: rows(["k", ""]) }) },
    ];
    for (const { platform, fields } of cases) {
      const made = core.toJson(fields, platform);
      assert.equal(typeof made.text, "string");
      const back = core.fromJson(made.text, platform);
      assert.deepEqual(back.unshown, []);
      assert.deepEqual(core.build(back.form, { platform, app_id: "a.b" }).values, core.build(fields, { platform, app_id: "a.b" }).values, platform);
      assert.deepEqual(core.toJson(back.form, platform).text, made.text);
    }
    assert.equal(core.toJson(form(), "android").text, JSON.stringify({ title: "Autonom", body: "Hello from the Canvas" }, null, 2));
    assert.equal(core.toJson(form({ title: "", body: "" }), "ios").text, "{}", "the pre-fill does not need a complete message");
    const bad = core.toJson(form({ badge: "-1", rows: rows(["aps", "x"]) }), "ios");
    assert.match(bad.error, /Badge must be a whole number/);
    assert.deepEqual(bad.errors.map((error) => error.field), ["badge", "data"]);
  });

  test("the form cannot show: other fields, values of another type, a raw payload, Subtitle on Android", () => {
    const text = JSON.stringify({ schema: "autonom.push/v1", title: "T", category: "promo", badge: 1.5, silent: "yes", sound: 3,
      data: { n: 1, ok: "yes", nested: { a: 1 } }, image: "https://x/y.png", name: "Saved" });
    assert.deepEqual(core.fromJson(text, "ios").unshown, ["category", "badge", "silent", "sound", "data.n", "data.nested", "image", "name"]);
    assert.deepEqual(core.fromJson(JSON.stringify({ title: "T", subtitle: "S" }), "android").unshown, ["subtitle (iOS only)"]);
    assert.deepEqual(core.fromJson(JSON.stringify({ title: "T", subtitle: "S" }), "ios").unshown, []);
    assert.deepEqual(core.fromJson(JSON.stringify({ aps: {} }), "ios").unshown, ["aps (a raw APNs payload)"]);
    assert.deepEqual(core.fromJson(JSON.stringify({ data: [1] }), "ios").unshown, ["data"]);
    assert.deepEqual(core.fromJson(JSON.stringify({ schema: "other/v9", title: "T" }), "ios").unshown, ["schema"]);
    const many = {};
    for (let i = 0; i < 51; i += 1) many["k" + i] = "v";
    assert.deepEqual(core.fromJson(JSON.stringify({ data: many }), "ios").unshown, ["more than 50 data fields"]);
    assert.match(core.fromJson("{oops", "ios").error, /^The JSON is not valid\. Fix it to go back to the fields\.$/);
    assert.match(core.fromJson("[]", "ios").error, /must be a JSON object/);
  });
});

describe("summary", () => {
  test("one line: title and body, silent and data counts, custom JSON", () => {
    assert.equal(core.summary(form(), "ios"), "Autonom \u2014 Hello from the Canvas");
    assert.equal(core.summary(form({ body: "Line one\n  line two" }), "ios"), "Autonom \u2014 Line one line two");
    assert.equal(core.summary(form({ body: "" }), "android"), "Autonom");
    assert.equal(core.summary(form({ title: "", body: "", subtitle: "Only a subtitle" }), "ios"), "Only a subtitle");
    assert.equal(core.summary(form({ rows: rows(["url", "x"], ["", "skipped"]) }), "ios"), "Autonom \u2014 Hello from the Canvas \u00b7 1 data field");
    assert.equal(core.summary(form({ silent: true, rows: rows(["a", "1"], ["b", "2"]) }), "ios"), "Silent push \u00b7 2 data fields");
    assert.equal(core.summary(form({ silent: true }), "android"), "Silent push");
    assert.equal(core.summary(form({ title: "", body: "", rows: rows(["a", "1"]) }), "android"), "Data push \u00b7 1 data field");
    assert.equal(core.summary(form({ title: "", body: "" }), "android"), "Nothing to send yet");
    assert.equal(core.summary(form({ json: true }), "ios"), "Custom JSON");
  });
});

describe("status line", () => {
  test("each reply shape in plain words", () => {
    const line = (reply, platform = "android") => core.status(reply, platform);
    assert.deepEqual(line({ verified: false, verification: "unavailable" }, "ios"), { text: "Push sent (the device cannot confirm it).", hint: "" });
    assert.equal(line({ verified: true }, "ios").text, "Push sent and confirmed on the device.");
    assert.equal(line({ delivery: "app", verified: true, verification: "read_back" }).text, "Push delivered. It is in the notification shade.");
    assert.equal(line({ delivery: "app", verified: false, verification: "not_in_shade" }).text,
      "Push delivered to the app. Nothing in the shade: an app that is open handles it itself.");
    assert.equal(line({ delivery: "app", verified: false, verification: "receiver_accepted" }).text, "Data push delivered to the app.");
    assert.deepEqual(line({ delivery: "shade", verified: true, verification: "read_back", warnings: [{ code: "push_shade_only", error: "x" }] }),
      { text: "Notification shown in the shade. The app\u2019s code did not run.", hint: "" });
    assert.equal(line({ delivery: "shade", verified: false, verification: "not_in_shade" }).text,
      "Notification posted, but it was not found in the shade. The app\u2019s code did not run.");
    assert.equal(line({ delivery: "app", receiver_result: 0 }).text, "Push sent.");
    assert.equal(line(null).text, "Push sent.");
  });

  test("notifications denied: the line says why nothing is shown, and the warning is not repeated", () => {
    const denied = { code: "push_notifications_denied", error: "not granted", hint: "autonom permissions grant notifications x" };
    assert.deepEqual(core.status({ delivery: "app", verified: false, verification: "not_in_shade", warnings: [denied] }, "android"), {
      text: "Push delivered to the app. Notifications are not allowed for this app, so nothing is shown: grant them on the App tab.", hint: "" });
    const other = core.status({ delivery: "app", verified: false, verification: "not_in_shade", warnings: [denied, { code: "push_payload_large", error: "x" }] }, "android");
    assert.match(other.text, /so nothing is shown/);
    assert.equal(other.hint, "The message is larger than 4 KB: the emulator takes it, a real FCM send would not.");
    // the size warning speaks about the platform it is for: the reply's own platform wins over the page's
    const large = { code: "push_payload_large", error: "x" };
    assert.equal(core.status({ verified: false, warnings: [large] }, "ios").hint, "The payload is larger than 4 KB: the Simulator may take it, a real APNs send would not.");
    assert.equal(core.status({ verified: false, warnings: [{ ...large, platform: "android" }] }, "ios").hint, "The message is larger than 4 KB: the emulator takes it, a real FCM send would not.");
    assert.equal(core.status({ delivery: "app", verified: true, warnings: [{ ...large, platform: "ios" }] }, "android").hint, "The payload is larger than 4 KB: the Simulator may take it, a real APNs send would not.");
    // without the warning the app was in front; with a read-back the warning stays a hint
    assert.equal(core.status({ delivery: "app", verified: false, verification: "not_in_shade" }, "android").text,
      "Push delivered to the app. Nothing in the shade: an app that is open handles it itself.");
    assert.deepEqual(core.status({ delivery: "app", verified: true, verification: "read_back", warnings: [denied] }, "android"),
      { text: "Push delivered. It is in the notification shade.", hint: "Notifications are not allowed for this app: grant them on the App tab." });
  });

  test("a silent push the Simulator took is said to be unconfirmed", () => {
    assert.deepEqual(core.status({ verified: false, verification: "unavailable", warnings: [{ code: "push_silent_unconfirmed", error: "simctl accepted the silent push" }] }, "ios"),
      { text: "Push sent (the device cannot confirm it).", hint: "The Simulator took the silent push; whether the app ran its background code is not known." });
  });

  test("every warning is added once, in plain words", () => {
    const { hint } = core.status({ verified: true, verification: "read_back", warnings: [
      { code: "push_notifications_denied", error: "not granted", hint: "autonom permissions grant notifications x" },
      { code: "push_notifications_denied", error: "again" },
      { code: "slim_push_off", error: "com.apple.apsd is switched off", hint: "Switch it back on with 'autonom simulator slim enable --value ids=com.apple.apsd'." },
      { code: "push_payload_large", error: "4200 bytes" },
      { code: "something_new", error: "A new warning." },
      { code: "bare_code" },
      "not an object",
    ] }, "android");
    assert.equal(hint, "Notifications are not allowed for this app: grant them on the App tab. " +
      "Push is switched off on this Simulator (simulator slim), so it may not show. Switch it back on with 'autonom simulator slim enable --value ids=com.apple.apsd'. " +
      "The message is larger than 4 KB: the emulator takes it, a real FCM send would not. A new warning. bare_code");
  });

  test("push_not_delivered says what to do for each reason; anything else keeps the server's hint", () => {
    assert.match(core.failureHint({ code: "push_not_delivered", reason: "no_root" }), /without Play Store, or choose \u201cNotification shade only\u201d under Delivery/);
    assert.match(core.failureHint({ code: "push_not_delivered", reason: "not_received" }), /Open the app once, or tick \u201cReach a stopped app\u201d/);
    assert.match(core.failureHint({ code: "push_not_delivered", reason: "no_receiver" }), /Notification shade only/);
    for (const error of [{ code: "push_not_delivered", reason: "rejected" }, { code: "push_not_delivered" }, { code: "app_not_installed", reason: "no_root" }, null]) {
      assert.equal(core.failureHint(error), null, JSON.stringify(error));
    }
  });
});

describe("preview text", () => {
  test("iOS: the payload with its size; Android: the extras, the kind and the shade note; notes without the platform prefix", () => {
    const reply = { ios: { payload: { aps: { alert: "Hi" } }, bytes: 4200, over_limit: true }, android: { extras: { "gcm.n.e": "1", order_id: "42" }, kind: "notification" },
      notes: ["ios: channel is not used (Android only)", "android: subtitle is not used (iOS only)"] };
    assert.deepEqual(core.previewText(reply, "ios", "app"), { head: "The Simulator gets this APNs payload (4200 bytes, above the 4096-byte APNs limit):",
      body: JSON.stringify({ aps: { alert: "Hi" } }, null, 2), notes: "channel is not used (Android only) \u00b7 subtitle is not used (iOS only)" });
    assert.deepEqual(core.previewText({ ...reply, notes: [] }, "android", "app"), { head: "The app gets these FCM extras (a notification message):", body: "gcm.n.e = 1\norder_id = 42", notes: "" });
    assert.match(core.previewText({ android: { extras: {}, kind: "data" } }, "android", "shade").head, /a data message/);
    assert.match(core.previewText({}, "android", "shade").notes, /^With \u201cNotification shade only\u201d the shade shows the title and body and the app gets nothing/);
    assert.deepEqual(core.previewText(null, "ios", "app"), { head: "The Simulator gets this APNs payload (0 bytes):", body: "{}", notes: "" });
    assert.deepEqual(core.preview(form(), "ios"), { message: { title: "Autonom", body: "Hello from the Canvas" } });
    assert.deepEqual(core.preview(form({ json: true, json_text: "{\"aps\":{}}" }), "ios"), { raw: { aps: {} }, bytes: 10 });
    assert.match(core.preview(form({ badge: "x" }), "ios").errors[0].message, /Badge/);
  });
});

describe("remembered state", () => {
  test("snapshot and restore round trip, without the app id; anything broken falls back to the defaults", () => {
    const fields = form({ title: "T", rows: rows(["url", "x"]), json: true, json_text: "{\"title\":\"T\"}" });
    const text = core.snapshot(fields, { delivery: "shade", include_stopped: true, app_id: "com.private.app" }, true);
    assert.equal(text.includes("com.private.app"), false);
    assert.deepEqual(core.restore(text, "android"), { form: fields, delivery: "shade", include_stopped: true, open: true });
    assert.equal(core.restore(text, "ios").delivery, "app", "iOS has no shade");
    const fallback = { form: core.defaults(), delivery: "app", include_stopped: false, open: false };
    const valid = JSON.parse(text);
    const broken = [null, "", "{not json", "[]", "42", JSON.stringify({ ...valid, v: 2 }),
      JSON.stringify({ ...valid, form: { ...valid.form, title: 5 } }),
      JSON.stringify({ ...valid, form: { ...valid.form, silent: "yes" } }),
      JSON.stringify({ ...valid, form: { ...valid.form, rows: [["a"]] } }),
      JSON.stringify({ ...valid, form: { ...valid.form, rows: Array.from({ length: 51 }, () => ["k", "v"]) } }),
      JSON.stringify({ ...valid, form: { ...valid.form, json_text: "x".repeat(16385) } }),
      JSON.stringify({ ...valid, form: { ...valid.form, body: "x".repeat(4097) } })];
    for (const value of broken) {
      const restored = core.restore(value, "android");
      if (value && value.startsWith("{\"v\":1")) assert.deepEqual(restored.form, core.defaults(), value.slice(0, 80));
      else assert.deepEqual(restored, fallback, String(value).slice(0, 40));
    }
    assert.deepEqual(core.restore(JSON.stringify({ v: 1, form: valid.form, delivery: "sky", include_stopped: "yes", open: 1 }), "android"),
      { form: fields, delivery: "app", include_stopped: false, open: false });
  });
});

// --- the real tools process: what the page builds, the message model takes -----------------

function utf8(text) { return Buffer.byteLength(text, "utf8"); }

/** The Canvas tools process of this checkout in a temporary AUTONOM_HOME; push.compose only (no device). */
async function toolsProcess(t, platform) {
  const home = await mkdtemp(join(tmpdir(), "autonom-push-form-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const python = process.env.AUTONOM_TEST_PYTHON || "python3";
  const target = platform === "ios" ? "3760A5A8-E59D-4AE6-B1AF-A626908D7B61" : "emulator-5580";
  const child = spawn(python, ["-B", join(ROOT, "scripts/autonom_canvas_tools.py"), "--platform", platform, "--target", target,
    "--tool", "/usr/bin/false", "--captures-dir", join(home, "captures")], {
    cwd: ROOT, stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, AUTONOM_HOME: join(home, "home"), XDG_STATE_HOME: join(home, "state"), AUTONOM_CAPTURES_DIR: join(home, "captures"), PYTHONDONTWRITEBYTECODE: "1" },
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const waiting = new Map();
  createInterface({ input: child.stdout }).on("line", (line) => {
    const reply = JSON.parse(line);
    waiting.get(reply.id)?.(reply);
    waiting.delete(reply.id);
  });
  t.after(() => { child.stdin.end(); child.kill(); });
  let next = 0;
  return (op, payload = {}) => new Promise((resolveReply, reject) => {
    next += 1;
    waiting.set(next, resolveReply);
    child.once("exit", (code) => reject(new Error(`the tools process exited (${code}): ${stderr}`)));
    child.stdin.write(JSON.stringify({ id: next, op, origin: "human", payload }) + "\n");
  });
}

describe("round trip through the tools process", () => {
  for (const platform of ["ios", "android"]) {
    test(`${platform}: every message the form builds is valid, and Preview reads every field it shows from the compose reply`, async (t) => {
      const call = await toolsProcess(t, platform);
      const forms = [
        form(),
        form({ title: "Order shipped", subtitle: "Today", body: "Your order #42 is on its way", badge: "0", sound: "default", rows: rows(["url", "myapp://orders/42"], ["order_id", "42"]) }),
        form({ title: "", body: "", silent: true, rows: rows(["refresh", "1"]) }),
        form({ title: "", body: "", badge: "5", rows: rows(["k", ""]) }),
        form({ json: true, json_text: JSON.stringify({ title: "From JSON", category: "promo", data: { n: 1 } }) }),
      ];
      for (const fields of forms) {
        const request = core.preview(fields, platform);
        assert.ok(request.message, JSON.stringify(fields));
        const reply = await call("push.compose", { message: request.message, platform });
        assert.equal(reply.ok, true, JSON.stringify(reply));
        const result = reply.result;
        const { schema: _schema, ...echoed } = result.message;
        assert.deepEqual(echoed, request.message, "the model keeps every field the form sends");
        const shown = core.previewText(result, platform, "app");
        if (platform === "ios") {
          assert.ok(result.ios.bytes > 0);
          assert.equal(shown.head, `The Simulator gets this APNs payload (${result.ios.bytes} bytes):`);
          assert.deepEqual(JSON.parse(shown.body), result.ios.payload);
          assert.equal("android" in result, false);
        } else {
          assert.equal(shown.head, `The app gets these FCM extras (${result.android.kind === "data" ? "a data message" : "a notification message"}):`);
          assert.deepEqual(shown.body.split("\n").map((line) => line.split(" = ")[0]), Object.keys(result.android.extras));
          assert.equal("ios" in result, false);
        }
        assert.ok(Array.isArray(result.notes));
        const values = core.build(fields, { platform, app_id: platform === "ios" ? "com.example.App" : "com.example.app", delivery: "app" }).values;
        assert.deepEqual(values.message, request.message, "Send and Preview take the same message");
      }
      // The page refuses locally what the model refuses too.
      const reserved = await call("push.compose", { message: { title: "x", data: { "gcm.n.e": "1" } }, platform });
      assert.equal(reserved.ok, false);
      assert.equal(core.build(form({ rows: rows(["gcm.n.e", "1"]) }), { platform, app_id: "a.b" }).values, null);
      const silentTitle = await call("push.compose", { message: { title: "x", silent: true }, platform });
      assert.equal(silentTitle.ok, false, "silent with a title is refused by the model; the form drops the title instead");
    });
  }
});
